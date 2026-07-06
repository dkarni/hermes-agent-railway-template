"""Wave 3 report + alert tests (PRD sec 22, 21.2) — dashboard-only delivery.

Telegram delivery was cut: these assert reports and alerts are STORED with
dashboard delivery status and that no code path attempts network delivery.
Also: compose_daily numbers on a seeded DB; alert dedupe / quiet-period.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from .. import db as dbmod
from ..config import load_config
from ..jobs import alerts
from ..jobs.portfolio_view import ensure_portfolio
from ..jobs.reports import compose_daily, run_daily_report, run_weekly_report
from ..jobs.runner import JobContext


def _migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


def _config(tmp_path):
    return load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})


async def _ctx(conn):
    cur = await conn.execute(
        "INSERT INTO job_runs (job_name, trigger_type, started_at, status) "
        "VALUES ('t','manual',?, 'running')",
        (dbmod.utcnow_iso(),),
    )
    await conn.commit()
    return JobContext(conn, int(cur.lastrowid), "t")


async def _seed_day(conn, day: str):
    """Seed one resolved winning paper trade + a decision on ``day``."""
    await ensure_portfolio(conn, starting_bankroll=Decimal("1000"))
    now = f"{day}T12:00:00.000000Z"
    await conn.execute(
        "INSERT INTO markets (market_id, condition_id, category, status, winning_outcome, metadata_updated_at) "
        "VALUES ('mkt1','0xc','CRYPTO','resolved','Yes',?)", (now,),
    )
    cur = await conn.execute(
        "INSERT INTO observed_trades (source, wallet_address, condition_id, asset_id, source_side, outcome, idempotency_key, detected_at) "
        "VALUES ('data','0xw','0xc','a','BUY','Yes','k1',?)", (now,),
    )
    obs = int(cur.lastrowid)
    cur = await conn.execute(
        "INSERT INTO decision_journal (strategy, observed_trade_id, wallet_address, market_id, decision, executable_entry_price, expected_position_usd, created_at) "
        "VALUES ('default',?,'0xw','mkt1','paper_copy',?,?,?)",
        (obs, dbmod.px_to_micro(Decimal("0.5")), dbmod.usd_to_micro(Decimal("10")), now),
    )
    dj = int(cur.lastrowid)
    cur = await conn.execute("SELECT id FROM paper_portfolios WHERE status='active'")
    pid = int((await cur.fetchone())[0])
    await conn.execute(
        "INSERT INTO paper_trades (paper_portfolio_id, decision_journal_id, observed_trade_id, wallet_address, market_id, asset_id, outcome, status, shares, entry_cost, realized_pnl, benchmark_cohort, opened_at, closed_at, created_at) "
        "VALUES (?,?,?,'0xw','mkt1','a','Yes','resolved',?,?,?, 'filtered', ?, ?, ?)",
        (pid, dj, obs, dbmod.usd_to_micro(Decimal("20")), dbmod.usd_to_micro(Decimal("10")),
         dbmod.usd_to_micro(Decimal("10")), now, now, now),
    )
    # final review labelling it good_copy, eligible.
    await conn.execute(
        "INSERT INTO outcome_reviews (decision_journal_id, review_checkpoint, hypothetical_pnl, decision_quality_label, eligible_for_learning, created_at) "
        "VALUES (?, 'final', ?, 'good_copy', 1, ?)",
        (dj, dbmod.usd_to_micro(Decimal("10")), now),
    )
    await conn.commit()
    return now


@pytest.mark.asyncio
async def test_compose_daily_numbers(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        config = _config(tmp_path)
        day = "2026-07-06"
        await _seed_day(conn, day)
        report = await compose_daily(conn, config, day)
        assert report["report_date"] == day
        assert report["pnl_today"] == Decimal("10")
        assert report["total_pnl"] == Decimal("10")
        assert report["copies"] == 1
        assert report["win_sample"] == 1
        assert report["win_rate"] == Decimal("1")
        assert report["best_paper_trade"]["realized_pnl"] == Decimal("10")
        # top lesson picks the highest-|hypo| labelled review with its label.
        assert report["top_lesson"]["label"] == "good_copy"
        # blind cohort empty -> comparison insufficient with verbatim caveat.
        assert report["comparison"]["sufficient_sample"] is False
        assert "Insufficient sample" in report["comparison"]["caveat"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_daily_report_stores_dashboard_row(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        config = _config(tmp_path)
        day = "2026-07-06"
        await _seed_day(conn, day)
        ctx = await _ctx(conn)
        result = await run_daily_report(ctx, config, day=day)
        assert result["status"] == "stored"
        assert result["delivery"] == "dashboard"
        cur = await conn.execute(
            "SELECT report_type, delivery_status, telegram_message_id, delivery_error "
            "FROM daily_reports WHERE report_date = ?", (day,)
        )
        rtype, status, tg_id, err = await cur.fetchone()
        assert rtype == "daily"
        assert status == "dashboard"
        assert tg_id is None          # no telegram send
        assert err is None

        # Idempotent: re-run replaces the same-day row (still exactly one).
        await run_daily_report(ctx, config, day=day)
        cur = await conn.execute(
            "SELECT COUNT(*) FROM daily_reports WHERE report_type='daily' AND report_date = ?", (day,)
        )
        assert int((await cur.fetchone())[0]) == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_weekly_report_stored_dashboard(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        config = _config(tmp_path)
        await _seed_day(conn, "2026-07-05")
        ctx = await _ctx(conn)
        # Explicit week_end bypasses the Sunday gate.
        result = await run_weekly_report(ctx, config, week_end="2026-07-05")
        assert result["status"] == "stored"
        cur = await conn.execute(
            "SELECT delivery_status, telegram_message_id FROM daily_reports "
            "WHERE report_type='weekly' AND report_date='2026-07-05'"
        )
        status, tg_id = await cur.fetchone()
        assert status == "dashboard"
        assert tg_id is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_weekly_report_skips_when_not_sunday(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        # Force a non-Sunday timezone-aware "now" via a config whose tz is UTC;
        # we cannot easily freeze time, so assert the gate returns skipped when
        # week_end is None on a non-Sunday. To keep the test deterministic we
        # only assert the shape when it does skip.
        import datetime as _dt
        config = _config(tmp_path)
        ctx = await _ctx(conn)
        result = await run_weekly_report(ctx, config)
        if _dt.datetime.now(_dt.timezone.utc).astimezone(config.report_tz).weekday() != 6:
            assert result["status"] == "skipped"
            assert result["reason"] == "not_sunday"
        else:
            assert result["status"] == "stored"
    finally:
        await conn.close()


# --- alerts: dedupe + quiet period ------------------------------------------

def test_is_within_quiet_period_pure():
    assert alerts.is_within_quiet_period(None, "2026-07-06T10:00:00Z") is False
    assert alerts.is_within_quiet_period(
        "2026-07-06T09:00:00Z", "2026-07-06T10:00:00Z", quiet_seconds=6 * 3600
    ) is True
    assert alerts.is_within_quiet_period(
        "2026-07-06T01:00:00Z", "2026-07-06T10:00:00Z", quiet_seconds=6 * 3600
    ) is False


@pytest.mark.asyncio
async def test_alert_dedupe_within_quiet_period(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        first = await alerts.alert(
            conn, type="drawdown_breach", severity="critical",
            dedupe_key="dd:1", message="m1", now_iso="2026-07-06T09:00:00.000000Z",
        )
        assert first["stored"] is True and first["suppressed"] is False

        # Second within 6h -> suppressed (stored as audit row, not "stored").
        second = await alerts.alert(
            conn, type="drawdown_breach", severity="critical",
            dedupe_key="dd:1", message="m2", now_iso="2026-07-06T11:00:00.000000Z",
        )
        assert second["suppressed"] is True and second["stored"] is False

        # After the quiet period -> stored again.
        third = await alerts.alert(
            conn, type="drawdown_breach", severity="critical",
            dedupe_key="dd:1", message="m3", now_iso="2026-07-06T16:00:00.000000Z",
        )
        assert third["stored"] is True

        cur = await conn.execute(
            "SELECT delivery_channel, delivery_status FROM alerts WHERE dedupe_key='dd:1' ORDER BY id"
        )
        rows = await cur.fetchall()
        assert [r[1] for r in rows] == ["stored", "suppressed", "stored"]
        assert all(r[0] == "dashboard" for r in rows)  # never a network channel
    finally:
        await conn.close()
