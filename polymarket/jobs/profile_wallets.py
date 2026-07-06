"""Wallet profiling job (PRD sec 9, 10, 11).

Recomputes wallet_stats + scoring + category stats for wallets due for refresh
(tracked: 6h, others: daily), writes wallet_profiles (current row) and
wallet_profile_snapshots (append-only history) and wallet_category_stats, and
assigns statuses honouring the TRACKED_WALLET_LIMIT by score rank.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite

from ..config import Config
from ..db import (
    json_dumps,
    micro_to_px,
    micro_to_usd,
    usd_to_micro,
    utcnow_iso,
)
from ..domain import scoring
from ..domain.wallet_stats import (
    ResolvedMarket,
    StatTrade,
    WalletStats,
    compute_wallet_stats,
)
from ..jobs.common import load_active_rule_set, now_ts
from ..jobs.runner import JobContext

TRACKED_REFRESH_SECONDS = 6 * 3600
OTHER_REFRESH_SECONDS = 24 * 3600


async def run_profile_wallets(
    ctx: JobContext, config: Config, *, force_all: bool = False
) -> dict:
    rule_set_id, version, payload = await load_active_rule_set(ctx.conn)
    wallets = await _due_wallets(ctx.conn, force_all=force_all)
    if not wallets:
        return {"due_wallets": 0, "scored": 0}

    ranked: list[scoring.RankedWallet] = []
    computed: dict[str, tuple[WalletStats, scoring.WalletScore]] = {}

    for wallet in wallets:
        trades, resolved, complete = await _load_history(ctx.conn, wallet, config)
        ctx.read(len(trades))
        stats = compute_wallet_stats(
            trades, resolved,
            window_days=config.wallet_lookback_days,
            window_end_ts=now_ts(),
        )
        score = scoring.score_wallet(
            stats, payload,
            history_complete=complete,
            from_partial_scan=False,
            profile_stale=False,
        )
        computed[wallet] = (stats, score)
        ranked.append(scoring.RankedWallet(wallet, score))

    final_status = scoring.enforce_tracked_limit(
        ranked, tracked_wallet_limit=config.tracked_wallet_limit
    )

    for wallet, (stats, score) in computed.items():
        status, reason = final_status[wallet]
        await _persist_profile(ctx, wallet, stats, score, status, reason, version)
        await _persist_category_stats(ctx, wallet, stats, payload)
        ctx.written()

    await ctx.conn.commit()
    tracked = sum(1 for s in final_status.values() if s[0] == "track")
    return {"due_wallets": len(wallets), "scored": len(computed), "tracked": tracked}


async def _due_wallets(conn: aiosqlite.Connection, *, force_all: bool) -> list[str]:
    if force_all:
        cursor = await conn.execute(
            "SELECT wallet_address FROM wallet_profiles WHERE history_complete = 1"
        )
        return [row[0] for row in await cursor.fetchall()]
    now = datetime.now(timezone.utc)
    tracked_cut = (now - timedelta(seconds=TRACKED_REFRESH_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    other_cut = (now - timedelta(seconds=OTHER_REFRESH_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    cursor = await conn.execute(
        """
        SELECT wallet_address FROM wallet_profiles
         WHERE history_complete = 1
           AND (
                last_profiled_at IS NULL
             OR (status = 'track' AND last_profiled_at < ?)
             OR (status != 'track' AND last_profiled_at < ?)
           )
        """,
        (tracked_cut, other_cut),
    )
    return [row[0] for row in await cursor.fetchall()]


async def _load_history(
    conn: aiosqlite.Connection, wallet: str, config: Config
) -> tuple[list[StatTrade], dict[str, ResolvedMarket], bool]:
    cursor = await conn.execute(
        """
        SELECT condition_id, outcome, side, price_micro, size, ts, category
          FROM wallet_trades WHERE proxy_wallet = ? ORDER BY ts
        """,
        (wallet,),
    )
    rows = await cursor.fetchall()
    # Enrich category from markets where the ingest didn't have it.
    market_cache = await _market_map(conn, {r[0] for r in rows if r[0]})
    trades: list[StatTrade] = []
    for r in rows:
        condition_id = r[0] or ""
        category = r[6] or (market_cache.get(condition_id, {}).get("category") or "")
        trades.append(
            StatTrade(
                condition_id=condition_id,
                outcome=r[1] or "",
                side=r[2],
                price=micro_to_px(r[3]) if r[3] is not None else Decimal(0),
                size=micro_to_usd(r[4]) if r[4] is not None else Decimal(0),
                timestamp=int(r[5]),
                category=category,
            )
        )
    resolved: dict[str, ResolvedMarket] = {}
    for condition_id, meta in market_cache.items():
        resolved[condition_id] = ResolvedMarket(
            resolved=meta["status"] == "resolved",
            winning_outcome=meta.get("winning_outcome"),
        )
    cursor = await conn.execute(
        "SELECT history_complete FROM wallet_profiles WHERE wallet_address = ?", (wallet,)
    )
    row = await cursor.fetchone()
    complete = bool(row[0]) if row else False
    return trades, resolved, complete


async def _market_map(conn: aiosqlite.Connection, condition_ids: set[str]) -> dict[str, dict]:
    if not condition_ids:
        return {}
    out: dict[str, dict] = {}
    placeholders = ",".join("?" for _ in condition_ids)
    cursor = await conn.execute(
        f"SELECT condition_id, category, status, winning_outcome FROM markets "
        f"WHERE condition_id IN ({placeholders})",
        tuple(condition_ids),
    )
    for row in await cursor.fetchall():
        out[row[0]] = {"category": row[1], "status": row[2], "winning_outcome": row[3]}
    return out


async def _persist_profile(
    ctx: JobContext,
    wallet: str,
    stats: WalletStats,
    score: scoring.WalletScore,
    status: str,
    reason: str,
    version: int,
) -> None:
    now = utcnow_iso()
    raw = {
        "stats": {
            "net_realized_pnl": str(stats.net_realized_pnl),
            "gross_realized_pnl": str(stats.gross_realized_pnl),
            "roi": str(stats.roi),
            "win_rate": str(stats.win_rate),
            "trade_count": stats.trade_count,
            "resolved_trade_count": stats.resolved_trade_count,
            "positive_day_ratio": str(stats.positive_day_ratio),
            "profit_concentration_top1": str(stats.profit_concentration_top1),
            "profit_concentration_top3": str(stats.profit_concentration_top3),
            "drawdown_estimate": str(stats.drawdown_estimate),
        },
        "components": score.components.as_dict(),
        "weighted_score": str(score.weighted_score),
        "one_hit_wonder_penalty": str(score.one_hit_wonder_penalty),
    }
    # bump profile_version on change
    cursor = await ctx.conn.execute(
        "SELECT profile_version FROM wallet_profiles WHERE wallet_address = ?", (wallet,)
    )
    prev = await cursor.fetchone()
    new_version = (int(prev[0]) + 1) if prev and prev[0] else 1

    await ctx.conn.execute(
        """
        UPDATE wallet_profiles SET
            status = ?, global_score = ?, data_quality_score = ?,
            profile_version = ?, status_reason_code = ?,
            pnl_30d = ?, roi_30d = ?, win_rate = ?,
            resolved_trade_count = ?, trade_count = ?,
            profit_concentration_top1 = ?, profit_concentration_top3 = ?,
            max_drawdown_30d = ?, profile_window_start = ?, profile_window_end = ?,
            calculated_at = ?, last_profiled_at = ?, raw_json = ?
         WHERE wallet_address = ?
        """,
        (
            status,
            int(score.global_score),
            int(score.data_quality_score),
            new_version,
            reason,
            usd_to_micro(stats.net_realized_pnl),
            usd_to_micro(stats.roi),
            usd_to_micro(stats.win_rate),
            stats.resolved_trade_count,
            stats.trade_count,
            usd_to_micro(stats.profit_concentration_top1),
            usd_to_micro(stats.profit_concentration_top3),
            usd_to_micro(stats.drawdown_estimate),
            _iso_from_ts(stats.window_start_ts),
            _iso_from_ts(stats.window_end_ts),
            now,
            now,
            json_dumps(raw),
            wallet,
        ),
    )
    await ctx.conn.execute(
        """
        INSERT INTO wallet_profile_snapshots (
            wallet_address, profile_version, status, global_score,
            data_quality_score, status_reason_code, snapshot_json, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet,
            new_version,
            status,
            int(score.global_score),
            int(score.data_quality_score),
            reason,
            json_dumps(raw),
            now,
        ),
    )


async def _persist_category_stats(
    ctx: JobContext, wallet: str, stats: WalletStats, payload: dict
) -> None:
    now = utcnow_iso()
    for cat, cs in stats.per_category.items():
        await ctx.conn.execute(
            """
            INSERT INTO wallet_category_stats (
                wallet_address, category, trade_count, resolved_trade_count,
                pnl, roi, win_rate, category_score, sample_quality_score,
                window_start, window_end, calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address, category) DO UPDATE SET
                trade_count=excluded.trade_count,
                resolved_trade_count=excluded.resolved_trade_count,
                pnl=excluded.pnl, roi=excluded.roi, win_rate=excluded.win_rate,
                category_score=excluded.category_score,
                sample_quality_score=excluded.sample_quality_score,
                window_start=excluded.window_start, window_end=excluded.window_end,
                calculated_at=excluded.calculated_at
            """,
            (
                wallet,
                cat,
                cs.trade_count,
                cs.resolved_trade_count,
                usd_to_micro(cs.pnl),
                usd_to_micro(cs.roi),
                usd_to_micro(cs.win_rate),
                int(_category_score(cs, stats, payload)),
                min(100, cs.resolved_trade_count * 10),
                _iso_from_ts(stats.window_start_ts),
                _iso_from_ts(stats.window_end_ts),
                now,
            ),
        )


def _category_score(cs, stats: WalletStats, payload: dict) -> Decimal:
    k = int(payload.get("category_shrinkage_k", 10))
    n = Decimal(cs.resolved_trade_count)
    weight = n / (n + Decimal(k)) if (n + k) > 0 else Decimal(0)
    sample = scoring._category_sample_score(cs)
    overall = min(Decimal(100), (stats.win_rate * Decimal(100)))
    return (weight * sample + (Decimal(1) - weight) * overall).quantize(Decimal("1"))


def _iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
