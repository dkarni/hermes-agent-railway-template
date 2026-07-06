"""Frozen dataclasses for normalized Polymarket data (DESIGN.md sec 1.5).

Normalize at the boundary; keep the raw JSON alongside for debugging. Money and
prices are Decimal here (converted to micro-units only when persisted). API
floats are parsed via Decimal(str(x)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


def to_decimal(value: object) -> Decimal:
    """Parse an API numeric (float/int/str) into an exact Decimal."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class LeaderboardEntry:
    wallet_address: str
    rank: int | None
    pnl: Decimal
    volume: Decimal
    user_name: str
    x_username: str
    verified_badge: bool
    profile_image: str
    category: str
    time_period: str
    order_by: str
    raw: dict


@dataclass(frozen=True)
class WalletTrade:
    wallet_address: str
    side: str                 # BUY | SELL
    asset_id: str
    condition_id: str
    size: Decimal
    price: Decimal
    timestamp: int            # unix seconds
    title: str
    slug: str
    event_slug: str
    outcome: str
    outcome_index: int | None
    transaction_hash: str
    raw: dict


@dataclass(frozen=True)
class Market:
    market_id: str
    condition_id: str
    event_id: str
    question: str
    event_title: str
    category: str
    slug: str
    event_slug: str
    yes_asset_id: str | None
    no_asset_id: str | None
    outcomes: tuple[str, ...]
    outcome_prices: tuple[Decimal, ...]
    clob_token_ids: tuple[str, ...]
    end_date: str
    closed: bool
    resolved: bool
    liquidity: Decimal | None
    raw: dict


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    asset_id: str
    market: str
    timestamp: str
    bids: tuple[OrderBookLevel, ...]   # normalized best-first (highest price first)
    asks: tuple[OrderBookLevel, ...]   # normalized best-first (lowest price first)
    raw: dict

    @property
    def best_bid(self) -> OrderBookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price


@dataclass(frozen=True)
class FillEstimate:
    """Result of walking the ask ladder for a BUY of a target USD size."""
    filled_shares: Decimal
    filled_usd: Decimal
    avg_price: Decimal | None       # weighted average fill price, None if nothing filled
    fully_filled: bool
    stopped_reason: str             # "complete" | "slippage_limit" | "book_exhausted" | "empty_book"
