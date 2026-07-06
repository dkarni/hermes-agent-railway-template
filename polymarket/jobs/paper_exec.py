"""Paper execution glue (PRD sec 14.4/14.6).

Entry: called from monitor after a ``paper_copy`` decision. Reuses the decision's
stored market-snapshot book to walk asks for the tier size (already shrunk to
cash/exposure caps by the decision engine), records a paper_trades row plus a
paper_ledger 'entry' movement, all inside one sqlite transaction with the cash
update. If the fill falls below the minimum the decision is converted to
``watchlist`` in the journal instead.

Exits (close_due_positions, run from reconcile):
  (a) final resolution -> settle at 1/0, ledger 'settlement';
  (b) qualifying source SELL (journal ``sell_position_close_candidate``) ->
      close at current best bid from a fresh snapshot, ledger 'exit';
  (c) admin close (function only, is_admin, excluded from strategy metrics).
Every exit preserves the original decision (paper_trades keeps decision link).
"""

from __future__ import annotations

import json
from decimal import Decimal

import aiosqlite

from ..adapters.clob import ClobAdapter
from ..adapters.models import OrderBook
from ..db import (
    json_dumps,
    micro_to_px,
    micro_to_usd,
    px_to_micro,
    usd_to_micro,
    utcnow_iso,
)
from ..domain import paper
from .portfolio_view import get_active_portfolio_id
from .runner import JobContext

ZERO = Decimal(0)


# --- monitor callback -------------------------------------------------------

async def paper_copy_callback(
    ctx: JobContext,
    *,
    decision_journal_id: int,
    observed_trade_id: int,
    wallet: str,
    market,
    asset_id: str | None,
    outcome: str,
    condition_id: str,
    target_usd: Decimal,
    rule_set_id: int,
    snapshot_id: int | None,
    book: OrderBook | None,
    payload: dict,
) -> None:
    """Adapter passed to run_monitor(on_paper_copy=...) — opens the entry."""
    slippage_limit = Decimal(str(payload["hard_gates"]["max_slippage"]))
    await open_entry_from_decision(
        ctx,
        decision_journal_id=decision_journal_id,
        observed_trade_id=observed_trade_id,
        wallet=wallet,
        market_id=(market.market_id if market else None),
        asset_id=asset_id,
        outcome=outcome,
        condition_id=condition_id,
        target_usd=target_usd,
        slippage_limit=slippage_limit,
        rule_set_id=rule_set_id,
        snapshot_id=snapshot_id,
        book=book,
    )


# --- entry ------------------------------------------------------------------

async def open_entry_from_decision(
    ctx: JobContext,
    *,
    decision_journal_id: int,
    observed_trade_id: int,
    wallet: str,
    market_id: str | None,
    asset_id: str | None,
    outcome: str,
    condition_id: str,
    target_usd: Decimal,
    slippage_limit: Decimal,
    rule_set_id: int,
    snapshot_id: int | None,
    book: OrderBook | None,
) -> int | None:
    """Open a paper trade for a paper_copy decision. Returns trade id or None.

    On None (no fill within limits) the decision row is downgraded to watchlist
    with reason ``entry_size_zero`` so the journal reflects reality. The whole
    entry (trade insert + ledger + cash decrement) is one transaction.
    """
    portfolio_id = await get_active_portfolio_id(ctx.conn)
    if portfolio_id is None:
        return None

    cur = await ctx.conn.execute(
        "SELECT cash_balance FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    cash = micro_to_usd(int((await cur.fetchone())[0]))
    effective_target = min(target_usd, cash)

    sim = paper.simulate_entry(
        book, target_usd=effective_target, slippage_limit=slippage_limit
    )
    if not sim.filled:
        await _downgrade_to_watch(ctx.conn, decision_journal_id, sim.reason)
        return None

    cost_micro = usd_to_micro(sim.cost_usd)
    if cost_micro > int(usd_to_micro(cash)):
        # Never allow negative cash (invariant).
        await _downgrade_to_watch(ctx.conn, decision_journal_id, "insufficient_cash")
        return None

    now = utcnow_iso()
    trade_cur = await ctx.conn.execute(
        """
        INSERT INTO paper_trades (
            paper_portfolio_id, decision_journal_id, observed_trade_id, wallet_address,
            market_id, asset_id, outcome, status, shares, entry_price, entry_best_ask,
            entry_slippage, entry_fee, entry_cost, rule_set_id, benchmark_cohort,
            current_mark, unrealized_pnl, opened_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, ?, ?, 'filtered', ?, 0, ?, ?)
        """,
        (
            portfolio_id,
            decision_journal_id,
            observed_trade_id,
            wallet,
            market_id,
            asset_id,
            outcome,
            usd_to_micro(sim.shares),  # micro-shares
            px_to_micro(sim.avg_price),
            px_to_micro(sim.best_ask) if sim.best_ask is not None else None,
            usd_to_micro(sim.slippage),
            cost_micro,
            rule_set_id,
            px_to_micro(sim.avg_price),
            now,
            now,
        ),
    )
    trade_id = int(trade_cur.lastrowid)

    new_cash_micro = int(usd_to_micro(cash)) - cost_micro
    await ctx.conn.execute(
        "UPDATE paper_portfolios SET cash_balance = ? WHERE id = ?",
        (new_cash_micro, portfolio_id),
    )
    await _ledger(
        ctx.conn, portfolio_id, trade_id, "entry", -cost_micro, new_cash_micro,
        {"reason": sim.reason, "shares_micro": usd_to_micro(sim.shares),
         "avg_price": str(sim.avg_price)},
    )
    await ctx.conn.commit()
    ctx.written()
    return trade_id


async def _downgrade_to_watch(conn: aiosqlite.Connection, decision_journal_id: int, reason: str) -> None:
    await conn.execute(
        """
        UPDATE decision_journal
           SET decision = 'watchlist',
               decision_reason_code = ?,
               expected_position_usd = 0
         WHERE id = ?
        """,
        (f"entry_size_zero:{reason}", decision_journal_id),
    )


# --- exits: close_due_positions (run from reconcile) ------------------------

async def close_due_positions(ctx: JobContext, clob: ClobAdapter) -> dict:
    """Settle resolved markets and process qualifying source SELL closes."""
    portfolio_id = await get_active_portfolio_id(ctx.conn)
    if portfolio_id is None:
        return {"settled": 0, "sell_closed": 0}

    settled = await _settle_resolved(ctx, portfolio_id)
    sell_closed = await _close_sell_candidates(ctx, clob, portfolio_id)
    return {"settled": settled, "sell_closed": sell_closed}


async def _settle_resolved(ctx: JobContext, portfolio_id: int) -> int:
    """Settle every open trade whose market has a winning_outcome set."""
    cur = await ctx.conn.execute(
        """
        SELECT pt.id, pt.shares, pt.entry_cost, pt.outcome, m.winning_outcome
          FROM paper_trades pt
          JOIN markets m ON m.market_id = pt.market_id
         WHERE pt.paper_portfolio_id = ? AND pt.status = 'open'
           AND m.winning_outcome IS NOT NULL
        """,
        (portfolio_id,),
    )
    rows = await cur.fetchall()
    count = 0
    for trade_id, shares_micro, cost_micro, outcome, winner in rows:
        shares = micro_to_usd(int(shares_micro or 0))
        cost = micro_to_usd(int(cost_micro or 0))
        won = (outcome or "") == (winner or "")
        settlement_value, realized = paper.settlement_pnl(shares, cost, won=won)
        await _apply_exit(
            ctx, portfolio_id, trade_id,
            proceeds=settlement_value, realized=realized,
            exit_price=(Decimal(1) if won else ZERO),
            exit_reason="settlement", entry_type="settlement", status="resolved",
        )
        count += 1
    return count


async def _close_sell_candidates(ctx: JobContext, clob: ClobAdapter, portfolio_id: int) -> int:
    """Close open trades whose source wallet SELL was flagged as a close candidate.

    The SELL journal entry carries reason ``sell_position_close_candidate`` and a
    matching (wallet, condition_id, outcome). We close the still-open paper
    thesis at a fresh best-bid snapshot. Missing book => skip (no fabricated exit).
    """
    cur = await ctx.conn.execute(
        """
        SELECT dj.id, dj.wallet_address, ot.condition_id, ot.outcome
          FROM decision_journal dj
          JOIN observed_trades ot ON ot.id = dj.observed_trade_id
         WHERE dj.decision_reason_code = 'sell_position_close_candidate'
        """,
    )
    candidates = await cur.fetchall()
    count = 0
    for _dj_id, wallet, cond, outcome in candidates:
        pt = await ctx.conn.execute(
            """
            SELECT pt.id, pt.asset_id, pt.shares, pt.entry_cost
              FROM paper_trades pt
              JOIN observed_trades ot ON ot.id = pt.observed_trade_id
             WHERE pt.paper_portfolio_id = ? AND pt.status = 'open'
               AND pt.wallet_address = ? AND ot.condition_id = ? AND pt.outcome = ?
             LIMIT 1
            """,
            (portfolio_id, wallet, cond, outcome),
        )
        row = await pt.fetchone()
        if row is None:
            continue
        trade_id, asset_id, shares_micro, cost_micro = row
        if not asset_id:
            continue
        try:
            book = await clob.get_book(asset_id)
        except Exception:  # noqa: BLE001
            await ctx.add_data_quality_event(
                severity="warning", event_type="sell_close_book_failed", source="clob",
                entity_type="asset", entity_id=asset_id,
            )
            continue
        if book.best_bid is None:
            continue
        exit_price = book.best_bid.price
        shares = micro_to_usd(int(shares_micro or 0))
        cost = micro_to_usd(int(cost_micro or 0))
        proceeds, realized = paper.exit_pnl(shares, cost, exit_price)
        await _apply_exit(
            ctx, portfolio_id, trade_id,
            proceeds=proceeds, realized=realized, exit_price=exit_price,
            exit_reason="source_sell", entry_type="exit", status="closed",
        )
        count += 1
    return count


async def admin_close_position(ctx: JobContext, trade_id: int, *, exit_price: Decimal) -> bool:
    """Administrative close for data-repair testing (PRD 14.6c).

    Flagged is_admin=1 and excluded from strategy metrics. Uses a supplied
    exit price (data repair), not an invented one. Returns True if closed.
    """
    portfolio_id = await get_active_portfolio_id(ctx.conn)
    if portfolio_id is None:
        return False
    cur = await ctx.conn.execute(
        "SELECT shares, entry_cost, status FROM paper_trades WHERE id = ? AND paper_portfolio_id = ?",
        (trade_id, portfolio_id),
    )
    row = await cur.fetchone()
    if row is None or row[2] != "open":
        return False
    shares = micro_to_usd(int(row[0] or 0))
    cost = micro_to_usd(int(row[1] or 0))
    proceeds, realized = paper.exit_pnl(shares, cost, exit_price)
    await ctx.conn.execute(
        "UPDATE paper_trades SET is_admin = 1 WHERE id = ?", (trade_id,)
    )
    await _apply_exit(
        ctx, portfolio_id, trade_id,
        proceeds=proceeds, realized=realized, exit_price=exit_price,
        exit_reason="admin_close", entry_type="admin_exit", status="closed",
    )
    return True


async def _apply_exit(
    ctx: JobContext,
    portfolio_id: int,
    trade_id: int,
    *,
    proceeds: Decimal,
    realized: Decimal,
    exit_price: Decimal,
    exit_reason: str,
    entry_type: str,
    status: str,
) -> None:
    """Credit proceeds to cash, mark the trade closed, ledger the movement. One txn."""
    proceeds_micro = usd_to_micro(proceeds)
    cur = await ctx.conn.execute(
        "SELECT cash_balance FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    cash_micro = int((await cur.fetchone())[0])
    new_cash = cash_micro + proceeds_micro
    now = utcnow_iso()
    await ctx.conn.execute(
        """
        UPDATE paper_trades
           SET status = ?, exit_price = ?, exit_fee = 0, exit_reason = ?,
               realized_pnl = ?, current_mark = ?, unrealized_pnl = 0,
               mark_updated_at = ?, closed_at = ?
         WHERE id = ?
        """,
        (
            status,
            px_to_micro(exit_price),
            exit_reason,
            usd_to_micro(realized),
            px_to_micro(exit_price),
            now,
            now,
            trade_id,
        ),
    )
    await ctx.conn.execute(
        "UPDATE paper_portfolios SET cash_balance = ? WHERE id = ?",
        (new_cash, portfolio_id),
    )
    await _ledger(
        ctx.conn, portfolio_id, trade_id, entry_type, proceeds_micro, new_cash,
        {"exit_reason": exit_reason, "realized_micro": usd_to_micro(realized)},
    )
    await ctx.conn.commit()
    ctx.written()


async def _ledger(
    conn: aiosqlite.Connection,
    portfolio_id: int,
    trade_id: int | None,
    entry_type: str,
    amount_micro: int,
    balance_after_micro: int,
    metadata: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO paper_ledger
            (paper_portfolio_id, paper_trade_id, entry_type, amount, balance_after,
             created_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            portfolio_id,
            trade_id,
            entry_type,
            int(amount_micro),
            int(balance_after_micro),
            utcnow_iso(),
            json_dumps(metadata),
        ),
    )
