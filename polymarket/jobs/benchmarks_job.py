"""Blind benchmark recording + resolution (PRD sec 16).

record_blind_benchmark: for an observed BUY from a tracked wallet, simulate a
fixed-$10 fill against the SAME book the filtered strategy saw and store a
benchmark_trades(cohort='blind') row. Idempotent per observed trade.

resolve_benchmarks: at market resolution, compute final_pnl for blind rows using
the recorded simulated fill (settle at 1/0 by outcome vs winning_outcome).
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.models import OrderBook
from ..db import micro_to_px, micro_to_usd, px_to_micro, usd_to_micro, utcnow_iso
from ..domain import benchmarks as bm
from ..domain import paper
from .runner import JobContext

ZERO = Decimal(0)


async def record_blind_benchmark(
    ctx: JobContext,
    *,
    observed_trade_id: int,
    side: str,
    book: OrderBook | None,
    payload: dict,
) -> bool:
    """Record a blind-cohort simulated fill for an observed BUY. Idempotent."""
    if side != "BUY":
        return False
    cur = await ctx.conn.execute(
        "SELECT 1 FROM benchmark_trades WHERE observed_trade_id = ? AND cohort = 'blind'",
        (observed_trade_id,),
    )
    if await cur.fetchone() is not None:
        return False

    slippage_limit = Decimal(str(payload["hard_gates"]["max_slippage"]))
    sim = bm.simulate_blind_entry(book, slippage_limit=slippage_limit)
    if not sim.filled or sim.avg_price is None:
        # No fabricated fill: record a null-size blind row for auditability.
        await ctx.conn.execute(
            """
            INSERT INTO benchmark_trades
                (observed_trade_id, cohort, simulated_entry_price, simulated_position_size,
                 final_pnl, decision_quality_label, created_at)
            VALUES (?, 'blind', NULL, 0, NULL, 'unfilled', ?)
            """,
            (observed_trade_id, utcnow_iso()),
        )
        await ctx.conn.commit()
        return True

    await ctx.conn.execute(
        """
        INSERT INTO benchmark_trades
            (observed_trade_id, cohort, simulated_entry_price, simulated_position_size,
             final_pnl, decision_quality_label, created_at)
        VALUES (?, 'blind', ?, ?, NULL, NULL, ?)
        """,
        (
            observed_trade_id,
            px_to_micro(sim.avg_price),
            usd_to_micro(sim.cost_usd),
            utcnow_iso(),
        ),
    )
    await ctx.conn.commit()
    return True


async def resolve_benchmarks(ctx: JobContext) -> int:
    """Compute final_pnl for filled blind rows whose market resolved."""
    cur = await ctx.conn.execute(
        """
        SELECT bt.id, bt.simulated_entry_price, bt.simulated_position_size,
               ot.outcome, m.winning_outcome
          FROM benchmark_trades bt
          JOIN observed_trades ot ON ot.id = bt.observed_trade_id
          JOIN markets m ON m.condition_id = ot.condition_id
         WHERE bt.cohort = 'blind' AND bt.final_pnl IS NULL
           AND bt.simulated_entry_price IS NOT NULL
           AND m.winning_outcome IS NOT NULL
        """,
    )
    rows = await cur.fetchall()
    count = 0
    for bt_id, entry_px_micro, size_micro, outcome, winner in rows:
        avg_price = micro_to_px(int(entry_px_micro))
        cost = micro_to_usd(int(size_micro or 0))
        shares = (cost / avg_price) if avg_price > 0 else ZERO
        won = (outcome or "") == (winner or "")
        _settle, realized = paper.settlement_pnl(shares, cost, won=won)
        label = "blind_win" if realized > 0 else ("blind_loss" if realized < 0 else "blind_flat")
        await ctx.conn.execute(
            "UPDATE benchmark_trades SET final_pnl = ?, decision_quality_label = ? WHERE id = ?",
            (usd_to_micro(realized), label, bt_id),
        )
        count += 1
    await ctx.conn.commit()
    return count
