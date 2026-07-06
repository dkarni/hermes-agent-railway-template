"""CLOB adapter: order book and midpoint (DESIGN.md sec 1.5).

  GET /book?token_id=<asset>
  GET /midpoint?token_id=<asset>

CRITICAL ordering: the live API returns asks sorted DESCENDING by price and bids
ASCENDING, so best ask / best bid are the LAST elements. This adapter normalizes
to best-first (asks ascending, bids descending) and is unit-tested for it.
"""

from __future__ import annotations

from decimal import Decimal

from ..db import px_to_micro, usd_to_micro
from ..http import AllowlistClient
from .models import FillEstimate, OrderBook, OrderBookLevel, to_decimal


def _parse_levels(raw_levels: object) -> list[OrderBookLevel]:
    levels: list[OrderBookLevel] = []
    if isinstance(raw_levels, list):
        for item in raw_levels:
            if not isinstance(item, dict):
                continue
            levels.append(
                OrderBookLevel(
                    price=to_decimal(item.get("price", 0)),
                    size=to_decimal(item.get("size", 0)),
                )
            )
    return levels


def normalize_book(raw: dict) -> OrderBook:
    """Normalize raw /book response to best-first ordering.

    Asks -> ascending price (best/lowest first); bids -> descending price
    (best/highest first). We sort explicitly rather than relying on the API's
    documented worst-to-best ordering so the invariant holds even if it changes.
    """
    asks = sorted(_parse_levels(raw.get("asks")), key=lambda lvl: lvl.price)
    bids = sorted(_parse_levels(raw.get("bids")), key=lambda lvl: lvl.price, reverse=True)
    return OrderBook(
        asset_id=str(raw.get("asset_id") or ""),
        market=str(raw.get("market") or ""),
        timestamp=str(raw.get("timestamp") or ""),
        bids=tuple(bids),
        asks=tuple(asks),
        raw=raw,
    )


def estimate_fill(
    book: OrderBook,
    *,
    side: str = "BUY",
    usd_amount_micro: int,
    slippage_limit: Decimal | None = None,
) -> FillEstimate:
    """Walk the ask ladder (best-first) to fill a target USD amount for a BUY.

    slippage_limit is an absolute price ceiling relative to best ask: any ask
    priced more than slippage_limit above the best ask stops the fill (partial).
    Returns weighted average fill price, filled shares and filled USD.
    """
    if side.upper() != "BUY":
        raise ValueError("estimate_fill only supports BUY in version one")

    target_usd = Decimal(usd_amount_micro) / Decimal(1_000_000)
    if not book.asks:
        return FillEstimate(Decimal(0), Decimal(0), None, False, "empty_book")

    best_ask = book.asks[0].price
    price_ceiling = None if slippage_limit is None else best_ask + slippage_limit

    remaining_usd = target_usd
    filled_shares = Decimal(0)
    filled_usd = Decimal(0)
    reason = "book_exhausted"

    for level in book.asks:
        if remaining_usd <= 0:
            reason = "complete"
            break
        if price_ceiling is not None and level.price > price_ceiling:
            reason = "slippage_limit"
            break
        level_usd_capacity = level.price * level.size
        take_usd = min(remaining_usd, level_usd_capacity)
        take_shares = (take_usd / level.price) if level.price > 0 else Decimal(0)
        filled_shares += take_shares
        filled_usd += take_usd
        remaining_usd -= take_usd
    else:
        reason = "complete" if remaining_usd <= 0 else "book_exhausted"

    if remaining_usd <= 0:
        reason = "complete"

    fully_filled = remaining_usd <= 0
    avg_price = (filled_usd / filled_shares) if filled_shares > 0 else None
    return FillEstimate(filled_shares, filled_usd, avg_price, fully_filled, reason)


class ClobAdapter:
    def __init__(self, client: AllowlistClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def get_book(self, token_id: str) -> OrderBook:
        data = await self._client.get_json(f"{self._base}/book", params={"token_id": token_id})
        return normalize_book(data if isinstance(data, dict) else {})

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        data = await self._client.get_json(f"{self._base}/midpoint", params={"token_id": token_id})
        if isinstance(data, dict) and data.get("mid") not in (None, ""):
            return to_decimal(data["mid"])
        return None
