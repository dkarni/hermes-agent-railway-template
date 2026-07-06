"""Hourly paper PnL snapshot (PRD sec 9, 14.5).

For each open paper trade: fetch the current book (clob), mark at the executable
exit price (best bid), update per-trade current_mark / unrealized_pnl. A stale or
missing book NEVER invents a price — the last mark is carried and the trade is
flagged mark_is_stale, and a data_quality_event is recorded.

Then write a portfolio-level pnl_snapshots row: cash, open_cost, unrealized,
realized (cumulative), equity, drawdown vs peak equity. Also snapshots the market
data used (a market_snapshots row per fetched asset).
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.clob import ClobAdapter
from ..adapters.models import OrderBook
from ..db import (
    json_or_none,
    micro_to_usd,
    px_to_micro,
    usd_to_micro,
    utcnow_iso,
)
from ..domain import paper
from .portfolio_view import get_active_portfolio_id
from .runner import JobContext

ZERO = Decimal(0)


async def run_pnl(ctx: JobContext, clob: ClobAdapter) -> dict:
    portfolio_id = await get_active_portfolio_id(ctx.conn)
    if portfolio_id is None:
        return {"open_trades": 0, "marked": 0, "stale": 0}

    cur = await ctx.conn.execute(
        "SELECT cash_balance, peak_equity FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    row = await cur.fetchone()
    cash = micro_to_usd(int(row[0]))
    peak_equity = micro_to_usd(int(row[1])) if row[1] is not None else cash

    cur = await ctx.conn.execute(
        """
        SELECT id, asset_id, market_id, shares, entry_cost, current_mark, unrealized_pnl
          FROM paper_trades
         WHERE paper_portfolio_id = ? AND status = 'open'
        """,
        (portfolio_id,),
    )
    trades = await cur.fetchall()

    open_cost = ZERO
    unrealized = ZERO
    marked = 0
    stale = 0
    now = utcnow_iso()

    for trade_id, asset_id, market_id, shares_micro, cost_micro, cur_mark, cur_unreal in trades:
        shares = micro_to_usd(int(shares_micro or 0))
        cost = micro_to_usd(int(cost_micro or 0))
        open_cost += cost

        best_bid = await _fresh_best_bid(ctx, clob, asset_id, market_id, now)
        if best_bid is None:
            # Carry last mark, flag stale, never fabricate a price.
            stale += 1
            await ctx.conn.execute(
                "UPDATE paper_trades SET mark_is_stale = 1, mark_updated_at = ? WHERE id = ?",
                (now, trade_id),
            )
            if cur_unreal is not None:
                unrealized += micro_to_usd(int(cur_unreal))
            continue

        unreal = paper.unrealized_pnl(shares, cost, best_bid)
        unrealized += unreal
        marked += 1
        await ctx.conn.execute(
            """
            UPDATE paper_trades
               SET current_mark = ?, unrealized_pnl = ?, mark_is_stale = 0, mark_updated_at = ?
             WHERE id = ?
            """,
            (px_to_micro(best_bid), usd_to_micro(unreal), now, trade_id),
        )

    realized = await _cumulative_realized(ctx, portfolio_id)
    equity = cash + open_cost + unrealized
    if equity > peak_equity:
        peak_equity = equity
    drawdown = max(ZERO, peak_equity - equity)

    await ctx.conn.execute(
        "UPDATE paper_portfolios SET peak_equity = ? WHERE id = ?",
        (usd_to_micro(peak_equity), portfolio_id),
    )
    await ctx.conn.execute(
        """
        INSERT INTO pnl_snapshots
            (paper_portfolio_id, cash_balance, open_cost, unrealized_pnl, realized_pnl,
             equity, drawdown, price_source, source_timestamp, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'clob_best_bid', ?, ?)
        """,
        (
            portfolio_id,
            usd_to_micro(cash),
            usd_to_micro(open_cost),
            usd_to_micro(unrealized),
            usd_to_micro(realized),
            usd_to_micro(equity),
            usd_to_micro(drawdown),
            now,
            now,
        ),
    )
    await ctx.conn.commit()
    ctx.written()
    return {"open_trades": len(trades), "marked": marked, "stale": stale}


async def _fresh_best_bid(
    ctx: JobContext, clob: ClobAdapter, asset_id: str | None, market_id: str | None, now: str
) -> Decimal | None:
    """Fetch a fresh book and snapshot it; return best bid or None if unavailable."""
    if not asset_id:
        return None
    try:
        book = await clob.get_book(asset_id)
    except Exception:  # noqa: BLE001
        await ctx.add_data_quality_event(
            severity="warning", event_type="pnl_book_fetch_failed", source="clob",
            entity_type="asset", entity_id=asset_id,
        )
        return None
    if book.best_bid is None:
        await ctx.add_data_quality_event(
            severity="warning", event_type="pnl_book_empty", source="clob",
            entity_type="asset", entity_id=asset_id,
        )
        return None
    await _snapshot_book(ctx, book, market_id, now)
    return book.best_bid.price


async def _snapshot_book(ctx: JobContext, book: OrderBook, market_id: str | None, now: str) -> None:
    best_bid = book.best_bid.price if book.best_bid else None
    best_ask = book.best_ask.price if book.best_ask else None
    spread = book.spread
    await ctx.conn.execute(
        """
        INSERT INTO market_snapshots
            (market_id, asset_id, best_bid, best_ask, spread, data_source,
             source_timestamp, collected_at, is_stale, raw_json)
        VALUES (?, ?, ?, ?, ?, 'clob', ?, ?, 0, ?)
        """,
        (
            market_id,
            book.asset_id,
            px_to_micro(best_bid) if best_bid is not None else None,
            px_to_micro(best_ask) if best_ask is not None else None,
            px_to_micro(spread) if spread is not None and spread >= 0 else None,
            book.timestamp or None,
            now,
            json_or_none(book.raw),
        ),
    )


async def _cumulative_realized(ctx: JobContext, portfolio_id: int) -> Decimal:
    cur = await ctx.conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_trades
         WHERE paper_portfolio_id = ? AND status IN ('closed', 'resolved') AND is_admin = 0
        """,
        (portfolio_id,),
    )
    row = await cur.fetchone()
    return micro_to_usd(int(row[0] or 0))
