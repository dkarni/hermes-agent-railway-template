from __future__ import annotations

from decimal import Decimal

from ..domain.wallet_stats import (
    ResolvedMarket,
    StatTrade,
    compute_wallet_stats,
)


def _t(cond, outcome, side, price, size, ts, cat="CRYPTO"):
    return StatTrade(cond, outcome, side, Decimal(price), Decimal(size), ts, cat)


def test_realized_pnl_reconstruction_resolution():
    # A: buy 100 @0.5 (cost 50), A wins -> +50
    # B: buy 100 @0.4 (cost 40), B loses -> -40  => net +10
    trades = [
        _t("cA", "Yes", "BUY", "0.5", "100", 1000),
        _t("cB", "Yes", "BUY", "0.4", "100", 2000),
    ]
    resolved = {"cA": ResolvedMarket(True, "Yes"), "cB": ResolvedMarket(True, "No")}
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=3000)
    assert s.net_realized_pnl == Decimal("10.000000")
    assert s.gross_realized_pnl == Decimal("50.000000")
    assert s.capital_deployed == Decimal("90.000000")
    assert s.roi == Decimal("0.111111")
    assert s.resolved_trade_count == 2
    assert s.resolved_wins == 1
    assert s.win_rate == Decimal("0.500000")


def test_sell_realizes_against_average_cost():
    # buy 100 @0.4, sell 50 @0.6 -> realized 50*(0.6-0.4)=10
    trades = [
        _t("cA", "Yes", "BUY", "0.4", "100", 1000),
        _t("cA", "Yes", "SELL", "0.6", "50", 2000),
    ]
    s = compute_wallet_stats(trades, {}, window_days=30, window_end_ts=3000)
    assert s.net_realized_pnl == Decimal("10.000000")


def test_sell_larger_than_position_is_clamped_no_short():
    # buy 50, sell 200 -> only 50 realized; no negative position.
    trades = [
        _t("cA", "Yes", "BUY", "0.4", "50", 1000),
        _t("cA", "Yes", "SELL", "0.6", "200", 2000),
    ]
    s = compute_wallet_stats(trades, {}, window_days=30, window_end_ts=3000)
    assert s.net_realized_pnl == Decimal("10.000000")  # 50*(0.6-0.4)


def test_profit_concentration_top1():
    # three winning resolved buys: gains 90, 5, 5 -> top1 = 90/100 = 0.9
    trades = [
        _t("c1", "Yes", "BUY", "0.1", "100", 1000),  # win -> +90
        _t("c2", "Yes", "BUY", "0.5", "10", 1000),   # win -> +5
        _t("c3", "Yes", "BUY", "0.5", "10", 1000),   # win -> +5
    ]
    resolved = {c: ResolvedMarket(True, "Yes") for c in ("c1", "c2", "c3")}
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=3000)
    assert s.profit_concentration_top1 == Decimal("0.900000")
    assert s.profit_concentration_top3 == Decimal("1.000000")


def test_category_stats_split():
    trades = [
        _t("cA", "Yes", "BUY", "0.5", "100", 1000, "CRYPTO"),  # win +50
        _t("cB", "Yes", "BUY", "0.5", "100", 1000, "POLITICS"),  # lose -50
    ]
    resolved = {"cA": ResolvedMarket(True, "Yes"), "cB": ResolvedMarket(True, "No")}
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=3000)
    assert s.per_category["CRYPTO"].pnl == Decimal("50.000000")
    assert s.per_category["POLITICS"].pnl == Decimal("-50.000000")
    assert s.per_category["CRYPTO"].win_rate == Decimal("1.000000")


def test_missing_categories_do_not_create_unknown_bucket():
    trades = [
        _t("cA", "Yes", "BUY", "0.5", "100", 1000, ""),
        _t("cB", "Yes", "BUY", "0.5", "100", 1000, "UNKNOWN"),
    ]
    resolved = {"cA": ResolvedMarket(True, "Yes"), "cB": ResolvedMarket(True, "No")}
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=3000)
    assert s.net_realized_pnl == Decimal("0.000000")
    assert s.resolved_trade_count == 2
    assert s.per_category == {}


def test_drawdown_estimate():
    # cumulative: +100 then -60 -> peak 100, trough 40 -> drawdown 60
    trades = [
        _t("c1", "Yes", "BUY", "0.5", "200", 1000),  # win +100
        _t("c2", "Yes", "BUY", "0.5", "120", 1500),  # lose -60
    ]
    resolved = {"c1": ResolvedMarket(True, "Yes"), "c2": ResolvedMarket(True, "No")}
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=3000)
    assert s.drawdown_estimate == Decimal("60.000000")


def test_empty_history_is_zeroed():
    s = compute_wallet_stats([], {}, window_days=30, window_end_ts=3000)
    assert s.trade_count == 0
    assert s.roi == Decimal(0)
    assert s.data_completeness_score == Decimal(0)
