"""Dashboard alerts with dedupe + quiet-period suppression (PRD sec 21.2).

Delivery is dashboard-only (no Telegram/network): every alert is stored in the
``alerts`` table with ``delivery_channel='dashboard'`` and, when it is not
suppressed, ``delivery_status='stored'``. Suppressed duplicates (same
``dedupe_key`` within the quiet period) are stored with
``delivery_status='suppressed'`` so the health page can still show that the
condition recurred without spamming the operator.

The quiet period defaults to 6h (PRD 21.2 "deduplication and quiet periods").
Pure suppression logic (``is_within_quiet_period``) is unit-tested separately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite

from ..db import json_or_none, utcnow_iso

QUIET_PERIOD_SECONDS = 6 * 3600  # PRD 21.2 default quiet window


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_within_quiet_period(
    last_sent_iso: str | None, now_iso: str, *, quiet_seconds: int = QUIET_PERIOD_SECONDS
) -> bool:
    """True when ``last_sent_iso`` is within ``quiet_seconds`` before ``now_iso``.

    None (never sent) => not within a quiet period. Pure and unit-tested.
    """
    if not last_sent_iso:
        return False
    delta = _parse_iso(now_iso) - _parse_iso(last_sent_iso)
    return timedelta(0) <= delta < timedelta(seconds=quiet_seconds)


async def _last_stored_at(conn: aiosqlite.Connection, dedupe_key: str) -> str | None:
    """Most recent sent_at for a non-suppressed alert with this dedupe key."""
    cur = await conn.execute(
        """
        SELECT sent_at FROM alerts
         WHERE dedupe_key = ? AND delivery_status = 'stored'
         ORDER BY sent_at DESC LIMIT 1
        """,
        (dedupe_key,),
    )
    row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def alert(
    conn: aiosqlite.Connection,
    *,
    type: str,
    severity: str,
    dedupe_key: str,
    message: str,
    metadata: dict | None = None,
    quiet_seconds: int = QUIET_PERIOD_SECONDS,
    now_iso: str | None = None,
) -> dict:
    """Store a dashboard alert, suppressing duplicates within the quiet period.

    Returns {'stored': bool, 'suppressed': bool, 'alert_id': int}. Never sends
    over the network. Idempotent-ish: repeated identical calls inside the quiet
    window are recorded as 'suppressed' rows (audit trail) rather than dropped.
    """
    now = now_iso or utcnow_iso()
    last = await _last_stored_at(conn, dedupe_key)
    suppressed = is_within_quiet_period(last, now, quiet_seconds=quiet_seconds)
    status = "suppressed" if suppressed else "stored"
    cur = await conn.execute(
        """
        INSERT INTO alerts
            (type, severity, dedupe_key, message, sent_at, delivery_channel,
             delivery_status, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, 'dashboard', ?, ?, ?)
        """,
        (
            type,
            severity,
            dedupe_key,
            message,
            now if not suppressed else None,
            status,
            json_or_none(metadata),
            now,
        ),
    )
    await conn.commit()
    return {"stored": not suppressed, "suppressed": suppressed, "alert_id": int(cur.lastrowid)}
