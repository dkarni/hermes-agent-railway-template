"""Initial 30-day history ingestion (PRD sec 8.3).

For wallets lacking complete history, paginate trades back to the lookback cutoff
and store them in ``wallet_trades`` (raw ingested history, distinct from
observed_trades). Resolve the involved markets via gamma in batches and upsert
into ``markets``. Concurrency-limited across wallets; records completeness on
the profile.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import aiosqlite

from ..adapters.dataapi import DataApiAdapter
from ..adapters.gamma import GammaAdapter
from ..adapters.models import WalletTrade
from ..config import Config
from ..db import px_to_micro, usd_to_micro, utcnow_iso
from ..jobs.common import now_ts, upsert_market
from ..jobs.runner import JobContext

DEFAULT_CONCURRENCY = 3


async def run_ingest_history(
    ctx: JobContext,
    config: Config,
    dataapi: DataApiAdapter,
    gamma: GammaAdapter,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit_wallets: int | None = None,
) -> dict:
    wallets = await _pending_wallets(ctx.conn, limit=limit_wallets)
    if not wallets:
        return {"pending_wallets": 0, "ingested": 0}

    lookback_cutoff = now_ts() - config.wallet_lookback_days * 86400
    sem = asyncio.Semaphore(concurrency)
    condition_ids: set[str] = set()
    counts: dict[str, int] = {}

    failed: set[str] = set()

    async def _one(wallet: str) -> None:
        async with sem:
            try:
                trades = await dataapi.iter_user_trades(
                    wallet,
                    stop_predicate=lambda t: t.timestamp < lookback_cutoff,
                )
                ctx.read(len(trades))
                n = await _store_trades(ctx, wallet, trades)
                counts[wallet] = n
                for t in trades:
                    if t.condition_id:
                        condition_ids.add(t.condition_id)
            except Exception as exc:  # noqa: BLE001 - one wallet must not kill the batch
                failed.add(wallet)
                await ctx.add_data_quality_event(
                    severity="error",
                    event_type="wallet_ingest_failed",
                    source="dataapi",
                    entity_type="wallet",
                    entity_id=wallet,
                    details={"type": type(exc).__name__, "message": str(exc)[:500]},
                )

    await asyncio.gather(*[_one(w) for w in wallets])
    # Failed wallets stay history_complete=0 and are retried on the next run.
    wallets = [w for w in wallets if w not in failed]

    # Resolve involved markets (gamma handles batching internally).
    resolved_markets = 0
    if condition_ids:
        markets = await gamma.get_markets_by_condition_ids(sorted(condition_ids))
        mapped = {m.condition_id for m in markets}
        for market in markets:
            await upsert_market(ctx.conn, market)
            resolved_markets += 1
        unmapped = condition_ids - mapped
        if unmapped:
            await ctx.add_data_quality_event(
                severity="warning",
                event_type="unmapped_markets",
                source="gamma",
                details={"count": len(unmapped), "sample": sorted(unmapped)[:5]},
            )

    # Mark completeness on profiles and seed the monitor cursor to the newest
    # ingested trade so the monitor only emits genuinely new signals (observed
    # trades are monitor-detected, never backfilled history).
    now = utcnow_iso()
    for wallet in wallets:
        await ctx.conn.execute(
            """
            UPDATE wallet_profiles
               SET history_complete = 1, history_ingested_at = ?
             WHERE wallet_address = ?
            """,
            (now, wallet),
        )
        cur = await ctx.conn.execute(
            "SELECT MAX(ts) FROM wallet_trades WHERE proxy_wallet = ?", (wallet,)
        )
        max_ts_row = await cur.fetchone()
        newest = int(max_ts_row[0]) if max_ts_row and max_ts_row[0] else 0
        await ctx.conn.execute(
            """
            INSERT INTO monitor_cursors (wallet_address, last_trade_ts, last_polled_at, overlap_seconds)
            VALUES (?, ?, ?, 120)
            ON CONFLICT(wallet_address) DO UPDATE SET
                last_trade_ts = MAX(monitor_cursors.last_trade_ts, excluded.last_trade_ts)
            """,
            (wallet, newest, now),
        )
    await ctx.conn.commit()

    return {
        "pending_wallets": len(wallets) + len(failed),
        "ingested_trades": sum(counts.values()),
        "resolved_markets": resolved_markets,
        "failed_wallets": len(failed),
    }


async def _pending_wallets(conn: aiosqlite.Connection, *, limit: int | None) -> list[str]:
    # Priority: best (lowest) leaderboard rank first, so the first batches
    # after a scan ingest the top-ranked wallets, not set-iteration order.
    sql = (
        "SELECT wp.wallet_address, "
        "       (SELECT MIN(le.rank) FROM leaderboard_entries le "
        "         WHERE le.wallet_address = wp.wallet_address) AS best_rank "
        "FROM wallet_profiles wp WHERE wp.history_complete = 0 "
        "ORDER BY best_rank IS NULL, best_rank, wp.first_seen_at IS NULL, wp.first_seen_at"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cursor = await conn.execute(sql)
    return [row[0] for row in await cursor.fetchall()]


async def _store_trades(ctx: JobContext, wallet: str, trades: list[WalletTrade]) -> int:
    now = utcnow_iso()
    written = 0
    for t in trades:
        try:
            price_micro = px_to_micro(t.price)
        except ValueError:
            ctx.skipped()
            continue
        cursor = await ctx.conn.execute(
            """
            INSERT OR IGNORE INTO wallet_trades (
                proxy_wallet, condition_id, asset_id, transaction_hash, side,
                outcome, outcome_index, price_micro, size, ts, title, slug,
                event_slug, category, ingestion_run_id, ingested_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                t.condition_id or None,
                t.asset_id or None,
                t.transaction_hash or None,
                t.side,
                t.outcome or None,
                t.outcome_index,
                price_micro,
                usd_to_micro(t.size),
                t.timestamp,
                t.title or None,
                t.slug or None,
                t.event_slug or None,
                None,
                ctx.job_run_id,
                now,
                # Bulk 30d history is re-fetchable public data; storing every
                # raw payload bloated the DB by ~1GB (retention period: zero).
                None,
            ),
        )
        if cursor.rowcount:
            written += 1
            ctx.written()
        else:
            ctx.skipped()
    await ctx.conn.commit()
    return written
