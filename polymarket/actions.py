"""Operator action dispatch (PRD sec 19.2).

Actions run a job via jobs/runner (which records a job_runs row and holds the
per-name asyncio lock so the same job never overlaps). Handlers here build the
adapters/config a job needs, launch it with asyncio.create_task, and return
{"job_run_id": ...} immediately - the HTTP layer never blocks on the job.

If the named job is already running (a 'running' job_runs row for that name),
the caller gets a 409 with the running job's id instead of a new run.
"""

from __future__ import annotations

import asyncio
import json
import traceback

from .config import Config
from .db import json_or_none, utcnow_iso
from .http import make_client
from .jobs.runner import JobContext, _lock_for

# Action name -> internal job_name (the lock key + job_runs.job_name).
ACTION_JOBS: dict[str, str] = {
    "scan-leaderboard": "leaderboard_scan",
    "ingest-history": "ingest_history",
    "run-monitor": "monitor",
    "profile-wallets": "profile_wallets",
    "reconcile-trades": "reconcile",
    "update-pnl": "pnl",
    "review-outcomes": "reviews",
    "evaluate-rules": "rule_eval",
    "generate-report": "daily_report",
    "generate-weekly-report": "weekly_report",
    "health-check": "health",
    "reset-portfolio": "reset_portfolio",
}


class ActionRunning(Exception):
    """Raised when an action's underlying job is already running."""

    def __init__(self, job_name: str, job_run_id: int) -> None:
        super().__init__(f"{job_name} already running (run {job_run_id})")
        self.job_name = job_name
        self.job_run_id = job_run_id


class ActionNotFound(Exception):
    pass


async def _running_id(conn, job_name: str) -> int | None:
    cur = await conn.execute(
        "SELECT id FROM job_runs WHERE job_name = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
        (job_name,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else None


def _build_adapters(config: Config):
    client = make_client(config)
    from .adapters.clob import ClobAdapter
    from .adapters.dataapi import DataApiAdapter
    from .adapters.gamma import GammaAdapter

    return (
        DataApiAdapter(client, config.data_base_url),
        GammaAdapter(client, config.gamma_base_url),
        ClobAdapter(client, config.clob_base_url),
    )


def _coro_for(action: str, config: Config):
    """Return a coro_fn(ctx) for the given action name (adapters built lazily)."""
    if action == "scan-leaderboard":
        dataapi, _, _ = _build_adapters(config)
        from .jobs.leaderboard_scan import run_leaderboard_scan
        return lambda ctx: run_leaderboard_scan(ctx, config, dataapi)
    if action == "profile-wallets":
        from .jobs.profile_wallets import run_profile_wallets
        return lambda ctx: run_profile_wallets(ctx, config)
    if action == "ingest-history":
        dataapi, gamma, _ = _build_adapters(config)
        from .jobs.ingest_history import run_ingest_history
        return lambda ctx: run_ingest_history(
            ctx, config, dataapi, gamma, limit_wallets=config.tracked_wallet_limit
        )
    if action == "run-monitor":
        dataapi, gamma, clob = _build_adapters(config)
        from .jobs.monitor import run_monitor
        from .jobs.paper_exec import paper_copy_callback
        from .jobs.portfolio_view import load_portfolio_view

        async def _monitor(ctx):
            portfolio = await load_portfolio_view(ctx.conn)
            return await run_monitor(
                ctx, config, dataapi, gamma, clob,
                portfolio=portfolio, on_paper_copy=paper_copy_callback,
            )
        return _monitor
    if action == "reconcile-trades":
        dataapi, gamma, clob = _build_adapters(config)
        from .jobs.portfolio_view import load_portfolio_view
        from .jobs.reconcile import run_reconcile

        async def _reconcile(ctx):
            portfolio = await load_portfolio_view(ctx.conn)
            return await run_reconcile(ctx, config, dataapi, gamma, clob, portfolio=portfolio)
        return _reconcile
    if action == "update-pnl":
        _, _, clob = _build_adapters(config)
        from .jobs.pnl import run_pnl
        return lambda ctx: run_pnl(ctx, clob)
    if action == "review-outcomes":
        _, _, clob = _build_adapters(config)
        from .jobs.reviews import run_reviews
        return lambda ctx: run_reviews(ctx, clob)
    if action == "evaluate-rules":
        from .jobs.rule_eval import run_rule_eval
        return lambda ctx: run_rule_eval(ctx, config)
    if action == "generate-report":
        from .jobs.reports import run_daily_report
        return lambda ctx: run_daily_report(ctx, config)
    if action == "generate-weekly-report":
        from .jobs.reports import run_weekly_report
        return lambda ctx: run_weekly_report(ctx, config)
    if action == "health-check":
        from .jobs.reports import check_drawdown_breach, check_repeated_job_failures

        async def _health(ctx):
            dd = await check_drawdown_breach(ctx.conn, config)
            jf = await check_repeated_job_failures(ctx.conn)
            return {"drawdown": dd, "job_failures": jf}
        return _health
    if action == "reset-portfolio":
        from .jobs.portfolio_view import reset_portfolio

        async def _reset(ctx):
            pid = await reset_portfolio(
                ctx.conn, starting_bankroll=config.paper_starting_bankroll
            )
            ctx.written()
            return {"status": "reset", "portfolio_id": pid}
        return _reset
    raise ActionNotFound(action)


async def _insert_running(conn, job_name: str) -> int:
    cur = await conn.execute(
        "INSERT INTO job_runs (job_name, trigger_type, started_at, status, lock_key) "
        "VALUES (?, 'manual', ?, 'running', ?)",
        (job_name, utcnow_iso(), job_name),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def _run_prebooked(conn, job_name: str, coro_fn, job_run_id: int) -> None:
    """Execute a pre-booked run, recording the outcome onto the existing row."""
    async with _lock_for(job_name):
        ctx = JobContext(conn, job_run_id, job_name)
        status = "success"
        error_json = None
        try:
            result = await coro_fn(ctx)
            if isinstance(result, dict):
                ctx.metadata.update(result)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            status = "error"
            error_json = json.dumps(
                {"type": type(exc).__name__, "message": str(exc),
                 "traceback": traceback.format_exc()}
            )
        await conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, records_read = ?, "
            "records_written = ?, records_skipped = ?, error_json = ?, metadata_json = ? "
            "WHERE id = ?",
            (
                utcnow_iso(), status, ctx.records_read, ctx.records_written,
                ctx.records_skipped, error_json,
                json_or_none(ctx.metadata) if ctx.metadata else None, job_run_id,
            ),
        )
        await conn.commit()


async def start_action(conn, config: Config, action: str) -> int:
    """Launch an action's job in the background. Returns the new job_run_id.

    Raises ActionRunning if the job is already running, ActionNotFound if the
    action name is unknown.
    """
    if action not in ACTION_JOBS:
        raise ActionNotFound(action)
    job_name = ACTION_JOBS[action]
    running = await _running_id(conn, job_name)
    if running is not None:
        raise ActionRunning(job_name, running)

    coro_fn = _coro_for(action, config)
    # Pre-create the running row synchronously so a concurrent second call sees
    # 'running' immediately (the runner's per-name lock still guards execution).
    job_run_id = await _insert_running(conn, job_name)
    asyncio.create_task(_run_prebooked(conn, job_name, coro_fn, job_run_id))
    return job_run_id


async def retry_job(conn, config: Config, job_run_id: int) -> dict:
    """Retry a prior job run by dispatching its action again (PRD 19.2)."""
    cur = await conn.execute("SELECT job_name FROM job_runs WHERE id = ?", (job_run_id,))
    row = await cur.fetchone()
    if row is None:
        raise ActionNotFound(f"job_run {job_run_id}")
    job_name = row[0]
    action = next((a for a, j in ACTION_JOBS.items() if j == job_name), None)
    if action is None:
        raise ActionNotFound(f"job {job_name} is not retriable")
    new_id = await start_action(conn, config, action)
    return {"job_run_id": new_id, "retried_from": job_run_id}


async def rollback_rule(conn, version: int) -> dict:
    """Manual rule rollback (reuses rule_eval._do_rollback via run_manual_rollback)."""
    from .jobs.rule_eval import run_manual_rollback

    job_name = "rule_eval"
    running = await _running_id(conn, job_name)
    if running is not None:
        raise ActionRunning(job_name, running)
    job_run_id = await _insert_running(conn, job_name)

    async def _coro(ctx):
        return await run_manual_rollback(ctx, version)

    await _run_prebooked(conn, job_name, _coro, job_run_id)
    cur = await conn.execute("SELECT metadata_json FROM job_runs WHERE id = ?", (job_run_id,))
    row = await cur.fetchone()
    meta = json.loads(row[0]) if row and row[0] else {}
    meta["job_run_id"] = job_run_id
    return meta
