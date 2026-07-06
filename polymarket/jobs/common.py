"""Shared job helpers: idempotency keys, market upsert, rule-set loading.

Jobs orchestrate I/O (sqlite + adapters) and delegate all math to domain/.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

import aiosqlite

from ..adapters.models import Market, WalletTrade
from ..db import (
    json_or_none,
    micro_to_px,
    px_to_micro,
    usd_to_micro,
    utcnow_iso,
)


def observed_idempotency_key(
    *,
    source: str,
    wallet: str,
    transaction_hash: str,
    asset_id: str,
    side: str,
    price: Decimal,
    size: Decimal,
    timestamp: int,
) -> str:
    """PRD 12.2 idempotency key.

    Prefers ``source + wallet + txhash + asset + side + price + size``. When the
    transaction hash is missing, falls back to a deterministic sha256 of the
    normalized fields plus timestamp so re-detection stays idempotent.
    """
    price_s = format(price, "f")
    size_s = format(size, "f")
    if transaction_hash:
        return "|".join([source, wallet, transaction_hash, asset_id, side, price_s, size_s])
    raw = "|".join([source, wallet, asset_id, side, price_s, size_s, str(timestamp)])
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def load_active_rule_set(conn: aiosqlite.Connection, *, strategy: str = "default") -> tuple[int, int, dict]:
    """Return (rule_set_id, version, payload) for the active rule set."""
    cursor = await conn.execute(
        "SELECT id, version, parameters_json FROM rule_sets WHERE strategy = ? AND status = 'active'",
        (strategy,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("no active rule set")
    return int(row[0]), int(row[1]), json.loads(row[2])


async def upsert_market(conn: aiosqlite.Connection, market: Market) -> None:
    """Insert or update a markets row from a normalized Market."""
    winning_outcome = _winning_outcome(market)
    status = "resolved" if market.resolved else ("closed" if market.closed else "open")
    now = utcnow_iso()
    await conn.execute(
        """
        INSERT INTO markets (
            market_id, condition_id, event_id, question, event_title, category,
            source_category, slug, event_slug, yes_asset_id, no_asset_id,
            scheduled_resolution_at, resolved_at, winning_outcome, status,
            metadata_updated_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            condition_id=excluded.condition_id,
            event_id=excluded.event_id,
            question=excluded.question,
            event_title=excluded.event_title,
            category=excluded.category,
            source_category=excluded.source_category,
            slug=excluded.slug,
            event_slug=excluded.event_slug,
            yes_asset_id=excluded.yes_asset_id,
            no_asset_id=excluded.no_asset_id,
            scheduled_resolution_at=excluded.scheduled_resolution_at,
            resolved_at=excluded.resolved_at,
            winning_outcome=excluded.winning_outcome,
            status=excluded.status,
            metadata_updated_at=excluded.metadata_updated_at,
            raw_json=excluded.raw_json
        """,
        (
            market.market_id,
            market.condition_id,
            market.event_id or None,
            market.question or None,
            market.event_title or None,
            market.category or None,
            market.category or None,
            market.slug or None,
            market.event_slug or None,
            market.yes_asset_id,
            market.no_asset_id,
            market.end_date or None,
            now if market.resolved else None,
            winning_outcome,
            status,
            now,
            json_or_none(market.raw),
        ),
    )
    await conn.commit()


def _winning_outcome(market: Market) -> str | None:
    if not market.resolved:
        return None
    # winning outcome = the outcome whose price settled at 1 (or highest).
    if market.outcomes and market.outcome_prices and len(market.outcomes) == len(market.outcome_prices):
        best_idx = max(range(len(market.outcome_prices)), key=lambda i: market.outcome_prices[i])
        if market.outcome_prices[best_idx] >= Decimal("0.5"):
            return market.outcomes[best_idx]
    return None


def seconds_to_resolution(end_date_iso: str, *, now_ts: int) -> int | None:
    if not end_date_iso:
        return None
    try:
        end = _parse_iso(end_date_iso)
    except ValueError:
        return None
    return int(end.timestamp() - now_ts)


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())
