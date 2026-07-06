"""Reconciliation job (PRD sec 9, every 15 min).

Re-polls tracked wallets with a wider overlap window and dedupes (repairing
missed/delayed events), and refreshes ``markets`` rows whose status may have
changed — for now, markets referenced by observed trades in the last 24h.
Records resolution (winning outcome from gamma: closed + outcomePrices) and logs
data_quality_events for gaps.
"""

from __future__ import annotations

import json
from decimal import Decimal

import aiosqlite

from ..adapters.clob import ClobAdapter
from ..adapters.dataapi import DataApiAdapter
from ..adapters.gamma import GammaAdapter
from ..config import Config
from ..db import utcnow_iso
from ..domain import decisions as dec
from ..domain.decisions import NullPortfolioView
from ..jobs.common import load_active_rule_set, now_ts, upsert_market
from ..jobs.monitor import _dedupe_observed, _process_signal
from ..jobs.runner import JobContext

WIDE_OVERLAP_SECONDS = 30 * 60


async def run_reconcile(
    ctx: JobContext,
    config: Config,
    dataapi: DataApiAdapter,
    gamma: GammaAdapter,
    clob: ClobAdapter,
    *,
    portfolio: dec.PortfolioView | None = None,
) -> dict:
    portfolio = portfolio or NullPortfolioView()
    rule_set_id, version, payload = await load_active_rule_set(ctx.conn)

    repaired = await _repair_wallets(
        ctx, config, dataapi, gamma, clob, payload, rule_set_id, version, portfolio
    )
    resolved = await _refresh_recent_markets(ctx, gamma)
    from .paper_exec import close_due_positions
    from .benchmarks_job import resolve_benchmarks
    exits = await close_due_positions(ctx, clob)
    bench = await resolve_benchmarks(ctx)

    return {
        "repaired_signals": repaired,
        "markets_refreshed": resolved,
        "settled": exits["settled"],
        "sell_closed": exits["sell_closed"],
        "benchmarks_resolved": bench,
    }


async def _repair_wallets(
    ctx, config, dataapi, gamma, clob, payload, rule_set_id, version, portfolio
) -> int:
    cursor = await ctx.conn.execute(
        "SELECT wallet_address FROM wallet_profiles WHERE status = 'track'"
    )
    wallets = [row[0] for row in await cursor.fetchall()]
    repaired = 0
    for wallet in wallets:
        cur = await ctx.conn.execute(
            "SELECT last_trade_ts FROM monitor_cursors WHERE wallet_address = ?", (wallet,)
        )
        row = await cur.fetchone()
        last_ts = int(row[0]) if row and row[0] else 0
        since = max(0, last_ts - WIDE_OVERLAP_SECONDS) if last_ts else 0
        trades = await dataapi.iter_user_trades(
            wallet, stop_predicate=lambda t: t.timestamp < since if since else False
        )
        ctx.read(len(trades))
        for trade in sorted(trades, key=lambda t: t.timestamp):
            observed_id = await _dedupe_observed(ctx, wallet, trade)
            if observed_id is None:
                ctx.skipped()
                continue
            ctx.written()
            await _process_signal(
                ctx, config, gamma, clob, payload, rule_set_id, version,
                wallet, trade, observed_id, portfolio,
            )
            repaired += 1
    await ctx.conn.commit()
    return repaired


async def _refresh_recent_markets(ctx: JobContext, gamma: GammaAdapter) -> int:
    cutoff = _iso_from_ts(now_ts() - 24 * 3600)
    cursor = await ctx.conn.execute(
        """
        SELECT DISTINCT condition_id FROM observed_trades
         WHERE detected_at >= ? AND condition_id IS NOT NULL
        """,
        (cutoff,),
    )
    condition_ids = [row[0] for row in await cursor.fetchall()]
    if not condition_ids:
        return 0
    markets = await gamma.get_markets_by_condition_ids(condition_ids)
    refreshed = 0
    for market in markets:
        await upsert_market(ctx.conn, market)
        refreshed += 1
        if market.resolved:
            await ctx.add_data_quality_event(
                severity="info", event_type="market_resolved", source="gamma",
                entity_type="market", entity_id=market.market_id,
                details={"winning_outcome": _winning(market)},
            )
    mapped = {m.condition_id for m in markets}
    gaps = set(condition_ids) - mapped
    if gaps:
        await ctx.add_data_quality_event(
            severity="warning", event_type="reconcile_unmapped_markets", source="gamma",
            details={"count": len(gaps), "sample": sorted(gaps)[:5]},
        )
    await ctx.conn.commit()
    return refreshed


def _winning(market) -> str | None:
    if not market.resolved:
        return None
    if market.outcomes and market.outcome_prices and len(market.outcomes) == len(market.outcome_prices):
        best = max(range(len(market.outcome_prices)), key=lambda i: market.outcome_prices[i])
        if market.outcome_prices[best] >= Decimal("0.5"):
            return market.outcomes[best]
    return None


def _iso_from_ts(ts_val: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
