"""Job run wrapper: records job_runs rows, per-name locks, records counts.

A JobContext is passed to the coroutine so it can bump record counts and record
data_quality_events. Exceptions are captured into job_runs.error_json; the same
job_name never runs concurrently (per-name asyncio.Lock).
"""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Awaitable, Callable

import aiosqlite

from ..db import json_or_none, utcnow_iso

_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(job_name: str) -> asyncio.Lock:
    lock = _LOCKS.get(job_name)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[job_name] = lock
    return lock


class JobContext:
    """Passed to a job coroutine to record progress and data-quality events."""

    def __init__(self, conn: aiosqlite.Connection, job_run_id: int, job_name: str) -> None:
        self.conn = conn
        self.job_run_id = job_run_id
        self.job_name = job_name
        self.records_read = 0
        self.records_written = 0
        self.records_skipped = 0
        self.metadata: dict = {}

    def read(self, n: int = 1) -> None:
        self.records_read += n

    def written(self, n: int = 1) -> None:
        self.records_written += n

    def skipped(self, n: int = 1) -> None:
        self.records_skipped += n

    async def add_data_quality_event(
        self,
        *,
        severity: str,
        event_type: str,
        source: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO data_quality_events
                (severity, source, event_type, entity_type, entity_id, detected_at, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                severity,
                source,
                event_type,
                entity_type,
                entity_id,
                utcnow_iso(),
                json_or_none(details),
            ),
        )
        await self.conn.commit()


async def run_job(
    conn: aiosqlite.Connection,
    name: str,
    coro_fn: Callable[[JobContext], Awaitable[object]],
    *,
    trigger_type: str = "manual",
) -> int:
    """Run coro_fn under a job_runs record and a per-name lock. Returns run id."""
    lock = _lock_for(name)
    async with lock:
        started = utcnow_iso()
        cursor = await conn.execute(
            """
            INSERT INTO job_runs (job_name, trigger_type, started_at, status, lock_key)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (name, trigger_type, started, name),
        )
        await conn.commit()
        job_run_id = cursor.lastrowid
        ctx = JobContext(conn, job_run_id, name)

        status = "success"
        error_json: str | None = None
        try:
            result = await coro_fn(ctx)
            if isinstance(result, dict):
                ctx.metadata.update(result)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            status = "error"
            error_json = json.dumps(
                {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            )

        await conn.execute(
            """
            UPDATE job_runs
               SET finished_at = ?, status = ?, records_read = ?, records_written = ?,
                   records_skipped = ?, error_json = ?, metadata_json = ?
             WHERE id = ?
            """,
            (
                utcnow_iso(),
                status,
                ctx.records_read,
                ctx.records_written,
                ctx.records_skipped,
                error_json,
                json_or_none(ctx.metadata) if ctx.metadata else None,
                job_run_id,
            ),
        )
        await conn.commit()
        return job_run_id
