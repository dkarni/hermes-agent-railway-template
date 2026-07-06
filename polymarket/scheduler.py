"""In-process asyncio scheduler (DESIGN.md sec 2, PRD sec 9).

Each registered job runs on an interval (``every_seconds``) or a daily wall-clock
time (``daily_at``), with a staggered start and per-run jitter. Every run goes
through jobs.runner.run_job, which records job_runs and holds a per-name lock so
the same job never overlaps itself.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .jobs.runner import run_job

log = logging.getLogger("polymarket.scheduler")

JobFactory = Callable[[], Awaitable[object]]  # returns a coro_fn(ctx)-compatible callable
# In practice we register a coroutine *factory* that takes a JobContext.
CoroFn = Callable[[object], Awaitable[object]]


@dataclass
class ScheduledJob:
    name: str
    coro_fn: CoroFn
    every_seconds: int | None = None
    daily_at: str | None = None            # "HH:MM"
    stagger_seconds: float = 0.0
    jitter_seconds: float = 0.0
    _task: asyncio.Task | None = field(default=None, repr=False)


class Scheduler:
    def __init__(self, conn, *, timezone: str = "UTC") -> None:
        self._conn = conn
        self._tz = ZoneInfo(timezone)
        self._jobs: list[ScheduledJob] = []
        self._running = False

    def register(
        self,
        name: str,
        coro_fn: CoroFn,
        *,
        every_seconds: int | None = None,
        daily_at: str | None = None,
        stagger_seconds: float = 0.0,
        jitter_seconds: float = 0.0,
    ) -> None:
        if (every_seconds is None) == (daily_at is None):
            raise ValueError("register exactly one of every_seconds or daily_at")
        self._jobs.append(
            ScheduledJob(
                name=name,
                coro_fn=coro_fn,
                every_seconds=every_seconds,
                daily_at=daily_at,
                stagger_seconds=stagger_seconds,
                jitter_seconds=jitter_seconds,
            )
        )

    def job_names(self) -> list[str]:
        return [j.name for j in self._jobs]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for job in self._jobs:
            job._task = asyncio.create_task(self._run_loop(job), name=f"poly-sched-{job.name}")

    async def stop(self) -> None:
        self._running = False
        for job in self._jobs:
            if job._task is not None:
                job._task.cancel()
        for job in self._jobs:
            if job._task is not None:
                try:
                    await job._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _run_once(self, job: ScheduledJob) -> None:
        try:
            await run_job(self._conn, job.name, job.coro_fn, trigger_type="scheduled")
        except Exception:  # noqa: BLE001 - runner records; never kill the loop
            log.exception("scheduler run failed for %s", job.name)

    async def _run_loop(self, job: ScheduledJob) -> None:
        if job.stagger_seconds:
            await asyncio.sleep(job.stagger_seconds)
        while self._running:
            if job.every_seconds is not None:
                await self._run_once(job)
                delay = job.every_seconds + random.uniform(0, job.jitter_seconds)
                await asyncio.sleep(delay)
            else:
                delay = self._seconds_until_daily(job.daily_at)  # type: ignore[arg-type]
                await asyncio.sleep(delay + random.uniform(0, job.jitter_seconds))
                if self._running:
                    await self._run_once(job)

    def _seconds_until_daily(self, hhmm: str) -> float:
        now = datetime.now(self._tz)
        hh, mm = (int(x) for x in hhmm.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
