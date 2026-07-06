from __future__ import annotations

import asyncio
import json
import os

import pytest

from .. import db as dbmod
from ..jobs.runner import JobContext, run_job


def _migrations_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


async def _fresh_db(tmp_path):
    return await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())


@pytest.mark.asyncio
async def test_run_job_records_success(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        async def work(ctx: JobContext):
            ctx.read(3)
            ctx.written(2)
            ctx.skipped(1)
            return {"note": "done"}

        run_id = await run_job(conn, "demo_job", work, trigger_type="manual")
        cursor = await conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        row = dict(await cursor.fetchone())
        assert row["status"] == "success"
        assert row["records_read"] == 3
        assert row["records_written"] == 2
        assert row["records_skipped"] == 1
        assert row["finished_at"] is not None
        assert json.loads(row["metadata_json"])["note"] == "done"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_job_records_error(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        async def work(ctx: JobContext):
            raise ValueError("boom")

        run_id = await run_job(conn, "failing_job", work)
        cursor = await conn.execute("SELECT status, error_json FROM job_runs WHERE id = ?", (run_id,))
        row = dict(await cursor.fetchone())
        assert row["status"] == "error"
        assert json.loads(row["error_json"])["type"] == "ValueError"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_data_quality_event_helper(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        async def work(ctx: JobContext):
            await ctx.add_data_quality_event(
                severity="warning", event_type="test_event", details={"k": 1}
            )

        await run_job(conn, "dq_job", work)
        cursor = await conn.execute("SELECT COUNT(*) FROM data_quality_events WHERE event_type='test_event'")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_same_job_name_serialized(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        order: list[str] = []

        async def work(ctx: JobContext):
            order.append(f"start-{ctx.job_run_id}")
            await asyncio.sleep(0.05)
            order.append(f"end-{ctx.job_run_id}")

        await asyncio.gather(
            run_job(conn, "serial_job", work),
            run_job(conn, "serial_job", work),
        )
        # per-name lock => no interleaving: each start is immediately followed by its end
        assert order[0].startswith("start-") and order[1].startswith("end-")
        assert order[2].startswith("start-") and order[3].startswith("end-")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_zombie_runs_aborted_at_startup(tmp_path):
    """job_runs left 'running' by a container restart are closed on boot."""
    from .. import db as dbmod
    from ..worker import _abort_zombie_runs

    from ..config import load_config

    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(str(tmp_path / "z.db"), config.migrations_dir)
    await conn.execute(
        "INSERT INTO job_runs (job_name, trigger_type, started_at, status, lock_key) "
        "VALUES ('ingest_history', 'scheduled', '2026-07-06T00:00:00Z', 'running', 'ingest_history')"
    )
    await conn.commit()
    n = await _abort_zombie_runs(conn)
    assert n == 1
    cur = await conn.execute("SELECT status, finished_at FROM job_runs")
    status, finished = await cur.fetchone()
    assert status == "aborted" and finished is not None
    await conn.close()
