"""Wave 3 benchmark unit tests (PRD sec 16).

Blind uses the same book snapshot (no optimistic fill); cohort_metrics profit
factor / win rate / max_drawdown; compare refuses an edge claim below the min
sample.
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.models import OrderBook, OrderBookLevel
from ..domain import benchmarks as bm
from ..domain import paper


def _book(asks):
    return OrderBook(
        asset_id="a", market="m", timestamp="1700000000000",
        asks=tuple(OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in asks),
        bids=(), raw={},
    )


def test_blind_shares_identical_snapshot_and_fill():
    book = _book([("0.40", "1000")])
    blind = bm.simulate_blind_entry(book, slippage_limit=Decimal("0.05"))
    filtered = paper.simulate_entry(book, target_usd=Decimal("10"), slippage_limit=Decimal("0.05"))
    # Same avg price from the same ladder — no optimistic (midpoint) fill.
    assert blind.avg_price == filtered.avg_price == Decimal("0.40")
    assert blind.cost_usd == Decimal("10")  # fixed $10 blind size


def test_blind_no_book_no_fabricated_fill():
    blind = bm.simulate_blind_entry(None, slippage_limit=Decimal("0.05"))
    assert blind.filled is False
    assert blind.avg_price is None


def test_cohort_metrics_profit_factor_win_rate():
    trades = [
        {"cost": Decimal("10"), "realized_pnl": Decimal("6"), "won": True},
        {"cost": Decimal("10"), "realized_pnl": Decimal("4"), "won": True},
        {"cost": Decimal("10"), "realized_pnl": Decimal("-5"), "won": False},
    ]
    m = bm.cohort_metrics(trades)
    assert m["sample"] == 3
    assert m["net_pnl"] == Decimal("5")
    assert m["wins"] == 2 and m["losses"] == 1
    assert m["win_rate"] == Decimal("2") / Decimal("3")
    # gross profit 10 / gross loss 5 = 2.
    assert m["profit_factor"] == Decimal("2")


def test_cohort_metrics_empty_and_no_loss_profit_factor_none():
    assert bm.cohort_metrics([])["profit_factor"] is None
    only_wins = [{"cost": Decimal("10"), "realized_pnl": Decimal("3"), "won": True}]
    assert bm.cohort_metrics(only_wins)["profit_factor"] is None  # no losses


def test_max_drawdown():
    # cumulative: +5, +9, +2, +7 -> peak 9, trough 2 -> dd 7.
    seq = [Decimal("5"), Decimal("4"), Decimal("-7"), Decimal("5")]
    assert bm.max_drawdown(seq) == Decimal("7")


def test_compare_refuses_edge_below_min_sample():
    filtered = bm.cohort_metrics([{"cost": Decimal("10"), "realized_pnl": Decimal("50"), "won": True}])
    blind = bm.cohort_metrics([{"cost": Decimal("10"), "realized_pnl": Decimal("-5"), "won": False}])
    result = bm.compare(filtered, blind, min_sample=20)
    assert result["sufficient_sample"] is False
    assert result["verdict"] == "insufficient_sample"


def test_compare_declares_filtered_better_with_enough_sample():
    filtered_rows = [{"cost": Decimal("10"), "realized_pnl": Decimal("2"), "won": True} for _ in range(20)]
    blind_rows = [{"cost": Decimal("10"), "realized_pnl": Decimal("-1"), "won": False} for _ in range(20)]
    result = bm.compare(bm.cohort_metrics(filtered_rows), bm.cohort_metrics(blind_rows), min_sample=20)
    assert result["sufficient_sample"] is True
    assert result["verdict"] == "filtered_better"
    assert result["filtered_minus_blind_pnl"] == Decimal("60")  # 40 - (-20)
