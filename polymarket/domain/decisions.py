"""Decision engine + structured explanation (PRD sec 13.4, 13.5, 14.2).

Turns a TradeScore into a decision (paper_copy / watchlist / skip), computes the
expected position size from the confidence tiers (PRD 14.2) and the portfolio
view, and builds the structured explanation stored as JSON in decision_journal.

Wave 2 produces decisions and journal entries only; it never opens paper trades.
The ``PortfolioView`` protocol is the seam Wave 3 implements against a real
portfolio. ``NullPortfolioView`` is used until then: unlimited cash, no open
positions, no copies today — so exposure gates always pass and sizing is bounded
only by the confidence tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Protocol, Sequence

from .trade_scoring import GateResult, TradeScore, TradeScoreInputs

ZERO = Decimal(0)

DECISION_PAPER_COPY = "paper_copy"
DECISION_WATCHLIST = "watchlist"
DECISION_SKIP = "skip"


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- portfolio view seam (Wave 3 implements) --------------------------------

class PortfolioView(Protocol):
    """Read-only portfolio state the decision engine needs.

    Wave 3 supplies a concrete implementation backed by paper_portfolios /
    paper_trades. All amounts are USD Decimals.
    """

    def available_cash(self) -> Decimal: ...

    def open_position_count(self) -> int: ...

    def open_position_for(self, wallet: str, condition_id: str, outcome: str) -> bool: ...

    def wallet_exposure(self, wallet: str) -> Decimal: ...

    def category_exposure(self, category: str) -> Decimal: ...

    def event_exposure(self, event_id: str) -> Decimal: ...

    def equity(self) -> Decimal: ...

    def copies_today(self, wallet: str) -> int: ...


class NullPortfolioView:
    """Wave-2 stand-in: unlimited cash, no positions, no prior copies.

    Documented behaviour: exposure/cash gates never bind, so a decision here is
    a pure signal-quality decision. Wave 3 swaps in a real view and the same
    decision code will then honour cash/exposure/daily-copy limits.
    """

    def available_cash(self) -> Decimal:
        return Decimal("1000000000")

    def open_position_count(self) -> int:
        return 0

    def open_position_for(self, wallet: str, condition_id: str, outcome: str) -> bool:
        return False

    def wallet_exposure(self, wallet: str) -> Decimal:
        return ZERO

    def category_exposure(self, category: str) -> Decimal:
        return ZERO

    def event_exposure(self, event_id: str) -> Decimal:
        return ZERO

    def equity(self) -> Decimal:
        return Decimal("1000")

    def copies_today(self, wallet: str) -> int:
        return 0


# --- sizing (PRD 14.2) ------------------------------------------------------

def confidence_tier_size(score: Decimal, payload: Mapping) -> Decimal:
    """USD size for a score from the confidence tiers, 0 if below all tiers."""
    for tier in payload["confidence_tiers"]:
        if _d(tier["min_score"]) <= score <= _d(tier["max_score"]):
            return _d(tier["size_usd"])
    return ZERO


def expected_position_size(
    score: Decimal,
    payload: Mapping,
    portfolio: PortfolioView,
    *,
    wallet: str,
    category: str,
    event_id: str,
    fillable_usd: Decimal,
) -> Decimal:
    """Smallest of tier, cash, per-position/wallet/category/event caps, fillable.

    Percent caps are expressed against portfolio equity (PRD 14.3). With
    NullPortfolioView (unlimited cash, huge equity) only the tier and fillable
    bound the size.
    """
    tier = confidence_tier_size(score, payload)
    if tier <= 0:
        return ZERO
    limits = payload["exposure_limits"]
    equity = portfolio.equity()

    candidates = [
        tier,
        portfolio.available_cash(),
        _d(limits["max_position_usd"]),
        _pct_headroom(equity, _d(limits["max_wallet_exposure_percent"]), portfolio.wallet_exposure(wallet)),
        _pct_headroom(equity, _d(limits["max_category_exposure_percent"]), portfolio.category_exposure(category)),
        _pct_headroom(equity, _d(limits["max_event_exposure_percent"]), portfolio.event_exposure(event_id)),
        fillable_usd,
    ]
    return max(ZERO, min(candidates))


def _pct_headroom(equity: Decimal, pct: Decimal, current: Decimal) -> Decimal:
    cap = equity * pct / Decimal(100)
    return max(ZERO, cap - current)


# --- portfolio gates --------------------------------------------------------

def portfolio_gates(
    payload: Mapping,
    portfolio: PortfolioView,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    category: str,
    event_id: str,
    tier_size: Decimal,
) -> list[GateResult]:
    limits = payload["exposure_limits"]
    results: list[GateResult] = []

    max_positions = int(_d(limits["max_open_positions"]))
    positions_ok = portfolio.open_position_count() < max_positions
    results.append(GateResult(
        "portfolio_open_positions",
        positions_ok,
        f"open={portfolio.open_position_count()} max={max_positions}",
    ))

    dup_thesis_ok = not portfolio.open_position_for(wallet, condition_id, outcome)
    results.append(GateResult(
        "portfolio_duplicate_thesis",
        dup_thesis_ok,
        "no existing thesis" if dup_thesis_ok else "wallet already holds this outcome",
    ))

    cash_ok = portfolio.available_cash() >= tier_size and tier_size > 0
    results.append(GateResult(
        "portfolio_cash",
        cash_ok,
        f"cash={portfolio.available_cash()} needed={tier_size}",
    ))

    max_copies = int(_d(limits["max_copies_per_wallet_per_day"]))
    copies_ok = portfolio.copies_today(wallet) < max_copies
    results.append(GateResult(
        "portfolio_daily_copies",
        copies_ok,
        f"today={portfolio.copies_today(wallet)} max={max_copies}",
    ))

    return results


# --- decision ---------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    decision: str
    decision_reason_code: str
    total_score: Decimal
    expected_position_usd: Decimal
    all_hard_gates: tuple[GateResult, ...]
    portfolio_gates: tuple[GateResult, ...]
    reasons: tuple[str, ...]
    risks: tuple[str, ...]


def decide(
    inputs: TradeScoreInputs,
    score: TradeScore,
    portfolio: PortfolioView,
    *,
    event_id: str = "",
) -> Decision:
    """Decision per PRD 13.4 with hard/freshness/portfolio gates and SELL rule.

    SELL handling (PRD 12.3): a source SELL never opens a short paper position.
    It is recorded as ``watchlist`` with a position-close reason when the
    portfolio holds the thesis, otherwise ``skip`` recorded for analysis.
    """
    payload = inputs.payload
    thresholds = payload["decision_thresholds"]
    copy_min = _d(thresholds["paper_copy_min_score"])
    watch_min = _d(thresholds["watchlist_min_score"])

    o = inputs.observed
    m = inputs.market

    tier_size = confidence_tier_size(score.total_score, payload)
    pgates = portfolio_gates(
        payload,
        portfolio,
        wallet=inputs.wallet.wallet_address,
        condition_id=m.condition_id,
        outcome=o.outcome,
        category=o.category,
        event_id=event_id,
        tier_size=tier_size,
    )

    # SELL branch first (PRD 12.3): never short.
    if o.side == "SELL":
        holds = portfolio.open_position_for(inputs.wallet.wallet_address, m.condition_id, o.outcome)
        if holds:
            return Decision(
                decision=DECISION_WATCHLIST,
                decision_reason_code="sell_position_close_candidate",
                total_score=score.total_score,
                expected_position_usd=ZERO,
                all_hard_gates=score.hard_gates,
                portfolio_gates=tuple(pgates),
                reasons=("source wallet sold; recorded as potential position close (Wave 3 acts)",),
                risks=("no short position opened in version one",),
            )
        return Decision(
            decision=DECISION_SKIP,
            decision_reason_code="sell_no_position_recorded_for_analysis",
            total_score=score.total_score,
            expected_position_usd=ZERO,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=("source wallet sold but no matching paper thesis; recorded for analysis only",),
            risks=("no short position opened in version one",),
        )

    fresh_ok = score.all_freshness_pass
    hard_ok = score.all_hard_pass
    portfolio_ok = all(g.passed for g in pgates)

    reasons = _reasons_ordered(inputs, score, pgates)
    risks = _risks(inputs, score)

    # Stale/partial data can never be paper_copy (PRD 12.4).
    if not fresh_ok:
        decision = DECISION_WATCHLIST if score.total_score >= watch_min else DECISION_SKIP
        return Decision(
            decision=decision,
            decision_reason_code="stale_data_downgrade",
            total_score=score.total_score,
            expected_position_usd=ZERO,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=("required market data is stale; cannot copy",) + reasons,
            risks=risks,
        )

    if score.total_score >= copy_min and hard_ok and portfolio_ok:
        size = expected_position_size(
            score.total_score, payload, portfolio,
            wallet=inputs.wallet.wallet_address,
            category=o.category,
            event_id=event_id,
            fillable_usd=inputs.fill.filled_usd,
        )
        if size <= 0:
            return Decision(
                decision=DECISION_WATCHLIST,
                decision_reason_code="score_ok_but_size_zero",
                total_score=score.total_score,
                expected_position_usd=ZERO,
                all_hard_gates=score.hard_gates,
                portfolio_gates=tuple(pgates),
                reasons=("score qualifies but no size available within limits",) + reasons,
                risks=risks,
            )
        return Decision(
            decision=DECISION_PAPER_COPY,
            decision_reason_code="score_above_copy_and_gates_pass",
            total_score=score.total_score,
            expected_position_usd=size,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=reasons,
            risks=risks,
        )

    # Non-recoverable hard-gate failures -> skip; else score-banded.
    if not hard_ok:
        failed = [g.name for g in score.hard_gates if not g.passed]
        recoverable = {"spread", "price_move", "source_data_fresh", "depth_and_slippage"}
        non_recoverable = [g for g in failed if g not in recoverable]
        if non_recoverable or score.total_score < watch_min:
            return Decision(
                decision=DECISION_SKIP,
                decision_reason_code="hard_gate_failed:" + ",".join(failed),
                total_score=score.total_score,
                expected_position_usd=ZERO,
                all_hard_gates=score.hard_gates,
                portfolio_gates=tuple(pgates),
                reasons=reasons,
                risks=risks,
            )
        return Decision(
            decision=DECISION_WATCHLIST,
            decision_reason_code="recoverable_gate_failed:" + ",".join(failed),
            total_score=score.total_score,
            expected_position_usd=ZERO,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=reasons,
            risks=risks,
        )

    if not portfolio_ok:
        failed = [g.name for g in pgates if not g.passed]
        return Decision(
            decision=DECISION_WATCHLIST,
            decision_reason_code="portfolio_limit:" + ",".join(failed),
            total_score=score.total_score,
            expected_position_usd=ZERO,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=reasons,
            risks=risks,
        )

    if score.total_score >= watch_min:
        return Decision(
            decision=DECISION_WATCHLIST,
            decision_reason_code="score_in_watch_band",
            total_score=score.total_score,
            expected_position_usd=ZERO,
            all_hard_gates=score.hard_gates,
            portfolio_gates=tuple(pgates),
            reasons=reasons,
            risks=risks,
        )

    return Decision(
        decision=DECISION_SKIP,
        decision_reason_code="score_below_watch_threshold",
        total_score=score.total_score,
        expected_position_usd=ZERO,
        all_hard_gates=score.hard_gates,
        portfolio_gates=tuple(pgates),
        reasons=reasons,
        risks=risks,
    )


def _reasons_ordered(
    inputs: TradeScoreInputs, score: TradeScore, pgates: Sequence[GateResult]
) -> tuple[str, ...]:
    """Reasons ordered by importance: failed gates first, then weak components."""
    reasons: list[str] = []
    for g in score.hard_gates:
        if not g.passed:
            reasons.append(f"hard gate failed: {g.name} ({g.detail})")
    for g in pgates:
        if not g.passed:
            reasons.append(f"portfolio gate failed: {g.name} ({g.detail})")
    comps = sorted(score.components.as_dict().items(), key=lambda kv: kv[1])
    for name, value in comps[:3]:
        reasons.append(f"component {name}={value:.1f}")
    reasons.append(f"wallet global score {inputs.wallet.global_score}")
    return tuple(reasons)


def _risks(inputs: TradeScoreInputs, score: TradeScore) -> tuple[str, ...]:
    risks: list[str] = []
    if score.price_move_absolute > 0:
        risks.append(f"price moved {score.price_move_absolute} against entry since source")
    if inputs.book.spread is not None:
        risks.append(f"spread {inputs.book.spread}")
    if inputs.market.seconds_to_resolution is not None:
        risks.append(f"time to resolution {inputs.market.seconds_to_resolution}s")
    if not inputs.fill.fully_filled:
        risks.append(f"partial fill: {inputs.fill.filled_usd}/{inputs.fill.target_usd} usd")
    return tuple(risks)


# --- structured explanation (PRD 13.5) --------------------------------------

def build_explanation(
    inputs: TradeScoreInputs,
    score: TradeScore,
    decision: Decision,
    *,
    rule_set_version: int,
    market_data_timestamp: str | None,
    wallet_profile_timestamp: str | None,
) -> dict:
    """Structured, queryable explanation stored as JSON in decision_journal."""
    return {
        "decision": decision.decision,
        "decision_reason_code": decision.decision_reason_code,
        "total_score": float(score.total_score),
        "component_scores": score.components.as_dict(),
        "rule_set_version": rule_set_version,
        "reasons": list(decision.reasons),
        "risks": list(decision.risks),
        "hard_gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail} for g in score.hard_gates
        ],
        "freshness_gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail} for g in score.freshness_gates
        ],
        "portfolio_gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail} for g in decision.portfolio_gates
        ],
        "market_data_timestamp": market_data_timestamp,
        "wallet_profile_timestamp": wallet_profile_timestamp,
        "source_entry_price": float(inputs.observed.source_price),
        "executable_entry_price": (
            float(score.executable_entry_price) if score.executable_entry_price is not None else None
        ),
        "price_move_absolute": float(score.price_move_absolute),
        "price_move_percent": float(score.price_move_percent),
        "expected_position_usd": float(decision.expected_position_usd),
        "portfolio_limit_result": "pass" if all(g.passed for g in decision.portfolio_gates) else "blocked",
    }
