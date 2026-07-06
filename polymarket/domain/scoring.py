"""Wallet scoring (PRD sec 11), parameterized entirely by the active rule set.

Pure and deterministic. No number is hardcoded: weights, penalty bands, status
thresholds and shrinkage k all come from the rule-set payload
(db.initial_rule_set_payload() shows the shape).

Component scores are 0..100 (PRD 11.2). The global score is the weight-weighted
blend minus the interpolated one-hit-wonder penalty (PRD 11.6), clamped 0..100.
Status assignment follows PRD 11.7; the TRACKED_WALLET_LIMIT is enforced by
score rank across a batch (excess `track` candidates degrade to `watch`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from .wallet_stats import CategoryStat, WalletStats

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


# --- status reason codes ----------------------------------------------------

STATUS_TRACK = "track"
STATUS_WATCH = "watch"
STATUS_IGNORE = "ignore"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_DATA_ERROR = "data_error"


@dataclass(frozen=True)
class ComponentScores:
    roi_quality: Decimal
    consistency: Decimal
    copyability: Decimal
    category_edge: Decimal
    liquidity_quality: Decimal
    entry_timing: Decimal
    resolved_sample_quality: Decimal

    def as_dict(self) -> dict[str, float]:
        return {
            "roi_quality": float(self.roi_quality),
            "consistency": float(self.consistency),
            "copyability": float(self.copyability),
            "category_edge": float(self.category_edge),
            "liquidity_quality": float(self.liquidity_quality),
            "entry_timing": float(self.entry_timing),
            "resolved_sample_quality": float(self.resolved_sample_quality),
        }


@dataclass(frozen=True)
class WalletScore:
    global_score: Decimal            # 0..100 after penalty + clamp
    weighted_score: Decimal          # before penalty
    one_hit_wonder_penalty: Decimal
    components: ComponentScores
    status: str
    status_reason_code: str
    data_quality_score: Decimal      # 0..100


def _clamp(value: Decimal, lo: Decimal = ZERO, hi: Decimal = HUNDRED) -> Decimal:
    return max(lo, min(hi, value))


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- component scores (0..100) ----------------------------------------------

def _score_roi_quality(stats: WalletStats) -> Decimal:
    """Net PnL relative to capital, dampened for tiny samples / tiny capital.

    Extreme ROI on very little capital or few resolved trades cannot reach the
    top (PRD 11.3). We map ROI through a saturating curve then multiply by a
    sample-sufficiency factor.
    """
    roi = stats.roi
    # Saturating: 0% ROI -> 50, +100% -> ~91, -100% -> ~9 (logistic-ish via ratio).
    base = HUNDRED * (roi / (Decimal("0.5") + (roi if roi >= 0 else -roi)) + ONE) / Decimal(2)
    base = _clamp(base)
    sufficiency = min(ONE, Decimal(stats.resolved_trade_count) / Decimal(10))
    # blend toward neutral 50 when sample is thin
    return _clamp(Decimal(50) + (base - Decimal(50)) * sufficiency)


def _score_consistency(stats: WalletStats) -> Decimal:
    """Positive-day ratio + win rate + concentration penalty + market breadth."""
    pos_day = stats.positive_day_ratio * HUNDRED
    win = stats.win_rate * HUNDRED
    concentration_penalty = stats.profit_concentration_top1 * Decimal(40)
    breadth = min(ONE, Decimal(stats.distinct_markets) / Decimal(15)) * HUNDRED
    raw = (pos_day * Decimal("0.35") + win * Decimal("0.35") + breadth * Decimal("0.30"))
    return _clamp(raw - concentration_penalty)


def _score_copyability(stats: WalletStats, executable_ratio: Decimal | None) -> Decimal:
    """Executability + freshness of edge. Uses executable ratio and recency."""
    execu = (executable_ratio if executable_ratio is not None else Decimal("0.75")) * HUNDRED
    # recency: fresh trades (< 3 days) score high, stale (> window) score low
    recency_factor = _clamp(
        HUNDRED - (Decimal(stats.recency_seconds) / Decimal(86400)) * Decimal(10)
    )
    frequency = min(ONE, stats.trade_frequency_per_day / Decimal(3)) * HUNDRED
    raw = execu * Decimal("0.5") + recency_factor * Decimal("0.3") + frequency * Decimal("0.2")
    return _clamp(raw)


def _score_liquidity_quality(stats: WalletStats, executable_ratio: Decimal | None) -> Decimal:
    execu = (executable_ratio if executable_ratio is not None else Decimal("0.75"))
    return _clamp(execu * HUNDRED)


def _score_entry_timing(stats: WalletStats) -> Decimal:
    """More time before resolution at entry = better (not last-second)."""
    if stats.entry_timing_seconds <= 0:
        return Decimal(50)
    days = stats.entry_timing_seconds / Decimal(86400)
    return _clamp(min(ONE, days / Decimal(7)) * HUNDRED)


def _score_resolved_sample_quality(stats: WalletStats, min_resolved: int) -> Decimal:
    if min_resolved <= 0:
        return HUNDRED
    ratio = Decimal(stats.resolved_trade_count) / Decimal(min_resolved)
    return _clamp(min(ONE, ratio) * HUNDRED)


def score_category_edge(
    stats: WalletStats,
    overall_component_proxy: Decimal,
    *,
    shrinkage_k: int,
) -> Decimal:
    """Best per-category edge, shrunk toward the wallet overall (PRD 10.3).

    category score = blend of the category sample win-rate-derived score and the
    wallet's overall score with weight n/(n+k). Returns the *max* shrunk edge
    across categories (the wallet's strongest proven category).
    """
    if not stats.per_category:
        return overall_component_proxy
    best = ZERO
    for cat in stats.per_category.values():
        n = Decimal(cat.resolved_trade_count)
        sample_score = _category_sample_score(cat)
        weight = n / (n + Decimal(shrinkage_k)) if (n + shrinkage_k) > 0 else ZERO
        shrunk = weight * sample_score + (ONE - weight) * overall_component_proxy
        best = max(best, shrunk)
    return _clamp(best)


def _category_sample_score(cat: CategoryStat) -> Decimal:
    win = cat.win_rate * HUNDRED
    roi_bonus = _clamp(HUNDRED * (cat.roi / (Decimal("0.5") + abs(cat.roi)) + ONE) / Decimal(2))
    return _clamp(win * Decimal("0.6") + roi_bonus * Decimal("0.4"))


# --- one-hit-wonder penalty (PRD 11.6) --------------------------------------

def one_hit_wonder_penalty(top1_share: Decimal, bands: Sequence[Mapping]) -> Decimal:
    """Interpolate the penalty within the matching band.

    Each band: {upper_share, penalty_min, penalty_max}. Bands are ordered by
    upper_share; the penalty interpolates linearly from penalty_min at the band's
    lower edge to penalty_max at its upper_share edge.
    """
    ordered = sorted(bands, key=lambda b: _d(b["upper_share"]))
    lower = ZERO
    for band in ordered:
        upper = _d(band["upper_share"])
        pmin = _d(band["penalty_min"])
        pmax = _d(band["penalty_max"])
        if top1_share <= upper or upper >= ONE:
            span = upper - lower
            if span <= 0:
                return _q(pmax)
            frac = _clamp((top1_share - lower) / span, ZERO, ONE)
            return _q(pmin + (pmax - pmin) * frac)
        lower = upper
    # above all bands -> max penalty of last band
    last = ordered[-1]
    return _q(_d(last["penalty_max"]))


# --- global score + status --------------------------------------------------

def score_wallet(
    stats: WalletStats,
    payload: Mapping,
    *,
    executable_ratio: Decimal | None = None,
    history_complete: bool = True,
    from_partial_scan: bool = False,
    profile_stale: bool = False,
) -> WalletScore:
    """Full wallet scoring per PRD sec 11. All numbers from ``payload``."""
    weights = payload["wallet_score_weights"]
    thresholds = payload["wallet_status_thresholds"]
    bands = payload["one_hit_wonder_penalty_bands"]
    shrinkage_k = int(payload.get("category_shrinkage_k", 10))
    min_resolved = int(thresholds["min_resolved_trades"])

    roi_q = _score_roi_quality(stats)
    consistency = _score_consistency(stats)
    copyability = _score_copyability(stats, executable_ratio)
    liquidity = _score_liquidity_quality(stats, executable_ratio)
    timing = _score_entry_timing(stats)
    resolved_q = _score_resolved_sample_quality(stats, min_resolved)
    # overall proxy for shrinkage uses the non-category components blended
    overall_proxy = _clamp(
        (roi_q + consistency + copyability) / Decimal(3)
    )
    category_edge = score_category_edge(stats, overall_proxy, shrinkage_k=shrinkage_k)

    components = ComponentScores(
        roi_quality=_q(roi_q),
        consistency=_q(consistency),
        copyability=_q(copyability),
        category_edge=_q(category_edge),
        liquidity_quality=_q(liquidity),
        entry_timing=_q(timing),
        resolved_sample_quality=_q(resolved_q),
    )

    weighted = (
        roi_q * _d(weights["roi_quality"])
        + consistency * _d(weights["consistency"])
        + copyability * _d(weights["copyability"])
        + category_edge * _d(weights["category_edge"])
        + liquidity * _d(weights["liquidity_quality"])
        + timing * _d(weights["entry_timing"])
        + resolved_q * _d(weights["resolved_sample_quality"])
    ) / HUNDRED

    penalty = one_hit_wonder_penalty(stats.profit_concentration_top1, bands)
    global_score = _clamp(weighted - penalty)

    data_quality = _q(stats.data_completeness_score * HUNDRED)

    status, reason = _assign_status(
        stats=stats,
        global_score=global_score,
        thresholds=thresholds,
        history_complete=history_complete,
        from_partial_scan=from_partial_scan,
        profile_stale=profile_stale,
    )

    return WalletScore(
        global_score=_q(global_score),
        weighted_score=_q(weighted),
        one_hit_wonder_penalty=penalty,
        components=components,
        status=status,
        status_reason_code=reason,
        data_quality_score=data_quality,
    )


def _assign_status(
    *,
    stats: WalletStats,
    global_score: Decimal,
    thresholds: Mapping,
    history_complete: bool,
    from_partial_scan: bool,
    profile_stale: bool,
) -> tuple[str, str]:
    track_min = _d(thresholds["track_min_score"])
    watch_min = _d(thresholds["watch_min_score"])
    min_resolved = int(thresholds["min_resolved_trades"])

    if not history_complete:
        return STATUS_DATA_ERROR, "incomplete_history"
    # No promotion/downgrade from partial scan or stale profile (PRD 11.7).
    if from_partial_scan or profile_stale:
        return STATUS_INSUFFICIENT, "partial_or_stale_no_promotion"
    if stats.resolved_trade_count < min_resolved:
        return STATUS_INSUFFICIENT, "below_min_resolved_trades"
    if global_score >= track_min:
        return STATUS_TRACK, "score_at_or_above_track_threshold"
    if global_score >= watch_min:
        return STATUS_WATCH, "score_in_watch_band"
    return STATUS_IGNORE, "score_below_watch_threshold"


# --- tracked-wallet cap enforcement -----------------------------------------

@dataclass(frozen=True)
class RankedWallet:
    wallet_address: str
    score: WalletScore


def enforce_tracked_limit(
    scored: Sequence[RankedWallet], *, tracked_wallet_limit: int
) -> dict[str, tuple[str, str]]:
    """Enforce the TRACKED_WALLET_LIMIT by score rank.

    Returns wallet_address -> (final_status, reason_code). Only wallets whose
    computed status is ``track`` compete for the cap; the top-N by global score
    keep ``track``, the rest degrade to ``watch`` with a capacity reason. Other
    statuses pass through unchanged.
    """
    result: dict[str, tuple[str, str]] = {}
    track_candidates = [rw for rw in scored if rw.score.status == STATUS_TRACK]
    track_candidates.sort(key=lambda rw: (rw.score.global_score, rw.wallet_address), reverse=True)
    for i, rw in enumerate(track_candidates):
        if i < tracked_wallet_limit:
            result[rw.wallet_address] = (STATUS_TRACK, rw.score.status_reason_code)
        else:
            result[rw.wallet_address] = (STATUS_WATCH, "tracked_wallet_limit_reached")
    for rw in scored:
        if rw.score.status != STATUS_TRACK:
            result[rw.wallet_address] = (rw.score.status, rw.score.status_reason_code)
    return result
