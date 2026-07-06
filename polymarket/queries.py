"""Read queries for the API + dashboard (PRD sec 19 / 20).

Every function is read-only over the shared aiosqlite connection and returns
plain dicts/lists with money & prices already converted to decimal STRINGS via
polymarket.serialize. Demo rows are excluded from aggregate metrics but shown
(flagged is_demo) in list rows. Timestamps pass through as stored (ISO UTC).
"""

from __future__ import annotations

import json
from decimal import Decimal

import aiosqlite

from . import db as dbmod
from . import serialize as ser
from .domain import benchmarks as bm

ZERO = Decimal(0)

# Whitelisted sort columns per list surface (avoid SQL injection via ?sort=).
# DB-column sorts (applied in SQL). paper_pnl is computed post-query.
WALLET_SORTS = {
    "score": "global_score",
    "roi": "roi_30d",
    "resolved": "resolved_trade_count",
    "raw_pnl": "pnl_30d",
    "pnl": "pnl_30d",
    "data_quality": "data_quality_score",
    "profiled": "last_profiled_at",
}
JOURNAL_SORTS = {"created": "created_at", "score": "total_score"}
PAPER_SORTS = {"opened": "opened_at", "pnl": "realized_pnl", "status": "status"}
TRADE_SORTS = {"detected": "detected_at", "score": "total_score"}


def _paginate(limit, offset, *, default_limit=50, max_limit=500):
    try:
        limit = int(limit) if limit is not None else default_limit
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)


# --- overview (PRD 20.2) ----------------------------------------------------

async def overview(conn: aiosqlite.Connection, config) -> dict:
    from .jobs.portfolio_view import get_active_portfolio_id

    portfolio_id = await get_active_portfolio_id(conn)
    equity = None
    cash = None
    starting = None
    total_pnl = None
    today_pnl = None
    open_positions = 0
    max_drawdown = None
    last_pnl_at = None
    version = None

    if portfolio_id is not None:
        cur = await conn.execute(
            "SELECT starting_bankroll, cash_balance FROM paper_portfolios WHERE id = ?",
            (portfolio_id,),
        )
        row = await cur.fetchone()
        starting_micro = int(row[0])
        cash = ser.money(int(row[1]))
        starting = ser.money(starting_micro)

        cur = await conn.execute(
            "SELECT equity, drawdown, collected_at FROM pnl_snapshots "
            "WHERE paper_portfolio_id = ? ORDER BY collected_at DESC LIMIT 1",
            (portfolio_id,),
        )
        snap = await cur.fetchone()
        if snap is not None:
            equity_micro = int(snap[0] or 0)
            equity = ser.money(equity_micro)
            total_pnl = ser.money(equity_micro - starting_micro)
            max_drawdown = ser.money(int(snap[1])) if snap[1] is not None else None
            last_pnl_at = snap[2]
        else:
            equity = cash
            total_pnl = ser.money(int(row[1]) - starting_micro)

        # Today's pnl = latest equity minus the first snapshot of the local/UTC day.
        cur = await conn.execute(
            "SELECT equity FROM pnl_snapshots WHERE paper_portfolio_id = ? "
            "AND substr(collected_at,1,10) = substr(?,1,10) ORDER BY collected_at ASC LIMIT 1",
            (portfolio_id, dbmod.utcnow_iso()),
        )
        first_today = await cur.fetchone()
        if first_today is not None and equity is not None:
            today_pnl = ser.money(int(snap[0] or 0) - int(first_today[0] or 0))

        cur = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE paper_portfolio_id = ? AND status = 'open'",
            (portfolio_id,),
        )
        open_positions = int((await cur.fetchone())[0])

    version = await dbmod.active_rule_set_version(conn)

    # Win rate + sample (resolved filtered trades).
    filtered_rows = await _resolved_metric_rows(conn, portfolio_id) if portfolio_id else []
    fm = bm.cohort_metrics(filtered_rows)

    # Blind comparison.
    blind_rows = await _blind_metric_rows(conn)
    blindm = bm.cohort_metrics(blind_rows)
    payload = await active_payload(conn)
    min_sample = bm.min_benchmark_sample(payload)
    comparison = bm.compare(fm, blindm, min_sample=min_sample)

    # Active tracked wallets + copy candidates today.
    cur = await conn.execute(
        "SELECT COUNT(*) FROM wallet_profiles WHERE status = 'track' AND is_demo = 0"
    )
    tracked = int((await cur.fetchone())[0])
    cur = await conn.execute(
        "SELECT COUNT(*) FROM decision_journal WHERE decision = 'paper_copy' "
        "AND substr(created_at,1,10) = substr(?,1,10) AND is_demo = 0",
        (dbmod.utcnow_iso(),),
    )
    copy_candidates_today = int((await cur.fetchone())[0])

    # Latest rule change + alerts + top lesson + merged what-changed feed.
    rule_changes = await recent_rule_changes(conn, limit=5)
    alerts = await recent_alerts(conn, limit=5)
    top_lesson = await _top_lesson(conn)
    feed = await changes_feed(conn, limit=10)

    # Data freshness + report status.
    freshness = await data_freshness(conn)
    report_status = await _latest_report_status(conn)

    return {
        "portfolio_id": portfolio_id,
        "starting_bankroll_usd": starting,
        "equity_usd": equity,
        "cash_usd": cash,
        "total_pnl_usd": total_pnl,
        "today_pnl_usd": today_pnl,
        "max_drawdown_usd": max_drawdown,
        "open_positions": open_positions,
        "active_tracked_wallets": tracked,
        "copy_candidates_today": copy_candidates_today,
        "win_rate": ser.dec(fm["win_rate"]),
        "win_sample": fm["sample"],
        "active_rule_version": version,
        "comparison": _comparison_json(comparison),
        "recent_rule_changes": rule_changes,
        "changes_feed": feed,
        "alerts": alerts,
        "top_lesson": top_lesson,
        "data_freshness": freshness,
        "report_status": report_status,
        "last_pnl_snapshot_at": last_pnl_at,
    }


def _comparison_json(comparison: dict) -> dict:
    def cohort(c):
        return {
            "sample": c["sample"],
            "net_pnl_usd": ser.dec(c["net_pnl"]),
            "roi": ser.dec(c["roi"]),
            "win_rate": ser.dec(c["win_rate"]),
        }
    return {
        "verdict": comparison["verdict"],
        "filtered_minus_blind_pnl_usd": ser.dec(comparison["filtered_minus_blind_pnl"]),
        "sufficient_sample": comparison["sufficient_sample"],
        "min_sample": comparison["min_sample"],
        "filtered": cohort(comparison["filtered"]),
        "blind": cohort(comparison["blind"]),
        "caveat": None if comparison["sufficient_sample"]
        else "Insufficient sample: not enough resolved trades in one or both "
             "cohorts to claim an edge over blind copying.",
    }


async def active_payload(conn) -> dict:
    cur = await conn.execute(
        "SELECT parameters_json FROM rule_sets WHERE strategy = 'default' AND status = 'active'"
    )
    row = await cur.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


async def _resolved_metric_rows(conn, portfolio_id, cohort="filtered") -> list[dict]:
    cur = await conn.execute(
        "SELECT entry_cost, realized_pnl FROM paper_trades "
        "WHERE paper_portfolio_id = ? AND benchmark_cohort = ? AND is_admin = 0 AND is_demo = 0 "
        "AND status IN ('closed','resolved') AND realized_pnl IS NOT NULL",
        (portfolio_id, cohort),
    )
    out = []
    for cost, pnl in await cur.fetchall():
        realized = dbmod.micro_to_usd(int(pnl or 0))
        out.append({"cost": dbmod.micro_to_usd(int(cost or 0)),
                    "realized_pnl": realized, "won": realized > 0})
    return out


async def _blind_metric_rows(conn) -> list[dict]:
    cur = await conn.execute(
        "SELECT simulated_position_size, final_pnl FROM benchmark_trades "
        "WHERE cohort = 'blind' AND final_pnl IS NOT NULL AND is_demo = 0"
    )
    out = []
    for size, pnl in await cur.fetchall():
        realized = dbmod.micro_to_usd(int(pnl or 0))
        out.append({"cost": dbmod.micro_to_usd(int(size or 0)),
                    "realized_pnl": realized, "won": realized > 0})
    return out


async def _top_lesson(conn) -> str | None:
    cur = await conn.execute(
        "SELECT notes_json FROM outcome_reviews WHERE review_checkpoint = 'final' "
        "AND notes_json IS NOT NULL AND is_demo = 0 ORDER BY id DESC LIMIT 20"
    )
    for (notes,) in await cur.fetchall():
        data = ser.load_json(notes)
        if isinstance(data, dict) and data.get("lesson"):
            return str(data["lesson"])
    return None


async def _latest_report_status(conn) -> dict | None:
    cur = await conn.execute(
        "SELECT report_type, report_date, delivery_status FROM daily_reports "
        "WHERE report_type = 'daily' ORDER BY report_date DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {"report_type": row[0], "report_date": row[1], "delivery_status": row[2]}


async def data_freshness(conn) -> dict:
    """Last successful monitor + pnl job times, and any stale/partial signals."""
    out = {}
    for job in ("monitor", "pnl", "leaderboard_scan"):
        cur = await conn.execute(
            "SELECT finished_at FROM job_runs WHERE job_name = ? AND status = 'success' "
            "ORDER BY id DESC LIMIT 1",
            (job,),
        )
        row = await cur.fetchone()
        out[f"last_{job}_success_at"] = row[0] if row else None
    # Only categories whose LATEST scan is partial: a partial scan self-heals
    # on the next complete run and must not pin the warning banner forever.
    cur = await conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT category, MAX(id) AS max_id FROM leaderboard_scans GROUP BY source, category, time_period
        ) latest JOIN leaderboard_scans ls ON ls.id = latest.max_id
        WHERE ls.is_partial = 1
        """
    )
    out["partial_scans"] = int((await cur.fetchone())[0])
    cur = await conn.execute(
        "SELECT COUNT(*) FROM data_quality_events WHERE severity = 'critical' AND resolved_at IS NULL"
    )
    out["unresolved_critical_events"] = int((await cur.fetchone())[0])
    cur = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open' AND mark_is_stale = 1"
    )
    out["stale_marks"] = int((await cur.fetchone())[0])
    return out


# --- wallets (PRD 20.3) -----------------------------------------------------

async def wallets(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    where = ["wp.is_demo = 0"]
    args: list = []
    status = params.get("status")
    if status:
        where.append("wp.status = ?")
        args.append(status)
    min_score = params.get("min_score")
    if min_score not in (None, ""):
        where.append("wp.global_score >= ?")
        args.append(int(min_score))
    min_resolved = params.get("min_resolved")
    if min_resolved not in (None, ""):
        where.append("wp.resolved_trade_count >= ?")
        args.append(int(min_resolved))
    if params.get("copyable_only") in ("1", "true", "yes", "on"):
        where.append("wp.status = 'track'")
    if params.get("exclude_stale") in ("1", "true", "yes", "on"):
        where.append("wp.history_complete = 1")

    category = params.get("category")
    join = ""
    if category:
        join = "JOIN wallet_category_stats wcs ON wcs.wallet_address = wp.wallet_address AND wcs.category = ?"
        args = [category] + args  # category binds before the WHERE args on the joined table

    where_sql = " AND ".join(where)
    sort_key = params.get("sort", "paper_pnl")
    direction = "ASC" if params.get("dir") == "asc" else "DESC"

    count_sql = f"SELECT COUNT(*) FROM wallet_profiles wp {join} WHERE {where_sql}"
    cur = await conn.execute(count_sql, args)
    total = int((await cur.fetchone())[0])

    contrib = await _paper_contribution_map(conn)

    if sort_key == "paper_pnl":
        # Rank by copied paper-PnL contribution (computed, not a DB column).
        # ORDER BY score first: the python pnl sort is stable, so equal-pnl
        # wallets (e.g. all zero during early burn-in) fall back to score rank.
        list_sql = f"SELECT wp.* FROM wallet_profiles wp {join} WHERE {where_sql} ORDER BY wp.global_score DESC NULLS LAST"
        cur = await conn.execute(list_sql, args)
        all_rows = [dict(r) for r in await cur.fetchall()]

        def _pnl_key(r):
            c = contrib.get(r["wallet_address"])
            return c["paper_pnl_micro"] if c else 0
        all_rows.sort(key=_pnl_key, reverse=(direction == "DESC"))
        page = all_rows[offset:offset + limit]
        items = [await _wallet_row(conn, r, contrib) for r in page]
    else:
        sort_col = WALLET_SORTS.get(sort_key, "global_score")
        list_sql = (
            f"SELECT wp.* FROM wallet_profiles wp {join} WHERE {where_sql} "
            f"ORDER BY {sort_col} {direction} NULLS LAST LIMIT ? OFFSET ?"
        )
        cur = await conn.execute(list_sql, args + [limit, offset])
        items = [await _wallet_row(conn, dict(r), contrib) for r in await cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items,
            "sort": sort_key}


async def _paper_contribution_map(conn) -> dict:
    """Per source-wallet copied paper-PnL contribution (realized + unrealized).

    Attributes filtered-cohort, non-admin paper trades to the source wallet the
    copy came from. Also returns copy count, win count, and average entry
    slippage + last signal (copy) time.
    """
    cur = await conn.execute(
        """
        SELECT wallet_address,
               COALESCE(SUM(COALESCE(realized_pnl,0) + COALESCE(unrealized_pnl,0)), 0) AS paper_pnl,
               COUNT(*) AS copies,
               SUM(CASE WHEN status IN ('closed','resolved') AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN status IN ('closed','resolved') THEN 1 ELSE 0 END) AS resolved,
               AVG(entry_slippage) AS avg_slippage,
               MAX(opened_at) AS last_copy_at
          FROM paper_trades
         WHERE benchmark_cohort = 'filtered' AND is_admin = 0 AND is_demo = 0
           AND wallet_address IS NOT NULL
         GROUP BY wallet_address
        """,
    )
    out: dict[str, dict] = {}
    for r in await cur.fetchall():
        resolved = int(r[4] or 0)
        wins = int(r[3] or 0)
        out[r[0]] = {
            "paper_pnl_micro": int(r[1] or 0),
            "copies": int(r[2] or 0),
            "wins": wins,
            "resolved": resolved,
            "win_rate": (str((Decimal(wins) / Decimal(resolved)).quantize(
                Decimal("0.0001"))) if resolved else None),
            "avg_slippage_micro": int(r[5]) if r[5] is not None else None,
            "last_copy_at": r[6],
        }
    return out


async def _wallet_row(conn, wp: dict, contrib: dict | None = None) -> dict:
    # Best category (highest category_score).
    cur = await conn.execute(
        "SELECT category, category_score FROM wallet_category_stats "
        "WHERE wallet_address = ? ORDER BY category_score DESC NULLS LAST LIMIT 1",
        (wp["wallet_address"],),
    )
    best = await cur.fetchone()
    components = ser.load_json(wp.get("raw_json"))
    c = (contrib or {}).get(wp["wallet_address"]) if contrib is not None else None
    return {
        "wallet_address": wp["wallet_address"],
        "label": (components or {}).get("user_name") if components else None,
        "status": wp["status"],
        "status_reason_code": wp["status_reason_code"],
        "global_score": wp["global_score"],
        "data_quality_score": wp["data_quality_score"],
        "roi_30d": ser.ratio(wp["roi_30d"]),
        "win_rate": ser.ratio(wp["win_rate"]),
        "pnl_30d_usd": ser.money(wp["pnl_30d"]),
        "blind_copy_pnl_30d_usd": ser.money(wp["blind_copy_pnl_30d"]),
        "resolved_trade_count": wp["resolved_trade_count"],
        "trade_count": wp["trade_count"],
        "profit_concentration_top1": ser.ratio(wp["profit_concentration_top1"]),
        "profit_concentration_top3": ser.ratio(wp["profit_concentration_top3"]),
        "median_detection_delay_seconds": wp["median_detection_delay_seconds"],
        "executable_trade_ratio": ser.ratio(wp["executable_trade_ratio"]),
        "best_category": best[0] if best else None,
        "best_category_score": best[1] if best else None,
        "history_complete": ser.flag(wp.get("history_complete")),
        "last_profiled_at": wp.get("last_profiled_at"),
        "profile_version": wp["profile_version"],
        "score_components": (components or {}).get("score_components") if components else None,
        # Copied paper-PnL contribution (default wallet ranking; PRD refinement).
        "copied_paper_pnl_usd": ser.money(c["paper_pnl_micro"]) if c else "0",
        "copied_trade_count": c["copies"] if c else 0,
        "copy_win_rate": c["win_rate"] if c else None,
        "avg_entry_slippage": ser.ratio(c["avg_slippage_micro"]) if c and c["avg_slippage_micro"] is not None else None,
        "last_signal_at": c["last_copy_at"] if c else None,
        "is_demo": ser.flag(wp["is_demo"]),
    }


async def wallet_detail(conn, address: str) -> dict | None:
    cur = await conn.execute("SELECT * FROM wallet_profiles WHERE wallet_address = ?", (address,))
    row = await cur.fetchone()
    if row is None:
        return None
    contrib = await _paper_contribution_map(conn)
    base = await _wallet_row(conn, dict(row), contrib)

    cur = await conn.execute(
        "SELECT * FROM wallet_category_stats WHERE wallet_address = ? ORDER BY category_score DESC NULLS LAST",
        (address,),
    )
    cats = [
        {
            "category": c["category"],
            "trade_count": c["trade_count"],
            "resolved_trade_count": c["resolved_trade_count"],
            "pnl_usd": ser.money(c["pnl"]),
            "roi": ser.ratio(c["roi"]),
            "win_rate": ser.ratio(c["win_rate"]),
            "consistency_score": c["consistency_score"],
            "copyability_score": c["copyability_score"],
            "category_score": c["category_score"],
            "sample_quality_score": c["sample_quality_score"],
        }
        for c in await cur.fetchall()
    ]

    # Recent upgrades/downgrades (status changes from snapshots).
    cur = await conn.execute(
        "SELECT profile_version, status, global_score, status_reason_code, captured_at "
        "FROM wallet_profile_snapshots WHERE wallet_address = ? ORDER BY captured_at DESC LIMIT 10",
        (address,),
    )
    snapshots = [
        {"profile_version": s[0], "status": s[1], "global_score": s[2],
         "status_reason_code": s[3], "captured_at": s[4]}
        for s in await cur.fetchall()
    ]

    # Wallet's paper-copy performance.
    cur = await conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM paper_trades "
        "WHERE wallet_address = ? AND is_admin = 0 AND is_demo = 0 AND status IN ('closed','resolved')",
        (address,),
    )
    pc = await cur.fetchone()

    base["category_stats"] = cats
    base["snapshots"] = snapshots
    base["paper_copy_performance"] = {
        "resolved_trades": int(pc[0]),
        "realized_pnl_usd": ser.money(int(pc[1] or 0)),
    }
    base["blind_copy_pnl_30d_usd"] = ser.money(row["blind_copy_pnl_30d"])
    return base


async def wallet_trades(conn, address: str, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    cur = await conn.execute(
        "SELECT COUNT(*) FROM wallet_trades WHERE proxy_wallet = ?", (address,)
    )
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        "SELECT * FROM wallet_trades WHERE proxy_wallet = ? ORDER BY ts DESC LIMIT ? OFFSET ?",
        (address, limit, offset),
    )
    items = [
        {
            "condition_id": t["condition_id"],
            "asset_id": t["asset_id"],
            "transaction_hash": t["transaction_hash"],
            "side": t["side"],
            "outcome": t["outcome"],
            "price": ser.price(t["price_micro"]),
            "size": ser.money(t["size"]),
            "ts": t["ts"],
            "title": t["title"],
            "category": t["category"],
            "is_demo": ser.flag(t["is_demo"]),
        }
        for t in await cur.fetchall()
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items,
            "wallet_address": address}


# --- signals / decision journal (PRD 20.5 / 20.7) ---------------------------

async def _signal_row(conn, dj: dict, *, full: bool = False) -> dict:
    market = None
    if dj.get("market_id"):
        cur = await conn.execute(
            "SELECT question, event_title, category, slug FROM markets WHERE market_id = ?",
            (dj["market_id"],),
        )
        m = await cur.fetchone()
        if m:
            market = {"question": m[0], "event_title": m[1], "category": m[2], "slug": m[3]}
    outcome = None
    detection_delay = None
    if dj.get("observed_trade_id"):
        cur = await conn.execute(
            "SELECT outcome, detection_delay_seconds FROM observed_trades WHERE id = ?",
            (dj["observed_trade_id"],),
        )
        ot = await cur.fetchone()
        if ot:
            outcome = ot[0]
            detection_delay = ot[1]

    reasons = ser.load_json(dj.get("reasons_json"))
    risks = ser.load_json(dj.get("risks_json"))
    components = ser.load_json(dj.get("component_scores_json"))
    row = {
        "id": dj["id"],
        "wallet_address": dj["wallet_address"],
        "market": market,
        "outcome": outcome,
        "decision": dj["decision"],
        "total_score": dj["total_score"],
        "decision_reason_code": dj["decision_reason_code"],
        "source_entry_price": ser.price(dj["source_entry_price"]),
        "executable_entry_price": ser.price(dj["executable_entry_price"]),
        "price_move_absolute": ser.ratio(dj["price_move_absolute"]),
        "price_move_percent": ser.ratio(dj["price_move_percent"]),
        "detection_delay_seconds": detection_delay,
        "expected_position_usd": ser.money(dj["expected_position_usd"]),
        "rule_version": None,
        "component_scores": components,
        "top_reasons": (reasons[:3] if isinstance(reasons, list) else reasons),
        "top_risk": (risks[0] if isinstance(risks, list) and risks else None),
        "data_quality_score": dj["data_quality_score"],
        "market_data_timestamp": dj["market_data_timestamp"],
        "created_at": dj["created_at"],
        "is_demo": ser.flag(dj["is_demo"]),
    }
    if dj.get("rule_set_id"):
        cur = await conn.execute("SELECT version FROM rule_sets WHERE id = ?", (dj["rule_set_id"],))
        rr = await cur.fetchone()
        row["rule_version"] = rr[0] if rr else None
    if full:
        row["reasons"] = reasons
        row["risks"] = risks
        row["hard_gates"] = ser.load_json(dj.get("hard_gates_json"))
        row["portfolio_limit_result"] = dj.get("portfolio_limit_result")
        row["wallet_profile_version"] = dj.get("wallet_profile_version")
        row["market_snapshot_id"] = dj.get("market_snapshot_id")
    return row


async def signals(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    where = ["1=1"]
    args: list = []
    if params.get("decision"):
        where.append("decision = ?")
        args.append(params["decision"])
    if params.get("wallet"):
        where.append("wallet_address = ?")
        args.append(params["wallet"])
    if params.get("since"):
        where.append("created_at >= ?")
        args.append(params["since"])
    where_sql = " AND ".join(where)
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    sort_col = TRADE_SORTS.get(params.get("sort", "detected"), "created_at")
    sort_col = "created_at" if sort_col == "detected_at" else sort_col

    cur = await conn.execute(f"SELECT COUNT(*) FROM decision_journal WHERE {where_sql}", args)
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        f"SELECT * FROM decision_journal WHERE {where_sql} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    items = [await _signal_row(conn, dict(r)) for r in await cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def signal_detail(conn, signal_id: int) -> dict | None:
    cur = await conn.execute("SELECT * FROM decision_journal WHERE id = ?", (signal_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return await _signal_row(conn, dict(row), full=True)


async def journal(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    where = ["1=1"]
    args: list = []
    if params.get("decision"):
        where.append("dj.decision = ?")
        args.append(params["decision"])
    if params.get("wallet"):
        where.append("dj.wallet_address = ?")
        args.append(params["wallet"])
    label = params.get("label")
    if label:
        where.append(
            "EXISTS (SELECT 1 FROM outcome_reviews orv WHERE orv.decision_journal_id = dj.id "
            "AND orv.review_checkpoint = 'final' AND orv.decision_quality_label = ?)"
        )
        args.append(label)
    if params.get("since"):
        where.append("dj.created_at >= ?")
        args.append(params["since"])
    where_sql = " AND ".join(where)
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    sort_col = JOURNAL_SORTS.get(params.get("sort", "created"), "created_at")

    cur = await conn.execute(f"SELECT COUNT(*) FROM decision_journal dj WHERE {where_sql}", args)
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        f"SELECT dj.* FROM decision_journal dj WHERE {where_sql} "
        f"ORDER BY dj.{sort_col} {direction} LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    items = []
    for r in await cur.fetchall():
        entry = await _signal_row(conn, dict(r), full=True)
        entry["reviews"] = await _reviews_for(conn, r["id"])
        items.append(entry)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def _reviews_for(conn, decision_journal_id: int) -> list[dict]:
    order = "CASE review_checkpoint WHEN '1h' THEN 1 WHEN '6h' THEN 2 WHEN '24h' THEN 3 ELSE 4 END"
    cur = await conn.execute(
        f"SELECT review_checkpoint, price_at_checkpoint, hypothetical_pnl, actual_pnl, "
        f"decision_quality_label, eligible_for_learning, notes_json, reviewed_at "
        f"FROM outcome_reviews WHERE decision_journal_id = ? ORDER BY {order}",
        (decision_journal_id,),
    )
    out = []
    for rv in await cur.fetchall():
        notes = ser.load_json(rv[6])
        out.append({
            "checkpoint": rv[0],
            "price_at_checkpoint": ser.price(rv[1]),
            "hypothetical_pnl_usd": ser.money(rv[2]),
            "actual_pnl_usd": ser.money(rv[3]),
            "decision_quality_label": rv[4],
            "eligible_for_learning": ser.flag(rv[5]),
            "lesson": (notes or {}).get("lesson") if isinstance(notes, dict) else None,
            "reviewed_at": rv[7],
        })
    return out


# --- paper trades (PRD 20.6) ------------------------------------------------

async def paper_trades(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    where = ["1=1"]
    args: list = []
    if params.get("status"):
        where.append("status = ?")
        args.append(params["status"])
    if params.get("cohort"):
        where.append("benchmark_cohort = ?")
        args.append(params["cohort"])
    if params.get("wallet"):
        where.append("wallet_address = ?")
        args.append(params["wallet"])
    where_sql = " AND ".join(where)
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    sort_col = PAPER_SORTS.get(params.get("sort", "opened"), "opened_at")

    cur = await conn.execute(f"SELECT COUNT(*) FROM paper_trades WHERE {where_sql}", args)
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        f"SELECT * FROM paper_trades WHERE {where_sql} ORDER BY {sort_col} {direction} NULLS LAST LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    items = [_paper_row(dict(r)) for r in await cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _paper_row(t: dict) -> dict:
    return {
        "id": t["id"],
        "portfolio_id": t["paper_portfolio_id"],
        "cohort": t["benchmark_cohort"],
        "wallet_address": t["wallet_address"],
        "market_id": t["market_id"],
        "outcome": t["outcome"],
        "status": t["status"],
        "shares": ser.money(t["shares"]),
        "entry_price": ser.price(t["entry_price"]),
        "entry_best_ask": ser.price(t["entry_best_ask"]),
        "entry_slippage": ser.ratio(t["entry_slippage"]),
        "entry_cost_usd": ser.money(t["entry_cost"]),
        "current_mark": ser.price(t["current_mark"]),
        "mark_is_stale": ser.flag(t["mark_is_stale"]),
        "unrealized_pnl_usd": ser.money(t["unrealized_pnl"]),
        "realized_pnl_usd": ser.money(t["realized_pnl"]),
        "exit_price": ser.price(t["exit_price"]),
        "exit_reason": t["exit_reason"],
        "is_admin": ser.flag(t["is_admin"]),
        "decision_journal_id": t["decision_journal_id"],
        "observed_trade_id": t["observed_trade_id"],
        "opened_at": t["opened_at"],
        "closed_at": t["closed_at"],
        "is_demo": ser.flag(t["is_demo"]),
        "pnl_kind": "paper",
    }


async def paper_trade_detail(conn, trade_id: int) -> dict | None:
    cur = await conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    base = _paper_row(dict(row))
    cur = await conn.execute(
        "SELECT entry_type, amount, balance_after, created_at, metadata_json "
        "FROM paper_ledger WHERE paper_trade_id = ? ORDER BY id ASC",
        (trade_id,),
    )
    base["ledger"] = [
        {"entry_type": e[0], "amount_usd": ser.money(e[1]),
         "balance_after_usd": ser.money(e[2]), "created_at": e[3],
         "metadata": ser.load_json(e[4])}
        for e in await cur.fetchall()
    ]
    # Hourly marks history: source entry comparison + hourly pnl snapshots at market level.
    if row["decision_journal_id"]:
        cur = await conn.execute(
            "SELECT source_entry_price FROM decision_journal WHERE id = ?",
            (row["decision_journal_id"],),
        )
        dj = await cur.fetchone()
        base["source_entry_price"] = ser.price(dj[0]) if dj else None
    cur = await conn.execute(
        "SELECT collected_at, best_ask, midpoint FROM market_snapshots "
        "WHERE asset_id = ? ORDER BY collected_at DESC LIMIT 48",
        (row["asset_id"],),
    )
    base["hourly_marks"] = [
        {"collected_at": h[0], "best_ask": ser.price(h[1]), "midpoint": ser.price(h[2])}
        for h in await cur.fetchall()
    ][::-1]
    return base


# --- performance (PRD 20.8) -------------------------------------------------

async def performance(conn, window: str | None = None) -> dict:
    from .jobs.portfolio_view import get_active_portfolio_id

    portfolio_id = await get_active_portfolio_id(conn)
    since = _window_since(window)
    filtered_rows = await _resolved_metric_rows(conn, portfolio_id) if portfolio_id else []
    fm = bm.cohort_metrics(filtered_rows)
    blind_rows = await _blind_metric_rows(conn)
    blindm = bm.cohort_metrics(blind_rows)
    payload = await active_payload(conn)
    comparison = bm.compare(fm, blindm, min_sample=bm.min_benchmark_sample(payload))

    equity_series = await equity_sparkline(conn, portfolio_id, since=since)

    # Category + wallet + rule-version performance breakdowns.
    category_perf = await _category_performance(conn, portfolio_id)
    wallet_perf = await _wallet_performance(conn, portfolio_id)
    rule_perf = await _rule_version_performance(conn, portfolio_id)
    labels = await _label_counts(conn)

    return {
        "window": window or "all",
        "filtered": _metrics_json(fm),
        "blind": _metrics_json(blindm),
        "comparison": _comparison_json(comparison),
        "equity_series": equity_series,
        "category_performance": category_perf,
        "wallet_performance": wallet_perf,
        "rule_version_performance": rule_perf,
        "decision_quality_labels": labels,
    }


def _metrics_json(m: dict) -> dict:
    return {
        "sample": m["sample"],
        "net_pnl_usd": ser.dec(m["net_pnl"]),
        "roi": ser.dec(m["roi"]),
        "win_rate": ser.dec(m["win_rate"]),
        "avg_pnl_usd": ser.dec(m["avg_pnl"]),
        "profit_factor": ser.dec(m["profit_factor"]) if m["profit_factor"] is not None else None,
        "max_drawdown_usd": ser.dec(m["max_drawdown"]),
        "wins": m["wins"],
        "losses": m["losses"],
        "capital_deployed_usd": ser.dec(m["capital_deployed"]),
        "pnl_kind": "paper",
    }


def _window_since(window: str | None) -> str | None:
    if not window or window == "all":
        return None
    import re
    from datetime import datetime, timedelta, timezone
    match = re.match(r"^(\d+)([dhw])$", window)
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


async def equity_sparkline(conn, portfolio_id, *, since=None, limit=200) -> list[dict]:
    if portfolio_id is None:
        return []
    args = [portfolio_id]
    where = "paper_portfolio_id = ?"
    if since:
        where += " AND collected_at >= ?"
        args.append(since)
    cur = await conn.execute(
        f"SELECT collected_at, equity, drawdown FROM pnl_snapshots WHERE {where} "
        f"ORDER BY collected_at ASC LIMIT ?",
        args + [limit],
    )
    return [
        {"collected_at": r[0], "equity_usd": ser.money(r[1]),
         "drawdown_usd": ser.money(r[2]) if r[2] is not None else None}
        for r in await cur.fetchall()
    ]


async def _category_performance(conn, portfolio_id) -> list[dict]:
    if portfolio_id is None:
        return []
    cur = await conn.execute(
        "SELECT COALESCE(m.category,'unknown') cat, COUNT(*), COALESCE(SUM(pt.realized_pnl),0) "
        "FROM paper_trades pt LEFT JOIN markets m ON m.market_id = pt.market_id "
        "WHERE pt.paper_portfolio_id = ? AND pt.is_admin = 0 AND pt.is_demo = 0 "
        "AND pt.status IN ('closed','resolved') AND pt.realized_pnl IS NOT NULL "
        "GROUP BY cat ORDER BY 3 DESC",
        (portfolio_id,),
    )
    return [{"category": r[0], "trades": int(r[1]), "realized_pnl_usd": ser.money(int(r[2] or 0))}
            for r in await cur.fetchall()]


async def _wallet_performance(conn, portfolio_id) -> list[dict]:
    if portfolio_id is None:
        return []
    cur = await conn.execute(
        "SELECT wallet_address, COUNT(*), COALESCE(SUM(realized_pnl),0) FROM paper_trades "
        "WHERE paper_portfolio_id = ? AND is_admin = 0 AND is_demo = 0 "
        "AND status IN ('closed','resolved') AND realized_pnl IS NOT NULL "
        "GROUP BY wallet_address ORDER BY 3 DESC LIMIT 25",
        (portfolio_id,),
    )
    return [{"wallet_address": r[0], "trades": int(r[1]), "realized_pnl_usd": ser.money(int(r[2] or 0))}
            for r in await cur.fetchall()]


async def _rule_version_performance(conn, portfolio_id) -> list[dict]:
    if portfolio_id is None:
        return []
    cur = await conn.execute(
        "SELECT rs.version, COUNT(*), COALESCE(SUM(pt.realized_pnl),0) FROM paper_trades pt "
        "LEFT JOIN rule_sets rs ON rs.id = pt.rule_set_id "
        "WHERE pt.paper_portfolio_id = ? AND pt.is_admin = 0 AND pt.is_demo = 0 "
        "AND pt.status IN ('closed','resolved') AND pt.realized_pnl IS NOT NULL "
        "GROUP BY rs.version ORDER BY rs.version DESC",
        (portfolio_id,),
    )
    return [{"rule_version": r[0], "trades": int(r[1]), "realized_pnl_usd": ser.money(int(r[2] or 0))}
            for r in await cur.fetchall()]


async def _label_counts(conn) -> dict:
    """Decision-quality label counts (missed winners, avoided losers, etc.)."""
    cur = await conn.execute(
        "SELECT decision_quality_label, COUNT(*) FROM outcome_reviews "
        "WHERE review_checkpoint = 'final' AND is_demo = 0 AND decision_quality_label IS NOT NULL "
        "GROUP BY decision_quality_label",
    )
    return {r[0]: int(r[1]) for r in await cur.fetchall()}


async def benchmarks(conn) -> dict:
    from .jobs.portfolio_view import get_active_portfolio_id

    portfolio_id = await get_active_portfolio_id(conn)
    filtered_rows = await _resolved_metric_rows(conn, portfolio_id) if portfolio_id else []
    fm = bm.cohort_metrics(filtered_rows)
    blind_rows = await _blind_metric_rows(conn)
    blindm = bm.cohort_metrics(blind_rows)
    payload = await active_payload(conn)
    comparison = bm.compare(fm, blindm, min_sample=bm.min_benchmark_sample(payload))
    return {"filtered": _metrics_json(fm), "blind": _metrics_json(blindm),
            "comparison": _comparison_json(comparison)}


# --- rules (PRD 20.9) -------------------------------------------------------

async def rules(conn) -> dict:
    cur = await conn.execute(
        "SELECT id, version, status, parameters_json, checksum, activated_at, "
        "deactivated_at, created_at FROM rule_sets WHERE strategy = 'default' ORDER BY version DESC"
    )
    versions = []
    active = None
    for r in await cur.fetchall():
        entry = {
            "id": r[0], "version": r[1], "status": r[2],
            "checksum": r[4], "activated_at": r[5],
            "deactivated_at": r[6], "created_at": r[7],
        }
        if r[2] == "active":
            active = {**entry, "parameters": ser.load_json(r[3])}
        versions.append(entry)
    changes = await recent_rule_changes(conn, limit=50)
    return {"active": active, "versions": versions, "changes": changes}


async def rule_version(conn, version: int) -> dict | None:
    cur = await conn.execute(
        "SELECT id, version, status, parameters_json, checksum, parent_rule_set_id, "
        "activated_at, deactivated_at, created_at FROM rule_sets "
        "WHERE strategy = 'default' AND version = ?",
        (version,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    cur = await conn.execute(
        "SELECT parameter_family, parameter_path, old_value_json, new_value_json, "
        "sample_size, target_metric, baseline_value, expected_value, outcome_status, "
        "evaluated_at, created_at FROM rule_changes WHERE rule_set_id = ? ORDER BY id",
        (r[0],),
    )
    changes = [
        {
            "parameter_family": c[0], "parameter_path": c[1],
            "old_value": ser.load_json(c[2]), "new_value": ser.load_json(c[3]),
            "sample_size": c[4], "target_metric": c[5],
            "baseline_value": c[6], "expected_value": c[7],
            "outcome_status": c[8], "evaluated_at": c[9], "created_at": c[10],
        }
        for c in await cur.fetchall()
    ]
    return {
        "id": r[0], "version": r[1], "status": r[2],
        "parameters": ser.load_json(r[3]), "checksum": r[4],
        "parent_rule_set_id": r[5], "activated_at": r[6],
        "deactivated_at": r[7], "created_at": r[8], "changes": changes,
    }


async def recent_rule_changes(conn, *, limit=20) -> list[dict]:
    cur = await conn.execute(
        "SELECT rc.id, rs.version, rc.parameter_family, rc.parameter_path, "
        "rc.old_value_json, rc.new_value_json, rc.outcome_status, rc.created_at "
        "FROM rule_changes rc LEFT JOIN rule_sets rs ON rs.id = rc.rule_set_id "
        "ORDER BY rc.id DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "id": r[0], "rule_version": r[1], "parameter_family": r[2],
            "parameter_path": r[3], "old_value": ser.load_json(r[4]),
            "new_value": ser.load_json(r[5]), "outcome_status": r[6], "created_at": r[7],
        }
        for r in await cur.fetchall()
    ]


# --- reports (PRD 20.10) ----------------------------------------------------

async def reports(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=30)
    report_type = params.get("type")
    where = ["1=1"]
    args: list = []
    if report_type:
        where.append("report_type = ?")
        args.append(report_type)
    where_sql = " AND ".join(where)
    cur = await conn.execute(f"SELECT COUNT(*) FROM daily_reports WHERE {where_sql}", args)
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        f"SELECT id, report_type, report_date, strategy_version, filtered_pnl, "
        f"blind_copy_pnl, filtered_minus_blind_pnl, max_drawdown, delivery_status, created_at "
        f"FROM daily_reports WHERE {where_sql} ORDER BY report_date DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    items = [_report_summary(r) for r in await cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _report_summary(r) -> dict:
    return {
        "id": r[0], "report_type": r[1], "report_date": r[2],
        "strategy_version": r[3],
        "filtered_pnl_usd": ser.money(r[4]),
        "blind_copy_pnl_usd": ser.money(r[5]),
        "filtered_minus_blind_pnl_usd": ser.money(r[6]),
        "max_drawdown_usd": ser.money(r[7]),
        "delivery_status": r[8], "created_at": r[9],
    }


async def report_detail(conn, report_date: str, *, report_type="daily") -> dict | None:
    cur = await conn.execute(
        "SELECT id, report_type, report_date, strategy_version, filtered_pnl, "
        "blind_copy_pnl, filtered_minus_blind_pnl, max_drawdown, summary_json, "
        "data_health_json, delivery_status, created_at FROM daily_reports "
        "WHERE report_date = ? AND report_type = ?",
        (report_date, report_type),
    )
    r = await cur.fetchone()
    if r is None:
        # Fall back to any type on that date.
        cur = await conn.execute(
            "SELECT id, report_type, report_date, strategy_version, filtered_pnl, "
            "blind_copy_pnl, filtered_minus_blind_pnl, max_drawdown, summary_json, "
            "data_health_json, delivery_status, created_at FROM daily_reports "
            "WHERE report_date = ? ORDER BY report_type LIMIT 1",
            (report_date,),
        )
        r = await cur.fetchone()
        if r is None:
            return None
    base = _report_summary(r)
    base["summary"] = ser.load_json(r[8])
    base["data_health"] = ser.load_json(r[9])
    return base


# --- job runs (PRD 20.11) ---------------------------------------------------

async def job_runs(conn, params) -> dict:
    limit, offset = _paginate(params.get("limit"), params.get("offset"), default_limit=50)
    where = ["1=1"]
    args: list = []
    if params.get("job_name"):
        where.append("job_name = ?")
        args.append(params["job_name"])
    if params.get("status"):
        where.append("status = ?")
        args.append(params["status"])
    where_sql = " AND ".join(where)
    cur = await conn.execute(f"SELECT COUNT(*) FROM job_runs WHERE {where_sql}", args)
    total = int((await cur.fetchone())[0])
    cur = await conn.execute(
        f"SELECT id, job_name, trigger_type, started_at, finished_at, status, "
        f"records_read, records_written, records_skipped, retry_count, error_json, metadata_json "
        f"FROM job_runs WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    items = [
        {
            "id": r[0], "job_name": r[1], "trigger_type": r[2],
            "started_at": r[3], "finished_at": r[4], "status": r[5],
            "records_read": r[6], "records_written": r[7], "records_skipped": r[8],
            "retry_count": r[9], "error": ser.load_json(r[10]),
            "metadata": ser.load_json(r[11]),
        }
        for r in await cur.fetchall()
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def wallet_status_transitions(conn, *, limit=20) -> list[dict]:
    """Derive status transitions from consecutive wallet_profile_snapshots rows.

    A transition is a snapshot whose status differs from the prior snapshot for
    the same wallet (ordered by captured_at). Uses LAG; no extra writes needed.
    """
    cur = await conn.execute(
        """
        WITH ordered AS (
            SELECT wallet_address, status, status_reason_code, global_score, captured_at,
                   LAG(status) OVER (PARTITION BY wallet_address ORDER BY captured_at) AS prev_status
              FROM wallet_profile_snapshots
             WHERE is_demo = 0
        )
        SELECT wallet_address, prev_status, status, status_reason_code, global_score, captured_at
          FROM ordered
         WHERE prev_status IS NOT NULL AND prev_status != status
         ORDER BY captured_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "kind": "wallet_status",
            "wallet_address": r[0],
            "from": r[1],
            "to": r[2],
            "status_reason_code": r[3],
            "global_score": r[4],
            "at": r[5],
        }
        for r in await cur.fetchall()
    ]


async def changes_feed(conn, *, limit=10) -> list[dict]:
    """Merged, time-ordered feed of rule_changes + wallet status transitions."""
    rule_changes = await recent_rule_changes(conn, limit=limit)
    for rc in rule_changes:
        rc["kind"] = "rule_change"
        rc["at"] = rc["created_at"]
    transitions = await wallet_status_transitions(conn, limit=limit)
    merged = rule_changes + transitions
    merged.sort(key=lambda e: e.get("at") or "", reverse=True)
    return merged[:limit]


async def recent_alerts(conn, *, limit=20) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, type, severity, message, delivery_status, created_at FROM alerts "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        {"id": r[0], "type": r[1], "severity": r[2], "message": r[3],
         "delivery_status": r[4], "created_at": r[5]}
        for r in await cur.fetchall()
    ]


async def health_ops(conn) -> dict:
    """Operational health view (PRD 20.11)."""
    freshness = await data_freshness(conn)
    cur = await conn.execute(
        "SELECT job_name, MAX(id) FROM job_runs WHERE status = 'success' GROUP BY job_name"
    )
    last_success = {}
    for name, run_id in await cur.fetchall():
        c2 = await conn.execute("SELECT finished_at FROM job_runs WHERE id = ?", (run_id,))
        last_success[name] = (await c2.fetchone())[0]
    cur = await conn.execute(
        "SELECT id, job_name, started_at, error_json FROM job_runs WHERE status = 'error' "
        "ORDER BY id DESC LIMIT 20"
    )
    failed = [
        {"id": r[0], "job_name": r[1], "started_at": r[2], "error": ser.load_json(r[3])}
        for r in await cur.fetchall()
    ]
    cur = await conn.execute(
        "SELECT COUNT(*) FROM wallet_profiles WHERE status = 'track' AND is_demo = 0 "
        "AND (next_profile_due_at IS NOT NULL AND next_profile_due_at < ?)",
        (dbmod.utcnow_iso(),),
    )
    stale_profiles = int((await cur.fetchone())[0])
    cur = await conn.execute(
        "SELECT severity, COUNT(*) FROM data_quality_events WHERE resolved_at IS NULL GROUP BY severity"
    )
    open_dq = {r[0]: int(r[1]) for r in await cur.fetchall()}
    # Missing outcome reviews: due final reviews not yet recorded (approx: closed
    # trades without a final review).
    cur = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades pt WHERE pt.status IN ('closed','resolved') "
        "AND pt.decision_journal_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM outcome_reviews orv WHERE orv.decision_journal_id = pt.decision_journal_id "
        "AND orv.review_checkpoint = 'final')"
    )
    missing_reviews = int((await cur.fetchone())[0])
    return {
        "data_freshness": freshness,
        "last_successful_jobs": last_success,
        "failed_jobs": failed,
        "stale_profiles": stale_profiles,
        "open_data_quality_events": open_dq,
        "missing_final_reviews": missing_reviews,
        "websocket_status": "disabled_v1",
        "external_api_hosts": ["gamma-api.polymarket.com", "data-api.polymarket.com",
                               "clob.polymarket.com"],
    }
