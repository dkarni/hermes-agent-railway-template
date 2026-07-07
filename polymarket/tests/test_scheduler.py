from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from starlette.testclient import TestClient

from .. import db as dbmod
from ..api import create_app
from ..config import load_config
from ..scheduler import Scheduler
from ..worker import build_scheduler


def _migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


@pytest.mark.asyncio
async def test_scheduler_runs_registered_job(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    ran = asyncio.Event()

    async def job(ctx):
        ran.set()

    sched = Scheduler(conn, timezone="UTC")
    sched.register("tick", job, every_seconds=3600, stagger_seconds=0)
    sched.start()
    try:
        await asyncio.wait_for(ran.wait(), timeout=2)
    finally:
        await sched.stop()
        await conn.close()
    assert ran.is_set()


@pytest.mark.asyncio
async def test_scheduler_register_requires_one_trigger(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        sched = Scheduler(conn, timezone="UTC")
        with pytest.raises(ValueError):
            sched.register("x", lambda ctx: None, every_seconds=1, daily_at="03:00")
        with pytest.raises(ValueError):
            sched.register("y", lambda ctx: None)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_build_scheduler_registers_all_jobs(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})
    conn = await dbmod.init_db(config.db_path, _migrations_dir())
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    try:
        from ..http import AllowlistClient
        allow = AllowlistClient(
            config.allowed_hosts(),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
        )
        sched = build_scheduler(conn, config, client=allow)
        names = set(sched.job_names())
        # Wave 2 + Wave 3: ~11 jobs total.
        assert names == {
            "leaderboard_scan", "ingest_history", "profile_wallets", "monitor",
            "reconcile", "resolve_markets", "pnl", "reviews", "health", "daily_report",
            "weekly_report", "rule_eval",
        }
        await allow.aclose()
    finally:
        await client.aclose()
        await conn.close()


@pytest.mark.asyncio
async def test_health_includes_scheduler_jobs(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})
    conn = await dbmod.init_db(config.db_path, _migrations_dir())
    from ..http import AllowlistClient
    allow = AllowlistClient(
        config.allowed_hosts(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
    )
    sched = build_scheduler(conn, config, client=allow)
    try:
        app = create_app(conn, config, scheduler=sched)
        with TestClient(app) as tc:
            body = tc.get("/api/polymarket/health").json()
            assert "monitor" in body["scheduler_jobs"]
            assert "reconcile" in body["scheduler_jobs"]
    finally:
        await allow.aclose()
        await conn.close()
