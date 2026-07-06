"""Pure paper-portfolio & execution math (PRD sec 14).

Accounting model decisions (documented, audited):
  * shares are stored as INTEGER micro-shares (x 1_000_000). A binary outcome
    token bought for cost ``c`` at avg price ``p`` yields ``shares = c / p``.
  * money & prices are INTEGER micro-units everywhere (see db.py helpers).
  * fees are ZERO. Polymarket's CLOB charges no trading fee, so entry_fee /
    exit_fee are always 0 (columns kept for schema completeness / future).
  * a long position is marked at the executable exit price = best bid (never the
    optimistic midpoint). Missing/stale books never fabricate a mark.

This module is pure: it takes primitive/Decimal inputs and returns dataclasses.
All DB reads/writes and the ledger transaction live in jobs/paper_exec.py and the
DbPortfolioView (jobs/portfolio_view.py). The fill walk itself reuses
adapters.clob.estimate_fill so the blind benchmark uses the identical model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from ..adapters.clob import estimate_fill
from ..adapters.models import FillEstimate, OrderBook
from ..db import usd_to_micro

ZERO = Decimal(0)
MIN_POSITION_USD = Decimal("1")  # below this an entry converts to watch


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- sizing / exposure shrink (PRD 14.2) ------------------------------------

@dataclass(frozen=True)
class SizeInputs:
    """Everything needed to shrink a tier size to the binding limit."""
    tier_usd: Decimal
    available_cash: Decimal
    max_position_usd: Decimal
    wallet_headroom: Decimal
    category_headroom: Decimal
    event_headroom: Decimal


@dataclass(frozen=True)
class SizeResult:
    size_usd: Decimal
    binding: str  # which limit set the size


def shrink_size(inputs: SizeInputs) -> SizeResult:
    """Smallest of tier / cash / per-position / wallet / category / event caps.

    Book-supported size is applied afterwards in ``simulate_entry`` (it depends on
    the actual fill walk). Returns the binding constraint name for auditing.
    """
    candidates = [
        ("tier", inputs.tier_usd),
        ("cash", inputs.available_cash),
        ("max_position", inputs.max_position_usd),
        ("wallet_exposure", inputs.wallet_headroom),
        ("category_exposure", inputs.category_headroom),
        ("event_exposure", inputs.event_headroom),
    ]
    binding, size = min(candidates, key=lambda kv: kv[1])
    return SizeResult(max(ZERO, size), binding)


# --- entry simulation (PRD 14.4) --------------------------------------------

@dataclass(frozen=True)
class EntrySim:
    """Result of simulating a paper BUY entry against a book."""
    filled: bool                 # True if a position should be opened
    reason: str                  # audit reason (why filled / why converted to watch)
    shares: Decimal              # exact shares (cost / avg_price)
    cost_usd: Decimal            # actual USD spent (= filled_usd)
    avg_price: Decimal | None    # weighted average fill price
    best_ask: Decimal | None
    slippage: Decimal            # avg_price - best_ask (signed), ZERO if no fill
    fill: FillEstimate | None


def simulate_entry(
    book: OrderBook | None,
    *,
    target_usd: Decimal,
    slippage_limit: Decimal,
    min_position_usd: Decimal = MIN_POSITION_USD,
) -> EntrySim:
    """Walk the ask ladder for ``target_usd`` and produce an entry.

    Book-supported size is the intrinsic outcome of the walk: if slippage or the
    book depth reduces the fill below ``min_position_usd`` the entry does not
    open (caller converts the decision to watch). Missing/empty book => no fill.
    """
    if target_usd < min_position_usd:
        return EntrySim(False, "below_min_size", ZERO, ZERO, None, None, ZERO, None)
    if book is None or not book.asks:
        return EntrySim(False, "no_book", ZERO, ZERO, None, None, ZERO, None)

    best_ask = book.asks[0].price
    fill = estimate_fill(
        book,
        side="BUY",
        usd_amount_micro=usd_to_micro(target_usd),
        slippage_limit=slippage_limit,
    )
    if fill.filled_usd < min_position_usd or fill.avg_price is None or fill.filled_shares <= 0:
        return EntrySim(
            False,
            f"fill_below_min:{fill.stopped_reason}",
            ZERO, ZERO, fill.avg_price, best_ask, ZERO, fill,
        )
    slippage = fill.avg_price - best_ask
    reason = "filled_full" if fill.fully_filled else "filled_partial"
    return EntrySim(
        True, reason, fill.filled_shares, fill.filled_usd, fill.avg_price,
        best_ask, slippage, fill,
    )


# --- position accounting (PRD 14.5) -----------------------------------------

def unrealized_pnl(shares: Decimal, cost_usd: Decimal, exit_price: Decimal | None) -> Decimal | None:
    """shares * best_bid - cost. None when no executable mark is available.

    ``exit_price`` is the executable exit price (best bid). None => cannot mark;
    the caller carries the last mark and flags staleness rather than inventing 0.
    """
    if exit_price is None:
        return None
    return (shares * exit_price) - cost_usd


def settlement_pnl(shares: Decimal, cost_usd: Decimal, *, won: bool) -> tuple[Decimal, Decimal]:
    """At resolution: settlement value (1/share if won else 0) and realized pnl.

    Fees are zero (see module docstring), so realized = settlement - cost.
    Returns (settlement_value, realized_pnl).
    """
    settlement_value = shares if won else ZERO
    return settlement_value, settlement_value - cost_usd


def exit_pnl(shares: Decimal, cost_usd: Decimal, exit_price: Decimal) -> tuple[Decimal, Decimal]:
    """Close at an executable exit price (best bid). Returns (proceeds, realized)."""
    proceeds = shares * exit_price
    return proceeds, proceeds - cost_usd
