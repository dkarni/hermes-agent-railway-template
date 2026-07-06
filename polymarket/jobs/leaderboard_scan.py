"""Leaderboard scan job (PRD sec 8.2).

Scans OVERALL + configured categories in pages of 50 up to
LEADERBOARD_WALLET_LIMIT, stores leaderboard_scans + leaderboard_entries (raw
json kept), and dedupes the wallet universe into wallet_profiles (new wallets
inserted as insufficient_data). Marks the scan is_partial when pagination is
incomplete; partial scans never promote or downgrade a wallet's status.
"""

from __future__ import annotations

from decimal import Decimal

import aiosqlite

from ..adapters.dataapi import LEADERBOARD_MAX_LIMIT, DataApiAdapter
from ..config import Config
from ..db import usd_to_micro, utcnow_iso
from ..jobs.runner import JobContext


async def run_leaderboard_scan(
    ctx: JobContext, config: Config, adapter: DataApiAdapter
) -> dict:
    categories = ["OVERALL", *[c for c in config.leaderboard_categories if c != "OVERALL"]]
    limit = config.leaderboard_wallet_limit
    total_wallets: set[str] = set()
    scans: list[int] = []
    partial_any = False

    for category in categories:
        scan_id, is_partial, count = await _scan_category(
            ctx, config, adapter, category=category, limit=limit
        )
        scans.append(scan_id)
        partial_any = partial_any or is_partial
        cursor = await ctx.conn.execute(
            "SELECT wallet_address FROM leaderboard_entries WHERE leaderboard_scan_id = ?",
            (scan_id,),
        )
        for row in await cursor.fetchall():
            total_wallets.add(row[0])

    inserted = await _dedupe_universe(ctx, total_wallets)

    return {
        "categories": categories,
        "scan_ids": scans,
        "wallet_universe": len(total_wallets),
        "new_wallets": inserted,
        "is_partial": partial_any,
    }


async def _scan_category(
    ctx: JobContext,
    config: Config,
    adapter: DataApiAdapter,
    *,
    category: str,
    limit: int,
) -> tuple[int, bool, int]:
    started = utcnow_iso()
    expected = limit
    entries: list = []
    offset = 0
    pages = (limit + LEADERBOARD_MAX_LIMIT - 1) // LEADERBOARD_MAX_LIMIT
    is_partial = False

    for _ in range(pages):
        page = await adapter.get_leaderboard(
            time_period="MONTH",
            order_by="PNL",
            category=None if category == "OVERALL" else category,
            limit=LEADERBOARD_MAX_LIMIT,
            offset=offset,
        )
        ctx.read(len(page))
        entries.extend(page)
        if len(page) < LEADERBOARD_MAX_LIMIT:
            # endpoint exhausted before reaching the requested count
            if len(entries) < expected:
                is_partial = True
            break
        offset += LEADERBOARD_MAX_LIMIT
        if len(entries) >= limit:
            break

    entries = entries[:limit]
    status = "partial" if is_partial else "success"

    cursor = await ctx.conn.execute(
        """
        INSERT INTO leaderboard_scans (
            source, category, time_period, order_by, scanned_at,
            expected_wallet_count, actual_wallet_count, lookback_days,
            status, is_partial, duration_ms, raw_summary_json, job_run_id
        ) VALUES ('data-api', ?, 'MONTH', 'PNL', ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            category,
            started,
            expected,
            len(entries),
            config.wallet_lookback_days,
            status,
            1 if is_partial else 0,
            ctx.job_run_id,
        ),
    )
    scan_id = cursor.lastrowid

    for entry in entries:
        await ctx.conn.execute(
            """
            INSERT OR IGNORE INTO leaderboard_entries (
                leaderboard_scan_id, category, wallet_address, rank,
                source_pnl, source_volume, user_name, profile_image,
                verified_badge, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                category,
                entry.wallet_address,
                entry.rank,
                usd_to_micro(entry.pnl),
                usd_to_micro(entry.volume),
                entry.user_name or None,
                entry.profile_image or None,
                1 if entry.verified_badge else 0,
                __import__("json").dumps(entry.raw, separators=(",", ":")),
            ),
        )
        ctx.written()
    await ctx.conn.commit()

    if is_partial:
        await ctx.add_data_quality_event(
            severity="warning",
            event_type="leaderboard_partial_scan",
            source="data-api",
            entity_type="leaderboard_scan",
            entity_id=str(scan_id),
            details={"category": category, "actual": len(entries), "expected": expected},
        )

    return scan_id, is_partial, len(entries)


async def _dedupe_universe(ctx: JobContext, wallets: set[str]) -> int:
    """Insert newly discovered wallets as insufficient_data (never downgrade)."""
    now = utcnow_iso()
    inserted = 0
    for wallet in wallets:
        cursor = await ctx.conn.execute(
            "SELECT 1 FROM wallet_profiles WHERE wallet_address = ?", (wallet,)
        )
        if await cursor.fetchone() is not None:
            continue
        await ctx.conn.execute(
            """
            INSERT OR IGNORE INTO wallet_profiles (
                wallet_address, status, status_reason_code, profile_version,
                first_seen_at, history_complete
            ) VALUES (?, 'insufficient_data', 'newly_discovered', 1, ?, 0)
            """,
            (wallet, now),
        )
        inserted += 1
    await ctx.conn.commit()
    return inserted
