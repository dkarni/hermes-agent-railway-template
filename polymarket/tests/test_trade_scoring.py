from __future__ import annotations

from decimal import Decimal

from ..db import initial_rule_set_payload
from ..domain import decisions as dec
from ..domain import trade_scoring as ts
from ..domain.decisions import NullPortfolioView
from ..jobs.common import observed_idempotency_key


def _payload():
    return initial_rule_set_payload()


def _inputs(**over):
    d = dict(
        wallet=ts.WalletFacts("0xw", "track", Decimal(90), Decimal(90), 100, 60, ("CRYPTO",)),
        category_stat_score=Decimal(80),
        observed=ts.ObservedFacts("BUY", Decimal("0.50"), 30, False, "CRYPTO", "Yes"),
        market=ts.MarketFacts("c", "m", "CRYPTO", False, False, False, 7200, True),
        book=ts.BookFacts(Decimal("0.49"), Decimal("0.51"), Decimal("0.02"), 10, False),
        fill=ts.FillFacts(Decimal("20"), Decimal("20"), Decimal("0.505"), True, "complete"),
        payload=_payload(),
    )
    d.update(over)
    return ts.TradeScoreInputs(**d)


def _gate(score, name):
    return next(g for g in score.hard_gates if g.name == name)


def _fgate(score, name):
    return next(g for g in score.freshness_gates if g.name == name)


# --- happy path -------------------------------------------------------------

def test_copy_decision_and_size():
    inp = _inputs()
    sc = ts.score_trade(inp)
    assert sc.all_hard_pass
    d = dec.decide(inp, sc, NullPortfolioView(), event_id="e")
    assert d.decision == dec.DECISION_PAPER_COPY
    assert d.expected_position_usd == Decimal("5")  # 75-84 tier


def test_high_score_higher_tier():
    inp = _inputs(
        wallet=ts.WalletFacts("0xw", "track", Decimal(100), Decimal(100), 5, 5, ("CRYPTO",)),
        category_stat_score=Decimal(100),
        book=ts.BookFacts(Decimal("0.499"), Decimal("0.501"), Decimal("0.002"), 1, False),
        observed=ts.ObservedFacts("BUY", Decimal("0.50"), 1, False, "CRYPTO", "Yes"),
        fill=ts.FillFacts(Decimal("20"), Decimal("20"), Decimal("0.5005"), True, "complete"),
    )
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, NullPortfolioView())
    assert d.decision == dec.DECISION_PAPER_COPY
    assert d.expected_position_usd in (Decimal("10"), Decimal("20"))


# --- every hard gate --------------------------------------------------------

def test_gate_closed_market():
    sc = ts.score_trade(_inputs(market=ts.MarketFacts("c", "m", "CRYPTO", True, False, False, 7200, True)))
    assert not _gate(sc, "market_state").passed


def test_gate_stale_data():
    sc = ts.score_trade(_inputs(book=ts.BookFacts(Decimal("0.49"), Decimal("0.51"), Decimal("0.02"), 500, True)))
    assert not _gate(sc, "source_data_fresh").passed


def test_gate_insufficient_depth():
    sc = ts.score_trade(_inputs(fill=ts.FillFacts(Decimal("1"), Decimal("20"), Decimal("0.5"), False, "book_exhausted")))
    assert not _gate(sc, "depth_and_slippage").passed


def test_gate_spread():
    sc = ts.score_trade(_inputs(book=ts.BookFacts(Decimal("0.30"), Decimal("0.70"), Decimal("0.40"), 10, False)))
    assert not _gate(sc, "spread").passed


def test_gate_price_move():
    # source 0.50, executable 0.60 -> abs move 0.10 > max 0.05
    sc = ts.score_trade(_inputs(fill=ts.FillFacts(Decimal("20"), Decimal("20"), Decimal("0.60"), True, "complete")))
    assert not _gate(sc, "price_move").passed


def test_gate_time_to_resolution():
    sc = ts.score_trade(_inputs(market=ts.MarketFacts("c", "m", "CRYPTO", False, False, False, 60, True)))
    assert not _gate(sc, "time_to_resolution").passed


def test_gate_wrong_category():
    sc = ts.score_trade(_inputs(
        observed=ts.ObservedFacts("BUY", Decimal("0.50"), 30, False, "SPORTS", "Yes"),
    ))
    assert not _gate(sc, "wallet_category").passed


def test_gate_ignored_wallet():
    sc = ts.score_trade(_inputs(
        wallet=ts.WalletFacts("0xw", "ignore", Decimal(90), Decimal(90), 100, 60, ("CRYPTO",)),
    ))
    assert not _gate(sc, "wallet_status").passed


def test_gate_duplicate():
    sc = ts.score_trade(_inputs(
        observed=ts.ObservedFacts("BUY", Decimal("0.50"), 30, True, "CRYPTO", "Yes"),
    ))
    assert not _gate(sc, "duplicate").passed


def test_freshness_gate_book():
    sc = ts.score_trade(_inputs(book=ts.BookFacts(Decimal("0.49"), Decimal("0.51"), Decimal("0.02"), 999, False)))
    assert not _fgate(sc, "book_freshness").passed


def test_freshness_gate_profile():
    sc = ts.score_trade(_inputs(
        wallet=ts.WalletFacts("0xw", "track", Decimal(90), Decimal(90), 999999, 60, ("CRYPTO",)),
    ))
    assert not _fgate(sc, "wallet_profile_freshness").passed


# --- decision thresholds ----------------------------------------------------

def test_stale_never_copies():
    inp = _inputs(book=ts.BookFacts(Decimal("0.49"), Decimal("0.51"), Decimal("0.02"), 999, True))
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, NullPortfolioView())
    assert d.decision in (dec.DECISION_WATCHLIST, dec.DECISION_SKIP)
    assert d.decision != dec.DECISION_PAPER_COPY
    assert d.decision_reason_code == "stale_data_downgrade"


def test_non_recoverable_gate_skips():
    inp = _inputs(market=ts.MarketFacts("c", "m", "CRYPTO", True, False, False, 7200, True))
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, NullPortfolioView())
    assert d.decision == dec.DECISION_SKIP


# --- SELL behavior ----------------------------------------------------------

def test_sell_no_position_skip():
    inp = _inputs(observed=ts.ObservedFacts("SELL", Decimal("0.50"), 30, False, "CRYPTO", "Yes"))
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, NullPortfolioView())
    assert d.decision == dec.DECISION_SKIP
    assert d.decision_reason_code == "sell_no_position_recorded_for_analysis"
    assert d.expected_position_usd == Decimal(0)


def test_sell_with_position_watchlist_close():
    class HoldingView(NullPortfolioView):
        def open_position_for(self, wallet, condition_id, outcome):
            return True

    inp = _inputs(observed=ts.ObservedFacts("SELL", Decimal("0.50"), 30, False, "CRYPTO", "Yes"))
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, HoldingView())
    assert d.decision == dec.DECISION_WATCHLIST
    assert d.decision_reason_code == "sell_position_close_candidate"


# --- idempotency key --------------------------------------------------------

def test_idempotency_key_stable_and_distinct():
    k1 = observed_idempotency_key(source="data-api", wallet="0xw", transaction_hash="0xabc",
                                  asset_id="a", side="BUY", price=Decimal("0.5"), size=Decimal("10"), timestamp=1)
    k2 = observed_idempotency_key(source="data-api", wallet="0xw", transaction_hash="0xabc",
                                  asset_id="a", side="BUY", price=Decimal("0.5"), size=Decimal("10"), timestamp=1)
    assert k1 == k2
    k3 = observed_idempotency_key(source="data-api", wallet="0xw", transaction_hash="0xabc",
                                  asset_id="a", side="SELL", price=Decimal("0.5"), size=Decimal("10"), timestamp=1)
    assert k1 != k3


def test_idempotency_key_fallback_without_txhash():
    k = observed_idempotency_key(source="data-api", wallet="0xw", transaction_hash="",
                                 asset_id="a", side="BUY", price=Decimal("0.5"), size=Decimal("10"), timestamp=42)
    assert k.startswith("sha256:")


def test_explanation_structure():
    inp = _inputs()
    sc = ts.score_trade(inp)
    d = dec.decide(inp, sc, NullPortfolioView(), event_id="e")
    exp = dec.build_explanation(inp, sc, d, rule_set_version=1,
                                market_data_timestamp="2026-07-06T00:00:00Z",
                                wallet_profile_timestamp="2026-07-06T00:00:00Z")
    for key in ("decision", "total_score", "component_scores", "rule_set_version",
                "reasons", "risks", "hard_gates", "freshness_gates", "market_data_timestamp",
                "wallet_profile_timestamp", "source_entry_price", "executable_entry_price",
                "expected_position_usd", "portfolio_limit_result"):
        assert key in exp
    assert len(exp["component_scores"]) == 8
