from __future__ import annotations

from decimal import Decimal

from ..db import initial_rule_set_payload
from ..domain import scoring
from ..domain.scoring import RankedWallet, WalletScore, one_hit_wonder_penalty
from ..domain.wallet_stats import (
    CategoryStat,
    ResolvedMarket,
    StatTrade,
    compute_wallet_stats,
)


def _payload():
    return initial_rule_set_payload()


def _bands():
    return _payload()["one_hit_wonder_penalty_bands"]


def test_one_hit_wonder_bands_and_interpolation():
    b = _bands()
    assert one_hit_wonder_penalty(Decimal("0.10"), b) == Decimal("0.00")
    assert one_hit_wonder_penalty(Decimal("0.25"), b) == Decimal("0.00")
    # midpoint of (0.25,0.40] band -> midpoint of (5,10) penalty
    assert one_hit_wonder_penalty(Decimal("0.325"), b) == Decimal("7.50")
    assert one_hit_wonder_penalty(Decimal("0.40"), b) == Decimal("10.00")
    # midpoint of (0.40,0.60] -> midpoint of (10,25)
    assert one_hit_wonder_penalty(Decimal("0.50"), b) == Decimal("17.50")
    assert one_hit_wonder_penalty(Decimal("1.00"), b) == Decimal("40.00")


def _many_trades(n_win, n_lose):
    trades = []
    resolved = {}
    ts = 1000
    for i in range(n_win):
        c = f"w{i}"
        trades.append(StatTrade(c, "Yes", "BUY", Decimal("0.5"), Decimal("10"), ts + i, "CRYPTO"))
        resolved[c] = ResolvedMarket(True, "Yes")
    for i in range(n_lose):
        c = f"l{i}"
        trades.append(StatTrade(c, "Yes", "BUY", Decimal("0.5"), Decimal("10"), ts + 100 + i, "CRYPTO"))
        resolved[c] = ResolvedMarket(True, "No")
    return trades, resolved


def test_status_insufficient_below_min_resolved():
    trades, resolved = _many_trades(3, 2)  # 5 resolved < 10
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=99999)
    sc = scoring.score_wallet(s, _payload())
    assert sc.status == scoring.STATUS_INSUFFICIENT
    assert sc.status_reason_code == "below_min_resolved_trades"


def test_status_track_high_score():
    trades, resolved = _many_trades(14, 1)  # 15 resolved, mostly wins
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
    sc = scoring.score_wallet(s, _payload())
    assert sc.status in (scoring.STATUS_TRACK, scoring.STATUS_WATCH)
    assert 0 <= sc.global_score <= 100


def test_status_ignore_low_score():
    trades, resolved = _many_trades(1, 14)  # mostly losses
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
    sc = scoring.score_wallet(s, _payload())
    assert sc.status in (scoring.STATUS_IGNORE, scoring.STATUS_WATCH)


def test_status_data_error_when_history_incomplete():
    trades, resolved = _many_trades(14, 1)
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
    sc = scoring.score_wallet(s, _payload(), history_complete=False)
    assert sc.status == scoring.STATUS_DATA_ERROR


def test_partial_scan_no_promotion():
    trades, resolved = _many_trades(14, 1)
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
    sc = scoring.score_wallet(s, _payload(), from_partial_scan=True)
    assert sc.status == scoring.STATUS_INSUFFICIENT
    assert sc.status_reason_code == "partial_or_stale_no_promotion"


def test_category_shrinkage():
    # Small sample category should shrink toward overall, not score a full edge.
    small = CategoryStat("CRYPTO", 2, 2, Decimal("100"), Decimal("50"), Decimal("2"),
                         Decimal("1.0"), 2, 0)
    big = CategoryStat("POLITICS", 40, 40, Decimal("100"), Decimal("50"), Decimal("2"),
                       Decimal("1.0"), 40, 0)
    overall = Decimal("50")
    k = 10
    from ..domain.wallet_stats import WalletStats
    stats = WalletStats(
        window_days=30, trade_count=42, buy_count=42, sell_count=0, resolved_trade_count=42,
        gross_realized_pnl=Decimal(0), net_realized_pnl=Decimal(0), unrealized_pnl=Decimal(0),
        capital_deployed=Decimal(0), roi=Decimal(0), win_rate=Decimal(0), resolved_wins=0,
        resolved_losses=0, avg_trade_size=Decimal(0), median_trade_size=Decimal(0),
        pnl_per_trade=Decimal(0), positive_day_ratio=Decimal(0),
        profit_concentration_top1=Decimal(0), profit_concentration_top3=Decimal(0),
        profit_concentration_top5=Decimal(0), drawdown_estimate=Decimal(0),
        entry_timing_seconds=Decimal(0), trade_frequency_per_day=Decimal(0), recency_seconds=0,
        data_completeness_score=Decimal(1), per_category={"CRYPTO": small, "POLITICS": big},
        window_start_ts=0, window_end_ts=1, distinct_markets=42, realized_events=42,
    )
    edge = scoring.score_category_edge(stats, overall, shrinkage_k=k)
    # big-sample category (weight 40/50) contributes more of its 100-ish sample
    # score than the small one (weight 2/12), so shrunk edge is well above overall.
    assert edge > overall


def test_tracked_limit_cap_by_rank():
    payload = _payload()
    trades, resolved = _many_trades(14, 1)
    ranked = []
    for i in range(5):
        s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
        # force distinct scores by fabricating WalletScore directly
        sc = WalletScore(
            global_score=Decimal(90 - i),
            weighted_score=Decimal(90 - i),
            one_hit_wonder_penalty=Decimal(0),
            components=scoring.score_wallet(s, payload).components,
            status=scoring.STATUS_TRACK,
            status_reason_code="x",
            data_quality_score=Decimal(90),
        )
        ranked.append(RankedWallet(f"0x{i}", sc))
    result = scoring.enforce_tracked_limit(ranked, tracked_wallet_limit=2)
    tracked = [w for w, (st, _) in result.items() if st == "track"]
    watched = [w for w, (st, r) in result.items() if st == "watch"]
    assert len(tracked) == 2
    assert set(tracked) == {"0x0", "0x1"}  # highest scores
    assert all(result[w][1] == "tracked_wallet_limit_reached" for w in watched)


def test_partial_scan_blocks_promotion():
    """PRD 11.7: a wallet whose latest leaderboard appearance came from a
    partial scan must not be promoted or downgraded (stays insufficient_data)."""
    trades, resolved = _many_trades(14, 1)  # would otherwise be track/watch
    s = compute_wallet_stats(trades, resolved, window_days=30, window_end_ts=1200)
    sc = scoring.score_wallet(s, _payload(), from_partial_scan=True)
    assert sc.status == scoring.STATUS_INSUFFICIENT
    assert sc.status_reason_code == "partial_or_stale_no_promotion"
