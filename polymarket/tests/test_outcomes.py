"""Wave 3 outcome-labelling unit tests (PRD sec 15.3).

Each label is exercised, including good_skip vs missed_winner under the IDENTICAL
execution model (decision-time executable price), and the unjudgeable path which
must set eligible_for_learning=0.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain import outcomes


PX = Decimal("0.50")   # decision-time executable entry price
SIZE = Decimal("10")   # hypothetical size


def test_hypothetical_pnl_win_and_loss():
    win = outcomes.hypothetical_pnl(executable_entry_price=PX, size_usd=SIZE, won=True)
    loss = outcomes.hypothetical_pnl(executable_entry_price=PX, size_usd=SIZE, won=False)
    assert win == Decimal("10")   # 20 shares * 1 - 10
    assert loss == Decimal("-10")


def test_hypothetical_pnl_none_paths():
    assert outcomes.hypothetical_pnl(executable_entry_price=None, size_usd=SIZE, won=True) is None
    assert outcomes.hypothetical_pnl(executable_entry_price=PX, size_usd=SIZE, won=None) is None
    assert outcomes.hypothetical_pnl(executable_entry_price=Decimal("0"), size_usd=SIZE, won=True) is None


def test_label_good_copy_and_bad_copy_from_actual():
    good, elig = outcomes.label_final(
        decision="paper_copy", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=True, actual_realized=Decimal("5"),
    )
    assert (good, elig) == (outcomes.GOOD_COPY, True)
    bad, elig = outcomes.label_final(
        decision="paper_copy", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=False, actual_realized=Decimal("-5"),
    )
    assert (bad, elig) == (outcomes.BAD_COPY, True)


def test_good_skip_vs_missed_winner_same_execution_model():
    # Skip that would have LOST -> good_skip; skip that would have WON -> missed_winner.
    # Both use hypothetical_pnl at the SAME executable entry price.
    good_skip, elig1 = outcomes.label_final(
        decision="skip", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=False, actual_realized=None,
    )
    missed, elig2 = outcomes.label_final(
        decision="skip", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=True, actual_realized=None,
    )
    assert (good_skip, elig1) == (outcomes.GOOD_SKIP, True)
    assert (missed, elig2) == (outcomes.MISSED_WINNER, True)


def test_watch_labels():
    missed_entry, _ = outcomes.label_final(
        decision="watchlist", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=True, actual_realized=None,
    )
    good_watch, _ = outcomes.label_final(
        decision="watchlist", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=False, actual_realized=None,
    )
    assert missed_entry == outcomes.MISSED_WATCH_ENTRY
    assert good_watch == outcomes.GOOD_WATCH


def test_unjudgeable_when_unresolved_sets_not_eligible():
    label, elig = outcomes.label_final(
        decision="skip", executable_entry_price=PX, hypo_size_usd=SIZE,
        won=None, actual_realized=None,
    )
    assert label == outcomes.UNJUDGEABLE
    assert elig is False


def test_unjudgeable_when_no_executable_price():
    label, elig = outcomes.label_final(
        decision="skip", executable_entry_price=None, hypo_size_usd=SIZE,
        won=True, actual_realized=None,
    )
    assert label == outcomes.UNJUDGEABLE
    assert elig is False


def test_checkpoint_labels_never_eligible():
    label, elig = outcomes.label_checkpoint(
        decision="paper_copy", executable_entry_price=PX,
        checkpoint_price=Decimal("0.60"), hypo_size_usd=SIZE,
    )
    assert label == outcomes.GOOD_COPY
    assert elig is False  # interim reviews are informational only
