"""Decision-quality labelling (PRD sec 15.3).

Pure functions: given a decision and the resolved/checkpoint facts, compute the
hypothetical PnL under the SAME execution model (decision-time executable price)
and assign a label. Unjudgeable => not eligible for learning.

Labels (§15.3):
  good_copy / bad_copy         — copied and (un)profitable
  good_skip / missed_winner    — skipped and would have lost / would have won
  good_watch / missed_watch_entry — watch avoided bad entry / offered a later entry
  unjudgeable                  — missing data / unresolved / no executable price
"""

from __future__ import annotations

from decimal import Decimal

ZERO = Decimal(0)

GOOD_COPY = "good_copy"
BAD_COPY = "bad_copy"
GOOD_SKIP = "good_skip"
MISSED_WINNER = "missed_winner"
GOOD_WATCH = "good_watch"
MISSED_WATCH_ENTRY = "missed_watch_entry"
UNJUDGEABLE = "unjudgeable"


def hypothetical_pnl(
    *, executable_entry_price: Decimal | None, size_usd: Decimal, won: bool | None
) -> Decimal | None:
    """PnL a copy at decision-time executable price would have realized at resolution.

    shares = size / entry_price; settlement 1/0. None when we cannot judge
    (missing entry price, unresolved, or non-positive size).
    """
    if executable_entry_price is None or executable_entry_price <= 0 or size_usd <= 0 or won is None:
        return None
    shares = size_usd / executable_entry_price
    settlement = shares if won else ZERO
    return settlement - size_usd


def label_final(
    *,
    decision: str,
    executable_entry_price: Decimal | None,
    hypo_size_usd: Decimal,
    won: bool | None,
    actual_realized: Decimal | None,
) -> tuple[str, bool]:
    """Return (label, eligible_for_learning) at final resolution.

    ``actual_realized`` is the real paper trade pnl when a copy was opened;
    otherwise the hypothetical is used to judge skip/watch counterfactuals.
    """
    if won is None:
        return UNJUDGEABLE, False

    if decision == "paper_copy":
        if actual_realized is None:
            # copied but no paper trade opened (size shrank) — judge hypothetically
            hypo = hypothetical_pnl(
                executable_entry_price=executable_entry_price, size_usd=hypo_size_usd, won=won
            )
            if hypo is None:
                return UNJUDGEABLE, False
            return (GOOD_COPY if hypo > 0 else BAD_COPY), True
        return (GOOD_COPY if actual_realized > 0 else BAD_COPY), True

    hypo = hypothetical_pnl(
        executable_entry_price=executable_entry_price, size_usd=hypo_size_usd, won=won
    )
    if hypo is None:
        return UNJUDGEABLE, False

    if decision == "skip":
        return (MISSED_WINNER if hypo > 0 else GOOD_SKIP), True
    if decision == "watchlist":
        # A watch that would have won at decision-time entry is a missed entry;
        # otherwise the watch correctly avoided a losing immediate entry.
        return (MISSED_WATCH_ENTRY if hypo > 0 else GOOD_WATCH), True
    return UNJUDGEABLE, False


def label_checkpoint(
    *,
    decision: str,
    executable_entry_price: Decimal | None,
    checkpoint_price: Decimal | None,
    hypo_size_usd: Decimal,
) -> tuple[str, bool]:
    """Interim (1h/6h/24h) label from price movement, not final resolution.

    Uses the checkpoint mark as a proxy exit for the counterfactual. Interim
    reviews are informational and excluded from learning (final drives learning).
    """
    if executable_entry_price is None or checkpoint_price is None or executable_entry_price <= 0:
        return UNJUDGEABLE, False
    if hypo_size_usd <= 0:
        return UNJUDGEABLE, False
    shares = hypo_size_usd / executable_entry_price
    mark_value = shares * checkpoint_price
    interim = mark_value - hypo_size_usd
    if decision == "paper_copy":
        return (GOOD_COPY if interim > 0 else BAD_COPY), False
    if decision == "skip":
        return (MISSED_WINNER if interim > 0 else GOOD_SKIP), False
    return (MISSED_WATCH_ENTRY if interim > 0 else GOOD_WATCH), False
