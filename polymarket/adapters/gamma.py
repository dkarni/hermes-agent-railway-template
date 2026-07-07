"""Gamma API adapter: market & event metadata (DESIGN.md sec 1.5).

  GET /markets?condition_ids=0x...  (also ?slug=, ?closed=false&limit=)

clobTokenIds, outcomes and outcomePrices arrive as JSON-encoded strings nested
inside the JSON response and must be decoded before mapping.
"""

from __future__ import annotations

import json
from decimal import Decimal

from ..http import AllowlistClient
from .models import Market, to_decimal

GAMMA_BATCH_SIZE = 20


def _decode_json_list(value: object) -> list:
    """Fields like clobTokenIds arrive as a JSON-encoded string list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _first_event(row: dict) -> dict:
    events = row.get("events")
    if isinstance(events, list) and events:
        return events[0] if isinstance(events[0], dict) else {}
    return {}


def parse_market(row: dict) -> Market:
    outcomes = tuple(str(o) for o in _decode_json_list(row.get("outcomes")))
    prices = tuple(to_decimal(p) for p in _decode_json_list(row.get("outcomePrices")))
    token_ids = tuple(str(t) for t in _decode_json_list(row.get("clobTokenIds")))

    yes_asset = token_ids[0] if len(token_ids) >= 1 else None
    no_asset = token_ids[1] if len(token_ids) >= 2 else None

    event = _first_event(row)
    liquidity_raw = row.get("liquidity")
    liquidity = to_decimal(liquidity_raw) if liquidity_raw not in (None, "") else None

    closed = bool(row.get("closed", False))
    # Gamma marks resolution via umaResolutionStatus / closed + a resolved flag
    # (field naming varies); treat an explicit winner or resolved flag as resolved.
    resolved = bool(row.get("resolved", False)) or (
        closed and row.get("umaResolutionStatus") == "resolved"
    )

    return Market(
        market_id=str(row.get("id") or row.get("conditionId") or ""),
        condition_id=str(row.get("conditionId") or ""),
        event_id=str(event.get("id") or row.get("eventId") or ""),
        question=str(row.get("question") or ""),
        event_title=str(event.get("title") or row.get("eventTitle") or ""),
        category=str(row.get("category") or event.get("category") or ""),
        slug=str(row.get("slug") or ""),
        event_slug=str(event.get("slug") or row.get("eventSlug") or ""),
        yes_asset_id=yes_asset,
        no_asset_id=no_asset,
        outcomes=outcomes,
        outcome_prices=prices,
        clob_token_ids=token_ids,
        end_date=str(row.get("endDate") or ""),
        closed=closed,
        resolved=resolved,
        liquidity=liquidity,
        raw=row,
    )


class GammaAdapter:
    def __init__(self, client: AllowlistClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def _get_markets(self, params: dict[str, object]) -> list[Market]:
        data = await self._client.get_json(f"{self._base}/markets", params=params)
        rows = data if isinstance(data, list) else data.get("data", [])
        return [parse_market(row) for row in rows]

    async def get_markets_by_condition_ids(self, condition_ids: list[str]) -> list[Market]:
        """Fetch markets by condition id in batches. Deduplicates by market_id."""
        out: dict[str, Market] = {}
        for start in range(0, len(condition_ids), GAMMA_BATCH_SIZE):
            batch = condition_ids[start : start + GAMMA_BATCH_SIZE]
            # gamma accepts repeated condition_ids params — but silently
            # EXCLUDES closed markets unless closed=true is passed (verified
            # live 2026-07-07: a resolved market returns [] without the flag).
            # Query both states so resolved history markets are not lost.
            for closed in ("false", "true"):
                markets = await self._get_markets({"condition_ids": batch, "closed": closed})
                for market in markets:
                    out[market.market_id] = market
        return list(out.values())

    async def get_market_by_slug(self, slug: str) -> Market | None:
        markets = await self._get_markets({"slug": slug})
        return markets[0] if markets else None


    async def get_event_tags(self, event_ids: list[str]) -> dict[str, list[str]]:
        """Tag labels per event id via /events?id=... (batched). The events
        embedded in /markets responses carry no tags; the category lives in the
        event's tag list (first label, e.g. "Sports")."""
        out: dict[str, list[str]] = {}
        for start in range(0, len(event_ids), GAMMA_BATCH_SIZE):
            batch = [e for e in event_ids[start : start + GAMMA_BATCH_SIZE] if e]
            if not batch:
                continue
            payload = await self._client.get_json(f"{self._base}/events", params={"id": batch})
            for row in payload if isinstance(payload, list) else []:
                labels = [
                    str(t.get("label"))
                    for t in (row.get("tags") or [])
                    if isinstance(t, dict) and t.get("label")
                ]
                out[str(row.get("id"))] = labels
        return out
