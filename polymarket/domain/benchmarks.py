"""Benchmark cohorts & performance metrics (PRD sec 16).

Blind cohort (§16.2): for EVERY eligible observed BUY from a tracked-or-better
wallet — the trades a blind copier would copy — simulate a fixed-size fill with
the SAME snapshot + fill model as the filtered strategy (no optimistic fills, no
quality filters). Recorded in benchmark_trades(cohort='blind').

Metrics (§16.3) are pure functions over lists of trade dicts, returned as plain
dicts for reports/dashboard. A comparison refuses an "edge" claim below the
minimum benchmark sample (from the rule payload, default 20).
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.models import OrderBook
from ..domain import paper

ZERO = Decimal(0)
BLIND_FIXED_SIZE_USD = Decimal("10")  # §16.2 fixed-size blind policy


def simulate_blind_entry(
    book: OrderBook | None, *, slippage_limit: Decimal, size_usd: Decimal = BLIND_FIXED_SIZE_USD
) -> paper.EntrySim:
    """Blind cohort entry: identical execution model, fixed $10 size."""
    return paper.simulate_entry(book, target_usd=size_usd, slippage_limit=slippage_limit)


def min_benchmark_sample(payload: dict) -> int:
    return int(payload.get("evidence", {}).get("min_benchmark_sample", 20))


# --- metrics (§16.3) — pure over rows of {realized_pnl, cost, won, ...} ------

def cohort_metrics(trades: list[dict]) -> dict:
    """Aggregate metrics over resolved trades.

    Each trade dict: {'realized_pnl': Decimal, 'cost': Decimal, 'won': bool}.
    All amounts are USD Decimals. Returns a dict of plain numbers/Decimals.
    """
    n = len(trades)
    if n == 0:
        return {
            "sample": 0, "net_pnl": ZERO, "roi": ZERO, "win_rate": ZERO,
            "avg_pnl": ZERO, "profit_factor": None, "max_drawdown": ZERO,
            "wins": 0, "losses": 0, "capital_deployed": ZERO,
        }
    net = sum((t["realized_pnl"] for t in trades), ZERO)
    capital = sum((t["cost"] for t in trades), ZERO)
    wins = sum(1 for t in trades if t["realized_pnl"] > 0)
    losses = sum(1 for t in trades if t["realized_pnl"] < 0)
    gross_profit = sum((t["realized_pnl"] for t in trades if t["realized_pnl"] > 0), ZERO)
    gross_loss = sum((-t["realized_pnl"] for t in trades if t["realized_pnl"] < 0), ZERO)
    roi = (net / capital) if capital > 0 else ZERO
    win_rate = Decimal(wins) / Decimal(n)
    avg_pnl = net / Decimal(n)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    return {
        "sample": n,
        "net_pnl": net,
        "roi": roi,
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown([t["realized_pnl"] for t in trades]),
        "wins": wins,
        "losses": losses,
        "capital_deployed": capital,
    }


def max_drawdown(pnl_sequence: list[Decimal]) -> Decimal:
    """Max peak-to-trough drop of the cumulative realized-pnl equity curve."""
    peak = ZERO
    cum = ZERO
    max_dd = ZERO
    for pnl in pnl_sequence:
        cum += pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def compare(filtered: dict, blind: dict, *, min_sample: int) -> dict:
    """Filtered vs blind. Refuses an edge claim below min_sample in either cohort."""
    enough = filtered["sample"] >= min_sample and blind["sample"] >= min_sample
    delta = filtered["net_pnl"] - blind["net_pnl"]
    if not enough:
        verdict = "insufficient_sample"
    elif delta > 0:
        verdict = "filtered_better"
    elif delta < 0:
        verdict = "blind_better"
    else:
        verdict = "tie"
    return {
        "filtered": filtered,
        "blind": blind,
        "filtered_minus_blind_pnl": delta,
        "min_sample": min_sample,
        "sufficient_sample": enough,
        "verdict": verdict,
    }
