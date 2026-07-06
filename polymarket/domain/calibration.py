"""Component calibration report (which score components discriminate outcomes).

Pure and deterministic. Given a set of judged decisions — each carrying its
per-component scores (``TradeComponentScores.as_dict`` keys, each 0..100), a
final decision-quality label (``domain.outcomes``) and a realized/hypothetical
PnL — this module measures, per component, how well a high score separates
GOOD outcomes from BAD ones.

Discrimination is summarised with the Mann-Whitney AUC (probability a random
GOOD record scores above a random BAD one, ties counted 0.5), the mean-score
separation, and a fixed set of score bands with their bad-rate and average PnL.

No DB/IO. Money and score math use ``Decimal``; float inputs are accepted and
converted at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from . import outcomes

ZERO = Decimal(0)
HALF = Decimal("0.5")

# Label buckets (spec §Purpose). Records with any other label are excluded.
GOOD_LABELS = frozenset({outcomes.GOOD_COPY, outcomes.GOOD_SKIP, outcomes.GOOD_WATCH})
BAD_LABELS = frozenset({outcomes.BAD_COPY, outcomes.MISSED_WINNER, outcomes.MISSED_WATCH_ENTRY})

# Fixed score bands: [0,25), [25,50), [50,75), [75,100]. The top band is closed
# on the right so a perfect 100 lands in it.
BAND_EDGES: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(0), Decimal(25)),
    (Decimal(25), Decimal(50)),
    (Decimal(50), Decimal(75)),
    (Decimal(75), Decimal(100)),
)

# Component names in report order (mirrors TradeComponentScores.as_dict()).
COMPONENT_NAMES: tuple[str, ...] = (
    "wallet_global_quality",
    "category_fit",
    "price_move_lateness",
    "executable_liquidity",
    "spread",
    "detection_latency",
    "time_to_resolution",
    "thesis_clarity",
)


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class CalibrationRecord:
    """One judged decision fed into the report.

    ``components`` maps component name -> score (0..100). ``label`` is the final
    decision-quality label. ``pnl`` is realized-if-available else hypothetical
    PnL in USD dollars (None when unavailable).
    """

    components: Mapping[str, Decimal | float]
    label: str
    decision: str
    pnl: Decimal | None


@dataclass(frozen=True)
class BandStat:
    lo: Decimal
    hi: Decimal
    n: int
    bad_rate: Decimal | None      # 0..1, None when the band is empty
    avg_pnl: Decimal | None       # None when no pnl values fell in the band


@dataclass(frozen=True)
class ComponentCalibration:
    component: str
    n_good: int
    n_bad: int
    auc: Decimal | None           # None if either class is empty
    mean_good: Decimal | None
    mean_bad: Decimal | None
    separation: Decimal | None    # mean_good - mean_bad, None if either missing
    bands: tuple[BandStat, ...]
    sufficient: bool              # n_good + n_bad >= min_sample


@dataclass(frozen=True)
class CalibrationSummary:
    total_records: int            # records with a GOOD or BAD label (others skipped)
    label_counts: Mapping[str, int]
    min_sample: int


def _band_index(score: Decimal) -> int | None:
    """Return the band index for a 0..100 score, or None if out of range.

    Bands are half-open [lo, hi) except the last, which is closed [75, 100].
    """
    if score < ZERO or score > Decimal(100):
        return None
    for idx, (lo, hi) in enumerate(BAND_EDGES):
        if idx == len(BAND_EDGES) - 1:
            if lo <= score <= hi:
                return idx
        elif lo <= score < hi:
            return idx
    return None


def _auc(good_scores: Sequence[Decimal], bad_scores: Sequence[Decimal]) -> Decimal | None:
    """Mann-Whitney AUC: P(random good > random bad), ties count 0.5.

    None when either class is empty.
    """
    if not good_scores or not bad_scores:
        return None
    wins = ZERO
    for g in good_scores:
        for b in bad_scores:
            if g > b:
                wins += Decimal(1)
            elif g == b:
                wins += HALF
    return wins / (Decimal(len(good_scores)) * Decimal(len(bad_scores)))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def _calibrate_component(
    name: str,
    records: Sequence[CalibrationRecord],
    *,
    min_sample: int,
) -> ComponentCalibration:
    good_scores: list[Decimal] = []
    bad_scores: list[Decimal] = []
    # Per-band accumulators: [n, n_bad, [pnl values]].
    band_n = [0] * len(BAND_EDGES)
    band_bad = [0] * len(BAND_EDGES)
    band_pnl: list[list[Decimal]] = [[] for _ in BAND_EDGES]

    for rec in records:
        if name not in rec.components:
            continue  # missing key -> skip this record for this component only
        raw = rec.components[name]
        if raw is None:
            continue
        score = _d(raw)
        is_good = rec.label in GOOD_LABELS
        if is_good:
            good_scores.append(score)
        else:
            bad_scores.append(score)
        idx = _band_index(score)
        if idx is not None:
            band_n[idx] += 1
            if not is_good:
                band_bad[idx] += 1
            if rec.pnl is not None:
                band_pnl[idx].append(_d(rec.pnl))

    bands: list[BandStat] = []
    for idx, (lo, hi) in enumerate(BAND_EDGES):
        n = band_n[idx]
        bad_rate = (Decimal(band_bad[idx]) / Decimal(n)) if n else None
        avg_pnl = _mean(band_pnl[idx])
        bands.append(BandStat(lo=lo, hi=hi, n=n, bad_rate=bad_rate, avg_pnl=avg_pnl))

    mean_good = _mean(good_scores)
    mean_bad = _mean(bad_scores)
    separation = (mean_good - mean_bad) if (mean_good is not None and mean_bad is not None) else None

    n_good = len(good_scores)
    n_bad = len(bad_scores)
    return ComponentCalibration(
        component=name,
        n_good=n_good,
        n_bad=n_bad,
        auc=_auc(good_scores, bad_scores),
        mean_good=mean_good,
        mean_bad=mean_bad,
        separation=separation,
        bands=tuple(bands),
        sufficient=(n_good + n_bad) >= min_sample,
    )


def component_calibration(
    records: Iterable[CalibrationRecord],
    *,
    min_sample: int = 20,
) -> tuple[dict[str, ComponentCalibration], CalibrationSummary]:
    """Calibrate every known component over the GOOD/BAD-labelled records.

    Records whose label is neither GOOD nor BAD are skipped defensively. Returns
    the per-component results plus a small summary (total judged records and a
    per-label count over the judged subset).
    """
    judged: list[CalibrationRecord] = []
    label_counts: dict[str, int] = {}
    for rec in records:
        if rec.label in GOOD_LABELS or rec.label in BAD_LABELS:
            judged.append(rec)
            label_counts[rec.label] = label_counts.get(rec.label, 0) + 1

    results = {
        name: _calibrate_component(name, judged, min_sample=min_sample)
        for name in COMPONENT_NAMES
    }
    summary = CalibrationSummary(
        total_records=len(judged),
        label_counts=label_counts,
        min_sample=min_sample,
    )
    return results, summary
