"""Daily + weekly reports (PRD sec 22) — dashboard-only delivery.

Delivery is dashboard-only: reports are composed and STORED in daily_reports;
there is no Telegram/network send. Each stored row carries
``delivery_status='dashboard'`` and ``telegram_message_id=NULL``. Weekly reports
share the same table, discriminated by the ``report_type`` column (migration
0004); a weekly row's ``report_date`` is the week's Sunday date so it never
collides with the daily rows' UNIQUE(report_date).

compose_daily/compose_weekly are read-only over a seeded DB and return plain
dicts (Decimals for money) that the report writers persist and the Wave-4
dashboard can render. All strategy numbers (e.g. drawdown limit, min benchmark
sample) come from the ACTIVE rule-set payload, never constants.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite

from ..config import Config
from ..db import (
    active_rule_set_version,
    json_dumps,
    json_or_none,
    micro_to_usd,
    usd_to_micro,
    utcnow_iso,
)
from ..domain import benchmarks as bm
from .alerts import alert
from .portfolio_view import get_active_portfolio_id
from .runner import JobContext

ZERO = Decimal(0)
DEFAULT_DRAWDOWN_LIMIT = Decimal("0.25")  # fraction of bankroll (PRD 30 / 21.2)

# The verbatim caveat surfaced when a filtered-vs-blind comparison lacks sample.
INSUFFICIENT_SAMPLE_CAVEAT = (
    "Insufficient sample: not enough resolved trades in one or both cohorts to "
    "claim an edge over blind copying."
)


# --- helpers ----------------------------------------------------------------

async def _active_payload(conn: aiosqlite.Connection, strategy: str = "default") -> dict:
    cur = await conn.execute(
        "SELECT parameters_json FROM rule_sets WHERE strategy = ? AND status = 'active'",
        (strategy,),
    )
    row = await cur.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _day_bounds(day: str) -> tuple[str, str]:
    """[start, end) ISO bounds for a YYYY-MM-DD day in UTC."""
    return f"{day}T00:00:00.000000Z", f"{day}T23:59:59.999999Z"


async def _resolved_trades(
    conn: aiosqlite.Connection, portfolio_id: int, cohort: str = "filtered"
) -> list[dict]:
    """Resolved/closed non-admin paper trades as benchmark metric rows."""
    cur = await conn.execute(
        """
        SELECT entry_cost, realized_pnl
          FROM paper_trades
         WHERE paper_portfolio_id = ? AND benchmark_cohort = ?
           AND is_admin = 0 AND is_demo = 0
           AND status IN ('closed', 'resolved') AND realized_pnl IS NOT NULL
        """,
        (portfolio_id, cohort),
    )
    out: list[dict] = []
    for cost_micro, realized_micro in await cur.fetchall():
        realized = micro_to_usd(int(realized_micro or 0))
        out.append({
            "cost": micro_to_usd(int(cost_micro or 0)),
            "realized_pnl": realized,
            "won": realized > 0,
        })
    return out


async def _blind_trades(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        """
        SELECT simulated_position_size, final_pnl
          FROM benchmark_trades
         WHERE cohort = 'blind' AND final_pnl IS NOT NULL AND is_demo = 0
        """,
    )
    out: list[dict] = []
    for size_micro, pnl_micro in await cur.fetchall():
        realized = micro_to_usd(int(pnl_micro or 0))
        out.append({
            "cost": micro_to_usd(int(size_micro or 0)),
            "realized_pnl": realized,
            "won": realized > 0,
        })
    return out


# --- daily composition (PRD 22.1) -------------------------------------------

async def compose_daily(conn: aiosqlite.Connection, config: Config, day: str) -> dict:
    """Compose the day's report as a plain dict (Decimals for money).

    ``day`` is a YYYY-MM-DD string in the report timezone's calendar but the DB
    stores UTC; callers pass the UTC calendar day for the report window (the
    scheduler fires near the local cutoff, close enough for a research report).
    """
    start, end = _day_bounds(day)
    payload = await _active_payload(conn)
    strategy_version = await active_rule_set_version(conn)
    portfolio_id = await get_active_portfolio_id(conn)

    equity = cash = ZERO
    pnl_today = total_pnl = ZERO
    open_positions = 0
    max_dd = ZERO

    if portfolio_id is not None:
        cur = await conn.execute(
            "SELECT cash_balance, starting_bankroll FROM paper_portfolios WHERE id = ?",
            (portfolio_id,),
        )
        prow = await cur.fetchone()
        cash = micro_to_usd(int(prow[0]))
        bankroll = micro_to_usd(int(prow[1]))

        # Latest pnl snapshot for equity + drawdown (most recent on/before end).
        cur = await conn.execute(
            """
            SELECT equity, drawdown FROM pnl_snapshots
             WHERE paper_portfolio_id = ? AND collected_at <= ?
             ORDER BY collected_at DESC LIMIT 1
            """,
            (portfolio_id, end),
        )
        srow = await cur.fetchone()
        if srow is not None:
            equity = micro_to_usd(int(srow[0] or 0))
            max_dd = micro_to_usd(int(srow[1] or 0))
        else:
            equity = cash

        cur = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE paper_portfolio_id = ? AND status = 'open'",
            (portfolio_id,),
        )
        open_positions = int((await cur.fetchone())[0])

        # Realized pnl today (trades closed/resolved today) and total.
        cur = await conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_trades
             WHERE paper_portfolio_id = ? AND is_admin = 0 AND is_demo = 0
               AND status IN ('closed', 'resolved') AND closed_at >= ? AND closed_at <= ?
            """,
            (portfolio_id, start, end),
        )
        pnl_today = micro_to_usd(int((await cur.fetchone())[0] or 0))
        cur = await conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_trades
             WHERE paper_portfolio_id = ? AND is_admin = 0 AND is_demo = 0
               AND status IN ('closed', 'resolved')
            """,
            (portfolio_id,),
        )
        total_pnl = micro_to_usd(int((await cur.fetchone())[0] or 0))

    # Win rate over all resolved (with sample n).
    filtered_rows = await _resolved_trades(conn, portfolio_id) if portfolio_id else []
    filtered_metrics = bm.cohort_metrics(filtered_rows)
    win_rate = filtered_metrics["win_rate"]
    win_sample = filtered_metrics["sample"]

    # Best / worst paper trade closed today.
    best_trade, worst_trade = await _best_worst_paper_trade(conn, portfolio_id, start, end)

    # Best / worst wallet today (by summed hypothetical/realized on final reviews).
    best_wallet, worst_wallet = await _best_worst_wallet(conn, start, end)

    # Decision counts today.
    counts = await _decision_counts(conn, start, end)

    # Missed winners / good skips today (from final reviews created today).
    label_counts = await _label_counts(conn, start, end)

    # Filtered vs blind comparison.
    blind_rows = await _blind_trades(conn)
    blind_metrics = bm.cohort_metrics(blind_rows)
    min_sample = bm.min_benchmark_sample(payload)
    comparison = bm.compare(filtered_metrics, blind_metrics, min_sample=min_sample)
    if not comparison["sufficient_sample"]:
        comparison["caveat"] = INSUFFICIENT_SAMPLE_CAVEAT

    # Rule changes today.
    rule_changes = await _rule_changes_today(conn, start, end)

    # Top lesson: highest |hypothetical_pnl| eligible final review today.
    top_lesson = await _top_lesson(conn, start, end)

    # Data health: open dq events by severity + job failures today.
    data_health = await _data_health(conn, start, end)

    return {
        "report_date": day,
        "strategy_version": strategy_version,
        "pnl_today": pnl_today,
        "total_pnl": total_pnl,
        "equity": equity,
        "cash": cash,
        "win_rate": win_rate,
        "win_sample": win_sample,
        "max_drawdown": max_dd,
        "open_positions": open_positions,
        "best_paper_trade": best_trade,
        "worst_paper_trade": worst_trade,
        "best_wallet": best_wallet,
        "worst_wallet": worst_wallet,
        "copies": counts["paper_copy"],
        "watches": counts["watchlist"],
        "skips": counts["skip"],
        "missed_winners": label_counts.get("missed_winner", 0),
        "avoided_losers": label_counts.get("good_skip", 0),
        "comparison": comparison,
        "filtered_pnl": filtered_metrics["net_pnl"],
        "blind_pnl": blind_metrics["net_pnl"],
        "filtered_minus_blind_pnl": comparison["filtered_minus_blind_pnl"],
        "rule_changes": rule_changes,
        "top_lesson": top_lesson,
        "data_health": data_health,
    }


async def _best_worst_paper_trade(conn, portfolio_id, start, end):
    if portfolio_id is None:
        return None, None
    cur = await conn.execute(
        """
        SELECT pt.id, pt.realized_pnl, m.question
          FROM paper_trades pt
     LEFT JOIN markets m ON m.market_id = pt.market_id
         WHERE pt.paper_portfolio_id = ? AND pt.is_admin = 0 AND pt.is_demo = 0
           AND pt.status IN ('closed', 'resolved') AND pt.realized_pnl IS NOT NULL
           AND pt.closed_at >= ? AND pt.closed_at <= ?
         ORDER BY pt.realized_pnl DESC
        """,
        (portfolio_id, start, end),
    )
    rows = await cur.fetchall()
    if not rows:
        return None, None
    best = {"trade_id": int(rows[0][0]), "realized_pnl": micro_to_usd(int(rows[0][1])),
            "question": rows[0][2]}
    worst = {"trade_id": int(rows[-1][0]), "realized_pnl": micro_to_usd(int(rows[-1][1])),
             "question": rows[-1][2]}
    return best, worst


async def _best_worst_wallet(conn, start, end):
    cur = await conn.execute(
        """
        SELECT dj.wallet_address, COALESCE(SUM(orv.hypothetical_pnl), 0) AS total
          FROM outcome_reviews orv
          JOIN decision_journal dj ON dj.id = orv.decision_journal_id
         WHERE orv.review_checkpoint = 'final' AND orv.is_demo = 0
           AND orv.created_at >= ? AND orv.created_at <= ?
         GROUP BY dj.wallet_address
         ORDER BY total DESC
        """,
        (start, end),
    )
    rows = await cur.fetchall()
    if not rows:
        return None, None
    best = {"wallet": rows[0][0], "pnl": micro_to_usd(int(rows[0][1]))}
    worst = {"wallet": rows[-1][0], "pnl": micro_to_usd(int(rows[-1][1]))}
    return best, worst


async def _decision_counts(conn, start, end) -> dict:
    cur = await conn.execute(
        """
        SELECT decision, COUNT(*) FROM decision_journal
         WHERE is_demo = 0 AND created_at >= ? AND created_at <= ?
         GROUP BY decision
        """,
        (start, end),
    )
    counts = {"paper_copy": 0, "watchlist": 0, "skip": 0}
    for decision, n in await cur.fetchall():
        counts[decision] = int(n)
    return counts


async def _label_counts(conn, start, end) -> dict:
    cur = await conn.execute(
        """
        SELECT decision_quality_label, COUNT(*)
          FROM outcome_reviews
         WHERE review_checkpoint = 'final' AND is_demo = 0
           AND created_at >= ? AND created_at <= ?
         GROUP BY decision_quality_label
        """,
        (start, end),
    )
    return {label: int(n) for label, n in await cur.fetchall() if label}


async def _rule_changes_today(conn, start, end) -> list[dict]:
    cur = await conn.execute(
        """
        SELECT parameter_family, old_value_json, new_value_json, outcome_status, target_metric
          FROM rule_changes
         WHERE created_at >= ? AND created_at <= ?
         ORDER BY id
        """,
        (start, end),
    )
    out = []
    for family, old_json, new_json, status, metric in await cur.fetchall():
        out.append({
            "family": family,
            "old_value": json.loads(old_json) if old_json else None,
            "new_value": json.loads(new_json) if new_json else None,
            "outcome_status": status,
            "target_metric": metric,
        })
    return out


async def _top_lesson(conn, start, end) -> dict | None:
    cur = await conn.execute(
        """
        SELECT orv.decision_quality_label, orv.hypothetical_pnl, m.question, dj.wallet_address
          FROM outcome_reviews orv
          JOIN decision_journal dj ON dj.id = orv.decision_journal_id
     LEFT JOIN markets m ON m.market_id = dj.market_id
         WHERE orv.review_checkpoint = 'final' AND orv.eligible_for_learning = 1
           AND orv.is_demo = 0 AND orv.hypothetical_pnl IS NOT NULL
           AND orv.created_at >= ? AND orv.created_at <= ?
         ORDER BY ABS(orv.hypothetical_pnl) DESC LIMIT 1
        """,
        (start, end),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "label": row[0],
        "hypothetical_pnl": micro_to_usd(int(row[1])),
        "market_question": row[2],
        "wallet": row[3],
    }


async def _data_health(conn, start, end) -> dict:
    cur = await conn.execute(
        """
        SELECT severity, COUNT(*) FROM data_quality_events
         WHERE resolved_at IS NULL
         GROUP BY severity
        """,
    )
    open_events = {sev: int(n) for sev, n in await cur.fetchall()}
    cur = await conn.execute(
        """
        SELECT COUNT(*) FROM job_runs
         WHERE status = 'error' AND started_at >= ? AND started_at <= ?
        """,
        (start, end),
    )
    job_failures = int((await cur.fetchone())[0])
    return {"open_data_quality_events": open_events, "job_failures_today": job_failures}


# --- Telegram-free formatting (kept for the dashboard's text preview) --------

def format_daily_text(report: dict) -> str:
    """Concise plain-text summary (short lines, no markdown). Dashboard preview."""
    c = report["comparison"]
    verdict = c.get("caveat") or f"filtered-vs-blind: {c['verdict']} ({report['filtered_minus_blind_pnl']:+})"
    lesson = report.get("top_lesson")
    lesson_line = (
        f"Lesson: {lesson['label']} on {lesson.get('market_question') or '?'}"
        if lesson else "Lesson: (none today)"
    )
    lines = [
        f"Polymarket paper report {report['report_date']} (v{report['strategy_version']})",
        f"PnL today {report['pnl_today']:+} | total {report['total_pnl']:+} | equity {report['equity']}",
        f"Cash {report['cash']} | open {report['open_positions']} | maxDD {report['max_drawdown']}",
        f"Win rate {report['win_rate']} (n={report['win_sample']})",
        f"Copies {report['copies']} watches {report['watches']} skips {report['skips']}",
        f"Missed winners {report['missed_winners']} | good skips {report['avoided_losers']}",
        verdict,
        lesson_line,
        f"Data health: {report['data_health']['open_data_quality_events']} | "
        f"job failures {report['data_health']['job_failures_today']}",
    ]
    return "\n".join(lines)


# --- persistence ------------------------------------------------------------

async def _store_report(
    conn: aiosqlite.Connection, report: dict, *, report_type: str
) -> int:
    """Insert-or-replace a daily_reports row (idempotent per (type, date)).

    Re-running the same day REPLACES the stored row (research reports are
    recomputed cheaply; no delivery side-effect exists to protect). Delivery is
    dashboard-only: delivery_status='dashboard', telegram_message_id NULL.
    """
    now = utcnow_iso()
    comparison = report["comparison"]
    summary = format_daily_text(report)
    data_health = report["data_health"]
    await conn.execute(
        """
        INSERT INTO daily_reports
            (report_type, report_date, strategy_version, filtered_pnl, blind_copy_pnl,
             filtered_minus_blind_pnl, max_drawdown, summary_json, data_health_json,
             delivery_status, telegram_message_id, delivery_error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'dashboard', NULL, NULL, ?)
        ON CONFLICT(report_type, report_date) DO UPDATE SET
            strategy_version=excluded.strategy_version,
            filtered_pnl=excluded.filtered_pnl,
            blind_copy_pnl=excluded.blind_copy_pnl,
            filtered_minus_blind_pnl=excluded.filtered_minus_blind_pnl,
            max_drawdown=excluded.max_drawdown,
            summary_json=excluded.summary_json,
            data_health_json=excluded.data_health_json,
            delivery_status='dashboard',
            telegram_message_id=NULL,
            delivery_error=NULL,
            created_at=excluded.created_at
        """,
        (
            report_type,
            report["report_date"],
            report["strategy_version"],
            usd_to_micro(report.get("filtered_pnl", ZERO)),
            usd_to_micro(report.get("blind_pnl", ZERO)),
            usd_to_micro(report["filtered_minus_blind_pnl"]),
            usd_to_micro(report["max_drawdown"]),
            json_dumps({"text": summary, "verdict": comparison["verdict"]}),
            json_or_none(data_health),
            now,
        ),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT id FROM daily_reports WHERE report_type = ? AND report_date = ?",
        (report_type, report["report_date"]),
    )
    row = await cur.fetchone()
    return int(row[0])


def _local_day(config: Config, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(config.report_tz)
    return local.strftime("%Y-%m-%d")


async def run_daily_report(ctx: JobContext, config: Config, *, day: str | None = None) -> dict:
    """Compose the daily report and store it (dashboard delivery, no network)."""
    day = day or _local_day(config)
    report = await compose_daily(ctx.conn, config, day)
    report_id = await _store_report(ctx.conn, report, report_type="daily")
    ctx.written()
    return {"status": "stored", "report_id": report_id, "report_date": day,
            "delivery": "dashboard"}


# --- weekly (PRD 22.2) ------------------------------------------------------

async def compose_weekly(conn: aiosqlite.Connection, config: Config, week_end: str) -> dict:
    """Weekly report incl. autonomy-gate progress (PRD 22.2 / 30)."""
    payload = await _active_payload(conn)
    strategy_version = await active_rule_set_version(conn)
    portfolio_id = await get_active_portfolio_id(conn)

    filtered_rows = await _resolved_trades(conn, portfolio_id) if portfolio_id else []
    filtered_metrics = bm.cohort_metrics(filtered_rows)
    blind_rows = await _blind_trades(conn)
    blind_metrics = bm.cohort_metrics(blind_rows)
    min_sample = bm.min_benchmark_sample(payload)
    comparison = bm.compare(filtered_metrics, blind_metrics, min_sample=min_sample)
    if not comparison["sufficient_sample"]:
        comparison["caveat"] = INSUFFICIENT_SAMPLE_CAVEAT

    # Autonomy gates (PRD 30): days of operation, judged paper copies vs 100,
    # unresolved critical DQ events, max drawdown.
    days_operating = await _days_of_operation(conn, portfolio_id)
    judged_copies = await _judged_paper_copies(conn)
    cur = await conn.execute(
        "SELECT COUNT(*) FROM data_quality_events WHERE severity = 'critical' AND resolved_at IS NULL"
    )
    unresolved_critical = int((await cur.fetchone())[0])

    max_dd = ZERO
    if portfolio_id is not None:
        cur = await conn.execute(
            "SELECT MAX(drawdown) FROM pnl_snapshots WHERE paper_portfolio_id = ?",
            (portfolio_id,),
        )
        r = await cur.fetchone()
        if r and r[0] is not None:
            max_dd = micro_to_usd(int(r[0]))

    rule_changes = await _rule_changes_week(conn, week_end)

    return {
        "report_date": week_end,
        "report_type": "weekly",
        "strategy_version": strategy_version,
        "filtered_metrics": filtered_metrics,
        "blind_metrics": blind_metrics,
        "comparison": comparison,
        "filtered_pnl": filtered_metrics["net_pnl"],
        "blind_pnl": blind_metrics["net_pnl"],
        "filtered_minus_blind_pnl": comparison["filtered_minus_blind_pnl"],
        "max_drawdown": max_dd,
        "rule_changes": rule_changes,
        "autonomy_gates": {
            "days_of_operation": days_operating,
            "days_required": 30,
            "judged_paper_copies": judged_copies,
            "judged_paper_copies_required": 100,
            "filtered_minus_blind_pnl": comparison["filtered_minus_blind_pnl"],
            "outperforms_blind": comparison["verdict"] == "filtered_better",
            "unresolved_critical_dq_events": unresolved_critical,
            "max_drawdown": max_dd,
        },
        "data_health": await _data_health_week(conn),
    }


async def _days_of_operation(conn, portfolio_id) -> int:
    if portfolio_id is None:
        return 0
    cur = await conn.execute(
        "SELECT started_at FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    row = await cur.fetchone()
    if row is None or not row[0]:
        return 0
    start = row[0].replace("Z", "+00:00")
    started = datetime.fromisoformat(start)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - started).days)


async def _judged_paper_copies(conn) -> int:
    """Judged (eligible) final reviews on paper_copy decisions (autonomy gate)."""
    cur = await conn.execute(
        """
        SELECT COUNT(*)
          FROM outcome_reviews orv
          JOIN decision_journal dj ON dj.id = orv.decision_journal_id
         WHERE orv.review_checkpoint = 'final' AND orv.eligible_for_learning = 1
           AND orv.is_demo = 0 AND dj.decision = 'paper_copy'
        """,
    )
    return int((await cur.fetchone())[0])


async def _rule_changes_week(conn, week_end) -> list[dict]:
    end = f"{week_end}T23:59:59.999999Z"
    start = _iso_week_start(week_end)
    return await _rule_changes_today(conn, start, end)


def _iso_week_start(week_end: str) -> str:
    d = date.fromisoformat(week_end) - timedelta(days=6)
    return f"{d.isoformat()}T00:00:00.000000Z"


async def _data_health_week(conn) -> dict:
    cur = await conn.execute(
        "SELECT severity, COUNT(*) FROM data_quality_events WHERE resolved_at IS NULL GROUP BY severity"
    )
    return {"open_data_quality_events": {s: int(n) for s, n in await cur.fetchall()}}


async def run_weekly_report(ctx: JobContext, config: Config, *, week_end: str | None = None) -> dict:
    """Run only on Sundays; store a weekly daily_reports row (dashboard delivery)."""
    now = datetime.now(timezone.utc).astimezone(config.report_tz)
    if week_end is None:
        # weekday(): Monday=0 .. Sunday=6
        if now.weekday() != 6:
            return {"status": "skipped", "reason": "not_sunday", "weekday": now.weekday()}
        week_end = now.strftime("%Y-%m-%d")
    report = await compose_weekly(ctx.conn, config, week_end)
    # Reuse daily_reports store, flat fields; summary carries the weekly text.
    now_iso = utcnow_iso()
    await ctx.conn.execute(
        """
        INSERT INTO daily_reports
            (report_type, report_date, strategy_version, filtered_pnl, blind_copy_pnl,
             filtered_minus_blind_pnl, max_drawdown, summary_json, data_health_json,
             delivery_status, telegram_message_id, delivery_error, created_at)
        VALUES ('weekly', ?, ?, ?, ?, ?, ?, ?, ?, 'dashboard', NULL, NULL, ?)
        ON CONFLICT(report_type, report_date) DO UPDATE SET
            strategy_version=excluded.strategy_version,
            filtered_pnl=excluded.filtered_pnl,
            blind_copy_pnl=excluded.blind_copy_pnl,
            filtered_minus_blind_pnl=excluded.filtered_minus_blind_pnl,
            max_drawdown=excluded.max_drawdown,
            summary_json=excluded.summary_json,
            data_health_json=excluded.data_health_json,
            delivery_status='dashboard',
            created_at=excluded.created_at
        """,
        (
            week_end,
            report["strategy_version"],
            usd_to_micro(report["filtered_pnl"]),
            usd_to_micro(report["blind_pnl"]),
            usd_to_micro(report["filtered_minus_blind_pnl"]),
            usd_to_micro(report["max_drawdown"]),
            json_dumps({
                "autonomy_gates": {k: (str(v) if isinstance(v, Decimal) else v)
                                   for k, v in report["autonomy_gates"].items()},
                "verdict": report["comparison"]["verdict"],
            }),
            json_or_none(report["data_health"]),
            now_iso,
        ),
    )
    await ctx.conn.commit()
    ctx.written()
    cur = await ctx.conn.execute(
        "SELECT id FROM daily_reports WHERE report_type = 'weekly' AND report_date = ?",
        (week_end,),
    )
    report_id = int((await cur.fetchone())[0])
    return {"status": "stored", "report_id": report_id, "report_date": week_end,
            "delivery": "dashboard"}


# --- drawdown-breach alert (wired from pnl / health passes) -----------------

async def check_drawdown_breach(conn: aiosqlite.Connection, config: Config) -> dict:
    """Raise a dashboard alert if drawdown exceeds the rule-payload limit.

    Limit = active payload exposure_limits.max_drawdown_limit_fraction, default
    0.25 of the starting bankroll (PRD 30). Dashboard-only alert, deduped.
    """
    portfolio_id = await get_active_portfolio_id(conn)
    if portfolio_id is None:
        return {"breached": False, "reason": "no_portfolio"}
    payload = await _active_payload(conn)
    limit_fraction = Decimal(str(
        payload.get("exposure_limits", {}).get("max_drawdown_limit_fraction",
                                                str(DEFAULT_DRAWDOWN_LIMIT))
    ))
    cur = await conn.execute(
        "SELECT starting_bankroll FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    bankroll = micro_to_usd(int((await cur.fetchone())[0]))
    cur = await conn.execute(
        """
        SELECT drawdown FROM pnl_snapshots WHERE paper_portfolio_id = ?
         ORDER BY collected_at DESC LIMIT 1
        """,
        (portfolio_id,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return {"breached": False, "reason": "no_snapshot"}
    drawdown = micro_to_usd(int(row[0]))
    limit_usd = bankroll * limit_fraction
    if drawdown <= limit_usd:
        return {"breached": False, "drawdown": drawdown, "limit": limit_usd}
    await alert(
        conn,
        type="drawdown_breach",
        severity="critical",
        dedupe_key=f"drawdown_breach:{portfolio_id}",
        message=f"Drawdown {drawdown} exceeds limit {limit_usd} ({limit_fraction} of bankroll).",
        metadata={"drawdown": str(drawdown), "limit": str(limit_usd)},
    )
    return {"breached": True, "drawdown": drawdown, "limit": limit_usd}


async def check_repeated_job_failures(
    conn: aiosqlite.Connection, *, threshold: int = 3, window_hours: int = 6
) -> dict:
    """Raise a dashboard alert when a job fails >= threshold times in the window."""
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    ) + "Z"
    cur = await conn.execute(
        """
        SELECT job_name, COUNT(*) FROM job_runs
         WHERE status = 'error' AND started_at >= ?
         GROUP BY job_name HAVING COUNT(*) >= ?
        """,
        (since, threshold),
    )
    alerted = []
    for job_name, n in await cur.fetchall():
        await alert(
            conn,
            type="repeated_job_failure",
            severity="warning",
            dedupe_key=f"repeated_job_failure:{job_name}",
            message=f"Job {job_name} failed {n} times in the last {window_hours}h.",
            metadata={"job_name": job_name, "failures": int(n)},
        )
        alerted.append(job_name)
    return {"alerted_jobs": alerted}
