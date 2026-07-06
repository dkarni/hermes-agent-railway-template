"""Trade scoring + hard/freshness gates (PRD sec 12.4, 13).

Pure and deterministic. Inputs are a ``TradeScoreInputs`` bundle of plain data
(wallet profile row, category stat, observed trade, market, snapshot/book, fill
estimate, portfolio view, rule payload). Every number comes from the payload.

Outputs component scores (PRD 13.2), the hard-gate results (PRD 13.3) and the
freshness-gate results (PRD 12.4). The decision itself lives in decisions.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


def _d(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _clamp(value: Decimal, lo: Decimal = ZERO, hi: Decimal = HUNDRED) -> Decimal:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class MarketFacts:
    condition_id: str
    market_id: str
    category: str
    closed: bool
    resolved: bool
    paused: bool
    seconds_to_resolution: int | None    # None if unknown
    has_valid_outcome_asset: bool


@dataclass(frozen=True)
class BookFacts:
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    book_age_seconds: int | None
    is_stale: bool


@dataclass(frozen=True)
class FillFacts:
    filled_usd: Decimal
    target_usd: Decimal
    avg_price: Decimal | None
    fully_filled: bool
    stopped_reason: str


@dataclass(frozen=True)
class WalletFacts:
    wallet_address: str
    status: str
    global_score: Decimal            # 0..100
    data_quality_score: Decimal      # 0..100
    profile_age_seconds: int | None
    median_detection_delay_seconds: int | None
    proven_categories: tuple[str, ...]  # categories the wallet has a proven edge in


@dataclass(frozen=True)
class ObservedFacts:
    side: str                        # BUY | SELL
    source_price: Decimal            # [0,1]
    detection_delay_seconds: int | None
    is_duplicate: bool
    category: str
    outcome: str


@dataclass(frozen=True)
class TradeScoreInputs:
    wallet: WalletFacts
    category_stat_score: Decimal | None   # 0..100 category-fit proxy, None if absent
    observed: ObservedFacts
    market: MarketFacts
    book: BookFacts
    fill: FillFacts
    payload: Mapping


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class TradeComponentScores:
    wallet_global_quality: Decimal
    category_fit: Decimal
    price_move_lateness: Decimal
    executable_liquidity: Decimal
    spread: Decimal
    detection_latency: Decimal
    time_to_resolution: Decimal
    thesis_clarity: Decimal

    def as_dict(self) -> dict[str, float]:
        return {
            "wallet_global_quality": float(self.wallet_global_quality),
            "category_fit": float(self.category_fit),
            "price_move_lateness": float(self.price_move_lateness),
            "executable_liquidity": float(self.executable_liquidity),
            "spread": float(self.spread),
            "detection_latency": float(self.detection_latency),
            "time_to_resolution": float(self.time_to_resolution),
            "thesis_clarity": float(self.thesis_clarity),
        }


@dataclass(frozen=True)
class TradeScore:
    total_score: Decimal
    components: TradeComponentScores
    price_move_absolute: Decimal
    price_move_percent: Decimal
    executable_entry_price: Decimal | None
    hard_gates: tuple[GateResult, ...]
    freshness_gates: tuple[GateResult, ...]

    @property
    def all_hard_pass(self) -> bool:
        return all(g.passed for g in self.hard_gates)

    @property
    def all_freshness_pass(self) -> bool:
        return all(g.passed for g in self.freshness_gates)


# --- price movement ---------------------------------------------------------

def _price_move(source_price: Decimal, executable: Decimal | None) -> tuple[Decimal, Decimal]:
    if executable is None or source_price <= 0:
        return ZERO, ZERO
    absolute = executable - source_price
    percent = absolute / source_price
    return absolute, percent


# --- component scores -------------------------------------------------------

def _score_price_move_lateness(abs_move: Decimal, max_abs: Decimal) -> Decimal:
    """Adverse (positive, price rose for a BUY) moves score lower."""
    if max_abs <= 0:
        return HUNDRED
    if abs_move <= 0:
        return HUNDRED  # price moved in our favour or flat
    ratio = _clamp(abs_move / max_abs, ZERO, ONE)
    return _clamp(HUNDRED - ratio * HUNDRED)


def _score_executable_liquidity(fill: FillFacts) -> Decimal:
    if fill.target_usd <= 0:
        return ZERO
    ratio = _clamp(fill.filled_usd / fill.target_usd, ZERO, ONE)
    return _clamp(ratio * HUNDRED)


def _score_spread(spread: Decimal | None, max_spread: Decimal) -> Decimal:
    if spread is None or max_spread <= 0:
        return ZERO if spread is None else HUNDRED
    ratio = _clamp(spread / max_spread, ZERO, ONE)
    return _clamp(HUNDRED - ratio * HUNDRED)


def _score_detection_latency(delay: int | None, book_freshness_limit: Decimal) -> Decimal:
    if delay is None:
        return Decimal(50)
    limit = book_freshness_limit if book_freshness_limit > 0 else Decimal(120)
    ratio = _clamp(Decimal(delay) / limit, ZERO, ONE)
    return _clamp(HUNDRED - ratio * HUNDRED)


def _score_time_to_resolution(seconds: int | None, min_seconds: Decimal) -> Decimal:
    if seconds is None:
        return Decimal(50)
    if seconds <= 0:
        return ZERO
    # comfortable window = 10x the minimum
    comfortable = min_seconds * Decimal(10) if min_seconds > 0 else Decimal(36000)
    ratio = _clamp(Decimal(seconds) / comfortable, ZERO, ONE)
    return _clamp(ratio * HUNDRED)


def _score_thesis_clarity(inputs: TradeScoreInputs) -> Decimal:
    """Proxy: wallet data quality + category proven + spread sanity."""
    dq = inputs.wallet.data_quality_score
    proven = HUNDRED if inputs.observed.category in inputs.wallet.proven_categories else Decimal(40)
    return _clamp(dq * Decimal("0.6") + proven * Decimal("0.4"))


def score_trade(inputs: TradeScoreInputs) -> TradeScore:
    payload = inputs.payload
    weights = payload["trade_score_weights"]
    gates_cfg = payload["hard_gates"]
    fresh_cfg = payload["freshness_gates_seconds"]

    max_abs_move = _d(gates_cfg["max_price_move_absolute"])
    max_spread = _d(gates_cfg["max_spread"])
    min_ttr = _d(gates_cfg["min_time_to_resolution_seconds"])
    book_fresh_limit = _d(fresh_cfg["order_book"])

    executable = inputs.fill.avg_price
    abs_move, pct_move = _price_move(inputs.observed.source_price, executable)

    wallet_global = _clamp(inputs.wallet.global_score)
    category_fit = _clamp(
        inputs.category_stat_score if inputs.category_stat_score is not None
        else inputs.wallet.global_score
    )
    price_move = _score_price_move_lateness(abs_move, max_abs_move)
    liquidity = _score_executable_liquidity(inputs.fill)
    spread = _score_spread(inputs.book.spread, max_spread)
    latency = _score_detection_latency(inputs.observed.detection_delay_seconds, book_fresh_limit)
    ttr = _score_time_to_resolution(inputs.market.seconds_to_resolution, min_ttr)
    thesis = _score_thesis_clarity(inputs)

    components = TradeComponentScores(
        wallet_global_quality=wallet_global.quantize(Decimal("0.01")),
        category_fit=category_fit.quantize(Decimal("0.01")),
        price_move_lateness=price_move.quantize(Decimal("0.01")),
        executable_liquidity=liquidity.quantize(Decimal("0.01")),
        spread=spread.quantize(Decimal("0.01")),
        detection_latency=latency.quantize(Decimal("0.01")),
        time_to_resolution=ttr.quantize(Decimal("0.01")),
        thesis_clarity=thesis.quantize(Decimal("0.01")),
    )

    total = (
        wallet_global * _d(weights["wallet_global_quality"])
        + category_fit * _d(weights["category_fit"])
        + price_move * _d(weights["price_move_lateness"])
        + liquidity * _d(weights["executable_liquidity"])
        + spread * _d(weights["spread"])
        + latency * _d(weights["detection_latency"])
        + ttr * _d(weights["time_to_resolution"])
        + thesis * _d(weights["thesis_clarity"])
    ) / HUNDRED

    hard = _hard_gates(inputs, abs_move)
    fresh = _freshness_gates(inputs)

    return TradeScore(
        total_score=_clamp(total).quantize(Decimal("0.01")),
        components=components,
        price_move_absolute=abs_move.quantize(Decimal("0.000001")),
        price_move_percent=pct_move.quantize(Decimal("0.000001")),
        executable_entry_price=executable,
        hard_gates=hard,
        freshness_gates=fresh,
    )


# --- hard gates (PRD 13.3) --------------------------------------------------

def _hard_gates(inputs: TradeScoreInputs, abs_move: Decimal) -> tuple[GateResult, ...]:
    m = inputs.market
    b = inputs.book
    f = inputs.fill
    o = inputs.observed
    w = inputs.wallet
    gates_cfg = inputs.payload["hard_gates"]

    max_spread = _d(gates_cfg["max_spread"])
    min_depth = _d(gates_cfg["min_depth_usd"])
    max_abs_move = _d(gates_cfg["max_price_move_absolute"])
    min_ttr = int(_d(gates_cfg["min_time_to_resolution_seconds"]))

    results: list[GateResult] = []

    # 1. market state mappable
    market_ok = (not m.closed) and (not m.resolved) and (not m.paused) and m.has_valid_outcome_asset
    results.append(GateResult(
        "market_state",
        market_ok,
        "market open and mappable" if market_ok
        else f"closed={m.closed} resolved={m.resolved} paused={m.paused} valid_asset={m.has_valid_outcome_asset}",
    ))

    # 2. source data present & not stale
    fresh_ok = (not b.is_stale) and b.best_ask is not None
    results.append(GateResult(
        "source_data_fresh",
        fresh_ok,
        "book fresh" if fresh_ok else f"stale={b.is_stale} best_ask={b.best_ask}",
    ))

    # 3. depth / slippage: proposed size fillable
    depth_ok = f.filled_usd >= min_depth and f.stopped_reason != "slippage_limit"
    results.append(GateResult(
        "depth_and_slippage",
        depth_ok,
        f"filled={f.filled_usd} min_depth={min_depth} reason={f.stopped_reason}",
    ))

    # 4. spread within max
    spread_ok = b.spread is not None and b.spread <= max_spread
    results.append(GateResult(
        "spread",
        spread_ok,
        f"spread={b.spread} max={max_spread}",
    ))

    # 5. price move within max (adverse only)
    move_ok = abs_move <= max_abs_move
    results.append(GateResult(
        "price_move",
        move_ok,
        f"abs_move={abs_move} max={max_abs_move}",
    ))

    # 6. time to resolution
    ttr_ok = m.seconds_to_resolution is not None and m.seconds_to_resolution >= min_ttr
    results.append(GateResult(
        "time_to_resolution",
        ttr_ok,
        f"ttr={m.seconds_to_resolution} min={min_ttr}",
    ))

    # 7. wallet within proven category
    cat_ok = o.category in w.proven_categories if w.proven_categories else False
    results.append(GateResult(
        "wallet_category",
        cat_ok,
        f"category={o.category} proven={list(w.proven_categories)}",
    ))

    # 8. wallet status usable
    status_ok = w.status == "track"
    results.append(GateResult(
        "wallet_status",
        status_ok,
        f"status={w.status}",
    ))

    # 9. not a duplicate
    dup_ok = not o.is_duplicate
    results.append(GateResult(
        "duplicate",
        dup_ok,
        "unique" if dup_ok else "duplicate trade or thesis",
    ))

    return tuple(results)


# --- freshness gates (PRD 12.4) ---------------------------------------------

def _freshness_gates(inputs: TradeScoreInputs) -> tuple[GateResult, ...]:
    fresh_cfg = inputs.payload["freshness_gates_seconds"]
    book_max = int(_d(fresh_cfg["order_book"]))
    meta_max = int(_d(fresh_cfg["market_metadata_open"]))
    profile_max = int(_d(fresh_cfg["wallet_profile"]))

    b = inputs.book
    w = inputs.wallet
    results: list[GateResult] = []

    book_age = b.book_age_seconds
    book_ok = (not b.is_stale) and (book_age is None or book_age <= book_max)
    results.append(GateResult(
        "book_freshness",
        book_ok,
        f"age={book_age} max={book_max} stale={b.is_stale}",
    ))

    profile_ok = w.profile_age_seconds is None or w.profile_age_seconds <= profile_max
    results.append(GateResult(
        "wallet_profile_freshness",
        profile_ok,
        f"age={w.profile_age_seconds} max={profile_max}",
    ))

    # metadata freshness gate name kept for completeness (open-state age).
    results.append(GateResult(
        "market_metadata_freshness",
        True,
        f"max={meta_max}",
    ))

    return tuple(results)
