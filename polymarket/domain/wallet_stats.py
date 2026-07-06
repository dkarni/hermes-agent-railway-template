"""Pure, deterministic 30-day wallet metrics (PRD sec 10.2).

No I/O: inputs are plain data (a list of observed trades and a dict of resolved
market outcomes); output is a frozen ``WalletStats`` dataclass. Jobs fetch and
persist; this module only computes so it can be unit-tested and audited.

Realized-PnL reconstruction (documented simplifications)
--------------------------------------------------------
We rebuild per (condition_id, outcome) positions from the wallet's BUY/SELL
sequence using an average-cost basis:

  * BUY: position size += shares, cost basis += shares * price.
  * SELL: realize pnl = shares_sold * (sell_price - avg_cost); reduce the
    position and its cost basis proportionally. A SELL larger than the held
    position is clamped to the held size (the remainder is ignored — we never
    model shorts, PRD 12.3).
  * Resolution: any residual open position in a *resolved* market settles at
    1.0 if its outcome won, else 0.0, realizing (settle - avg_cost) * shares.

Simplifications, by design:
  * Shares are treated as ``size`` from the source trade (Data API ``size`` is
    share count for Polymarket outcome tokens). No fees are modelled here
    (paper-fill fees belong to Wave 3's execution engine).
  * "Capital deployed" is the sum of BUY notional (shares * price), i.e. gross
    cost put to work, not peak concurrent exposure.
  * Mark-to-market PnL on still-open positions in *unresolved* markets is
    reported separately (``unrealized_pnl_micro``) and is 0 unless a mark price
    is supplied per market; callers without live marks pass none, so it is 0.
  * Positive-day ratio uses UTC calendar days of *realizing* events (SELL /
    resolution); a day with net-positive realized pnl counts positive.
  * Drawdown is estimated from the running cumulative realized-pnl curve
    ordered by realizing-event time (peak-to-trough), not a true equity curve.

All money/prices are Decimal in USD here; callers convert to micro-units when
persisting via db.usd_to_micro / px_to_micro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Iterable, Mapping, Sequence

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class StatTrade:
    """Minimal normalized view of an observed/ingested trade for stats."""

    condition_id: str
    outcome: str
    side: str            # BUY | SELL
    price: Decimal       # [0,1]
    size: Decimal        # shares
    timestamp: int       # unix seconds
    category: str = ""


@dataclass(frozen=True)
class ResolvedMarket:
    """Resolution facts for a market (condition_id keyed by caller)."""

    resolved: bool
    winning_outcome: str | None   # normalized outcome label that settled at 1.0


@dataclass(frozen=True)
class CategoryStat:
    category: str
    trade_count: int
    resolved_trade_count: int
    pnl: Decimal
    capital_deployed: Decimal
    roi: Decimal
    win_rate: Decimal            # 0..1
    wins: int
    losses: int


@dataclass(frozen=True)
class WalletStats:
    window_days: int
    trade_count: int
    buy_count: int
    sell_count: int
    resolved_trade_count: int

    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    unrealized_pnl: Decimal
    capital_deployed: Decimal
    roi: Decimal                       # net_realized / capital_deployed (0 if none)

    win_rate: Decimal                  # resolved wins / resolved outcomes (0..1)
    resolved_wins: int
    resolved_losses: int

    avg_trade_size: Decimal
    median_trade_size: Decimal
    pnl_per_trade: Decimal

    positive_day_ratio: Decimal        # 0..1
    profit_concentration_top1: Decimal  # 0..1 share of positive pnl
    profit_concentration_top3: Decimal
    profit_concentration_top5: Decimal

    drawdown_estimate: Decimal         # positive magnitude of worst peak-to-trough

    entry_timing_seconds: Decimal      # median (resolution_ts - entry_ts) across resolved buys
    trade_frequency_per_day: Decimal
    recency_seconds: int               # seconds since most recent trade (rel. to window_end)

    data_completeness_score: Decimal   # 0..1

    per_category: dict[str, CategoryStat]
    window_start_ts: int
    window_end_ts: int

    # bookkeeping surfaced for scoring/audit
    distinct_markets: int
    realized_events: int               # count of realizing events (sell/settle)


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class _Position:
    __slots__ = ("shares", "cost")

    def __init__(self) -> None:
        self.shares = ZERO
        self.cost = ZERO

    @property
    def avg_cost(self) -> Decimal:
        return (self.cost / self.shares) if self.shares > 0 else ZERO


def compute_wallet_stats(
    trades: Sequence[StatTrade],
    resolved: Mapping[str, ResolvedMarket],
    *,
    window_days: int,
    window_end_ts: int,
    executable_ratio: Decimal | None = None,
    marks: Mapping[str, Decimal] | None = None,
) -> WalletStats:
    """Reconstruct 30-day metrics from a wallet's trade history.

    ``resolved`` maps condition_id -> ResolvedMarket. ``marks`` optionally maps
    (condition_id, outcome) joined by '|' -> current mark price for unrealized
    pnl; absent marks contribute 0 unrealized pnl.
    ``executable_ratio`` is the share of trades still executable at copy size,
    supplied by the caller (needs live books) and folded into completeness/copy.
    """
    marks = marks or {}
    ordered = sorted(trades, key=lambda t: (t.timestamp, 0 if t.side == "BUY" else 1))

    positions: dict[tuple[str, str], _Position] = {}
    realizing_events: list[tuple[int, Decimal]] = []  # (ts, realized_pnl)
    per_trade_pnl: list[Decimal] = []                 # per closed/settled contribution
    capital_deployed = ZERO
    buy_count = 0
    sell_count = 0
    sizes: list[Decimal] = []
    distinct_markets: set[str] = set()
    category_rows: dict[str, dict] = {}
    entry_ts_by_key: dict[tuple[str, str], list[int]] = {}

    def _cat(cat: str) -> dict:
        return category_rows.setdefault(
            cat or "UNKNOWN",
            {"trades": 0, "resolved": 0, "pnl": ZERO, "capital": ZERO, "wins": 0, "losses": 0},
        )

    for t in ordered:
        key = (t.condition_id, t.outcome)
        pos = positions.setdefault(key, _Position())
        sizes.append(t.size)
        distinct_markets.add(t.condition_id)
        crow = _cat(t.category)
        crow["trades"] += 1
        if t.side == "BUY":
            buy_count += 1
            notional = t.price * t.size
            pos.shares += t.size
            pos.cost += notional
            capital_deployed += notional
            crow["capital"] += notional
            entry_ts_by_key.setdefault(key, []).append(t.timestamp)
        else:  # SELL — realize against average cost, no shorts
            sell_count += 1
            sell_shares = min(t.size, pos.shares)
            if sell_shares > 0:
                realized = sell_shares * (t.price - pos.avg_cost)
                pos.cost -= sell_shares * pos.avg_cost
                pos.shares -= sell_shares
                realizing_events.append((t.timestamp, realized))
                per_trade_pnl.append(realized)
                crow["pnl"] += realized

    # Settle residual open positions in resolved markets.
    resolved_wins = 0
    resolved_losses = 0
    entry_timings: list[int] = []
    for key, pos in positions.items():
        condition_id, outcome = key
        rm = resolved.get(condition_id)
        crow = _cat(_category_for(ordered, condition_id))
        if rm is not None and rm.resolved:
            settle = ONE if (rm.winning_outcome is not None and outcome == rm.winning_outcome) else ZERO
            won = settle == ONE
            if pos.shares > 0:
                realized = pos.shares * (settle - pos.avg_cost)
                realizing_events.append((window_end_ts, realized))
                per_trade_pnl.append(realized)
                crow["pnl"] += realized
            # win/loss counts every resolved position the wallet held
            if won:
                resolved_wins += 1
                crow["wins"] += 1
            else:
                resolved_losses += 1
                crow["losses"] += 1
            crow["resolved"] += 1
            for ets in entry_ts_by_key.get(key, []):
                entry_timings.append(max(0, window_end_ts - ets))
            pos.shares = ZERO
            pos.cost = ZERO

    # Unrealized on remaining open positions in unresolved markets (marks only).
    unrealized = ZERO
    for key, pos in positions.items():
        if pos.shares <= 0:
            continue
        mark = marks.get(f"{key[0]}|{key[1]}")
        if mark is not None:
            unrealized += pos.shares * (mark - pos.avg_cost)

    gross_realized = sum((p for p in per_trade_pnl if p > 0), ZERO)
    net_realized = sum(per_trade_pnl, ZERO)
    roi = _q(net_realized / capital_deployed) if capital_deployed > 0 else ZERO

    resolved_total = resolved_wins + resolved_losses
    win_rate = _q(Decimal(resolved_wins) / Decimal(resolved_total)) if resolved_total else ZERO

    trade_count = len(ordered)
    avg_size = _q(sum(sizes, ZERO) / Decimal(len(sizes))) if sizes else ZERO
    med_size = _q(Decimal(str(median([float(s) for s in sizes])))) if sizes else ZERO
    pnl_per_trade = _q(net_realized / Decimal(trade_count)) if trade_count else ZERO

    positive_day_ratio = _positive_day_ratio(realizing_events)
    top1, top3, top5 = _profit_concentration(per_trade_pnl)
    drawdown = _drawdown(realizing_events)
    entry_timing = _q(Decimal(str(median(entry_timings)))) if entry_timings else ZERO

    span_seconds = max(1, window_end_ts - (ordered[0].timestamp if ordered else window_end_ts))
    freq = _q(Decimal(trade_count) * Decimal(86400) / Decimal(span_seconds)) if trade_count else ZERO
    recency = (window_end_ts - ordered[-1].timestamp) if ordered else span_seconds

    completeness = _completeness(
        trade_count=trade_count,
        resolved_total=resolved_total,
        has_categories=any(t.category for t in ordered),
        executable_ratio=executable_ratio,
    )

    per_category = {
        cat: _finalize_category(cat, row)
        for cat, row in category_rows.items()
    }

    return WalletStats(
        window_days=window_days,
        trade_count=trade_count,
        buy_count=buy_count,
        sell_count=sell_count,
        resolved_trade_count=resolved_total,
        gross_realized_pnl=_q(gross_realized),
        net_realized_pnl=_q(net_realized),
        unrealized_pnl=_q(unrealized),
        capital_deployed=_q(capital_deployed),
        roi=roi,
        win_rate=win_rate,
        resolved_wins=resolved_wins,
        resolved_losses=resolved_losses,
        avg_trade_size=avg_size,
        median_trade_size=med_size,
        pnl_per_trade=pnl_per_trade,
        positive_day_ratio=positive_day_ratio,
        profit_concentration_top1=top1,
        profit_concentration_top3=top3,
        profit_concentration_top5=top5,
        drawdown_estimate=drawdown,
        entry_timing_seconds=entry_timing,
        trade_frequency_per_day=freq,
        recency_seconds=int(recency),
        data_completeness_score=completeness,
        per_category=per_category,
        window_start_ts=(ordered[0].timestamp if ordered else window_end_ts),
        window_end_ts=window_end_ts,
        distinct_markets=len(distinct_markets),
        realized_events=len(realizing_events),
    )


def _category_for(trades: Sequence[StatTrade], condition_id: str) -> str:
    for t in trades:
        if t.condition_id == condition_id and t.category:
            return t.category
    return "UNKNOWN"


def _finalize_category(cat: str, row: dict) -> CategoryStat:
    capital = row["capital"]
    pnl = row["pnl"]
    roi = _q(pnl / capital) if capital > 0 else ZERO
    resolved = row["resolved"]
    win_rate = _q(Decimal(row["wins"]) / Decimal(resolved)) if resolved else ZERO
    return CategoryStat(
        category=cat,
        trade_count=row["trades"],
        resolved_trade_count=resolved,
        pnl=_q(pnl),
        capital_deployed=_q(capital),
        roi=roi,
        win_rate=win_rate,
        wins=row["wins"],
        losses=row["losses"],
    )


def _positive_day_ratio(events: Iterable[tuple[int, Decimal]]) -> Decimal:
    by_day: dict[str, Decimal] = {}
    for ts, pnl in events:
        by_day[_day_key(ts)] = by_day.get(_day_key(ts), ZERO) + pnl
    if not by_day:
        return ZERO
    positive = sum(1 for v in by_day.values() if v > 0)
    return _q(Decimal(positive) / Decimal(len(by_day)))


def _profit_concentration(per_trade_pnl: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    gains = sorted((p for p in per_trade_pnl if p > 0), reverse=True)
    total = sum(gains, ZERO)
    if total <= 0:
        return ZERO, ZERO, ZERO
    top1 = sum(gains[:1], ZERO)
    top3 = sum(gains[:3], ZERO)
    top5 = sum(gains[:5], ZERO)
    return _q(top1 / total), _q(top3 / total), _q(top5 / total)


def _drawdown(events: Sequence[tuple[int, Decimal]]) -> Decimal:
    ordered = sorted(events, key=lambda e: e[0])
    cumulative = ZERO
    peak = ZERO
    max_dd = ZERO
    for _ts, pnl in ordered:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return _q(max_dd)


def _completeness(
    *,
    trade_count: int,
    resolved_total: int,
    has_categories: bool,
    executable_ratio: Decimal | None,
) -> Decimal:
    """Blend of sample size, resolved fraction, category presence, executability.

    Deterministic, capped 0..1. Not a probabilistic score — a coverage heuristic.
    """
    if trade_count == 0:
        return ZERO
    sample = min(ONE, Decimal(trade_count) / Decimal(30))
    resolved_frac = min(ONE, Decimal(resolved_total) / Decimal(10))
    cat = ONE if has_categories else Decimal("0.5")
    execu = executable_ratio if executable_ratio is not None else Decimal("0.75")
    score = (sample * Decimal("0.4") + resolved_frac * Decimal("0.4")
             + cat * Decimal("0.1") + execu * Decimal("0.1"))
    return _q(min(ONE, score))
