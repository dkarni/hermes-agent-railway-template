"""Worker entrypoint: `python -m polymarket.worker`.

Loads config (enforcing the paper-mode guard), initializes the database
(migrations + seed rule set), and serves the loopback API. The asyncio scheduler
arrives in a later wave; init_scheduler() is a clean no-op hook for now.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import db as dbmod
from .api import create_app
from .config import Config, load_config
from .http import make_client
from .scheduler import Scheduler

log = logging.getLogger("polymarket.worker")


def build_scheduler(conn, config: Config, *, client=None) -> Scheduler:
    """Register all Wave 2 jobs on an in-process scheduler (PRD sec 9).

    Adapters share one AllowlistClient (host allowlist + rate limiting). Polling
    cadences come from config. Wave 3 jobs (pnl hourly, reviews, rule_eval,
    reports) get registered here later — see the TODO hooks below.
    """
    from .adapters.clob import ClobAdapter
    from .adapters.dataapi import DataApiAdapter
    from .adapters.gamma import GammaAdapter
    from .jobs.runner import JobContext
    from .jobs.ingest_history import run_ingest_history
    from .jobs.leaderboard_scan import run_leaderboard_scan
    from .jobs.monitor import run_monitor
    from .jobs.paper_exec import paper_copy_callback
    from .jobs.pnl import run_pnl
    from .jobs.portfolio_view import load_portfolio_view
    from .jobs.profile_wallets import run_profile_wallets
    from .jobs.reconcile import run_reconcile
    from .jobs.reports import (
        check_drawdown_breach,
        check_repeated_job_failures,
        run_daily_report,
        run_weekly_report,
    )
    from .jobs.reviews import run_reviews
    from .jobs.rule_eval import run_rule_eval

    async def _quality_cb(event_type: str, host: str, details: dict) -> None:
        await conn.execute(
            "INSERT INTO data_quality_events (severity, source, event_type, detected_at, details_json) "
            "VALUES ('warning', ?, ?, ?, ?)",
            (host, event_type, dbmod.utcnow_iso(), dbmod.json_or_none(details)),
        )
        await conn.commit()

    client = client or make_client(config, quality_callback=_quality_cb)
    dataapi = DataApiAdapter(client, config.data_base_url)
    gamma = GammaAdapter(client, config.gamma_base_url)
    clob = ClobAdapter(client, config.clob_base_url)

    scheduler = Scheduler(conn, timezone=config.report_timezone)

    scheduler.register(
        "leaderboard_scan",
        lambda ctx: run_leaderboard_scan(ctx, config, dataapi),
        daily_at="03:00", jitter_seconds=30,
    )
    scheduler.register(
        "ingest_history",
        lambda ctx: run_ingest_history(ctx, config, dataapi, gamma),
        every_seconds=600, stagger_seconds=15, jitter_seconds=10,
    )
    scheduler.register(
        "profile_wallets",
        lambda ctx: run_profile_wallets(ctx, config),
        every_seconds=1800, stagger_seconds=30, jitter_seconds=10,
    )
    async def _monitor(ctx):
        # Real portfolio view (load_portfolio_view) so exposure/cash/daily-copy
        # gates bind; open a paper trade on every paper_copy decision.
        portfolio = await load_portfolio_view(ctx.conn)
        return await run_monitor(
            ctx, config, dataapi, gamma, clob,
            portfolio=portfolio, on_paper_copy=paper_copy_callback,
        )

    async def _reconcile(ctx):
        portfolio = await load_portfolio_view(ctx.conn)
        return await run_reconcile(
            ctx, config, dataapi, gamma, clob, portfolio=portfolio,
        )

    async def _health_pass(ctx):
        # Small health pass: drawdown breach + repeated job failures (dashboard
        # alerts only, deduped). Runs alongside pnl cadence.
        dd = await check_drawdown_breach(ctx.conn, config)
        jf = await check_repeated_job_failures(ctx.conn)
        return {"drawdown": dd, "job_failures": jf}

    scheduler.register(
        "monitor",
        _monitor,
        every_seconds=config.tracked_wallet_poll_seconds, stagger_seconds=5, jitter_seconds=5,
    )
    scheduler.register(
        "reconcile",
        _reconcile,
        every_seconds=900, stagger_seconds=45, jitter_seconds=15,
    )
    # Wave 3 jobs.
    scheduler.register(
        "pnl",
        lambda ctx: run_pnl(ctx, clob),
        every_seconds=3600, stagger_seconds=20, jitter_seconds=30,
    )
    scheduler.register(
        "reviews",
        lambda ctx: run_reviews(ctx, clob),
        every_seconds=300, stagger_seconds=10, jitter_seconds=10,
    )
    scheduler.register(
        "health",
        _health_pass,
        every_seconds=3600, stagger_seconds=90, jitter_seconds=30,
    )
    scheduler.register(
        "daily_report",
        lambda ctx: run_daily_report(ctx, config),
        daily_at=config.daily_report_time, jitter_seconds=15,
    )
    # Weekly report fires daily at the same cutoff but no-ops unless Sunday
    # (weekday check is inside run_weekly_report, evaluated in REPORT_TIMEZONE).
    scheduler.register(
        "weekly_report",
        lambda ctx: run_weekly_report(ctx, config),
        daily_at=_after(config.daily_report_time, minutes=2), jitter_seconds=15,
    )
    # Rule evaluation runs daily AFTER the report cutoff (PRD sec 9/17).
    scheduler.register(
        "rule_eval",
        lambda ctx: run_rule_eval(ctx, config),
        daily_at=_after(config.daily_report_time, minutes=5), jitter_seconds=20,
    )
    return scheduler


def _after(hhmm: str, *, minutes: int) -> str:
    """Return HH:MM shifted by ``minutes`` (wrapping within a day)."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    total = (hh * 60 + mm + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


async def init_scheduler(conn, config: Config) -> Scheduler:
    """Build and start the in-process asyncio scheduler."""
    scheduler = build_scheduler(conn, config)
    scheduler.start()
    return scheduler


async def _abort_zombie_runs(conn) -> int:
    """Close job_runs left 'running' by a mid-run container restart.

    The worker is the only writer, so any 'running' row at startup is a dead
    run. Left alone it would wedge the manual-action 409 guard ("already
    running") forever and lie on the health page.
    """
    cur = await conn.execute(
        """
        UPDATE job_runs
           SET status = 'aborted', finished_at = ?,
               error_json = json_object('type', 'Aborted',
                                        'message', 'worker restarted mid-run')
         WHERE status = 'running'
        """,
        (dbmod.utcnow_iso(),),
    )
    await conn.commit()
    return cur.rowcount


async def _startup(config: Config):
    from .jobs.portfolio_view import ensure_portfolio

    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    aborted = await _abort_zombie_runs(conn)
    if aborted:
        logging.getLogger("polymarket.worker").warning(
            "aborted %d zombie job run(s) from a previous container", aborted
        )
    await ensure_portfolio(conn, starting_bankroll=config.paper_starting_bankroll)
    scheduler = await init_scheduler(conn, config)
    return conn, scheduler


def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info(
        "polymarket worker starting (mode=%s, db=%s, port=%s)",
        config.trading_mode,
        config.db_path,
        config.port,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    conn, scheduler = loop.run_until_complete(_startup(config))

    async def _shutdown() -> None:
        await scheduler.stop()
        await conn.close()

    app = create_app(conn, config, on_shutdown=[_shutdown], scheduler=scheduler)

    uvicorn_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=config.port,
        log_level=config.log_level.lower(),
        loop="none",
    )
    server = uvicorn.Server(uvicorn_config)
    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    main()
