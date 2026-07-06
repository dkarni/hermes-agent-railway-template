"""Real portfolio state: bootstrap, reset, and the DbPortfolioView.

DbPortfolioView implements the Wave-2 ``PortfolioView`` Protocol from DB rows.
Domain math stays pure (domain/paper.py); this layer only queries. Exposure is
measured as current open-position *cost* (what was actually spent), consistent
with how ``entry_cost`` is recorded.
"""

from __future__ import annotations

from decimal import Decimal

import aiosqlite

from ..db import micro_to_usd, usd_to_micro, utcnow_iso

ZERO = Decimal(0)


async def get_active_portfolio_id(conn: aiosqlite.Connection, *, name: str = "default") -> int | None:
    cursor = await conn.execute(
        "SELECT id FROM paper_portfolios WHERE name = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
        (name,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else None


async def ensure_portfolio(
    conn: aiosqlite.Connection, *, starting_bankroll: Decimal, name: str = "default"
) -> int:
    """Create the single active portfolio if absent (worker init). Idempotent."""
    existing = await get_active_portfolio_id(conn, name=name)
    if existing is not None:
        return existing
    now = utcnow_iso()
    bankroll_micro = usd_to_micro(starting_bankroll)
    cursor = await conn.execute(
        """
        INSERT INTO paper_portfolios
            (name, version, starting_bankroll, cash_balance, status, peak_equity,
             started_at, created_at)
        VALUES (?, 1, ?, ?, 'active', ?, ?, ?)
        """,
        (name, bankroll_micro, bankroll_micro, bankroll_micro, now, now),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def reset_portfolio(
    conn: aiosqlite.Connection, *, starting_bankroll: Decimal, name: str = "default"
) -> int:
    """Admin reset: retire the active portfolio and create a fresh version.

    History is preserved (old row -> status 'retired', trades untouched). A new
    version row with a full bankroll becomes active. Exposed via API in Wave 4.
    """
    now = utcnow_iso()
    cursor = await conn.execute(
        "SELECT id, version FROM paper_portfolios WHERE name = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
        (name,),
    )
    row = await cursor.fetchone()
    next_version = 1
    if row is not None:
        next_version = int(row[1]) + 1
        await conn.execute(
            "UPDATE paper_portfolios SET status = 'retired', ended_at = ? WHERE id = ?",
            (now, int(row[0])),
        )
    bankroll_micro = usd_to_micro(starting_bankroll)
    cursor = await conn.execute(
        """
        INSERT INTO paper_portfolios
            (name, version, starting_bankroll, cash_balance, status, peak_equity,
             started_at, created_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (name, next_version, bankroll_micro, bankroll_micro, bankroll_micro, now, now),
    )
    await conn.commit()
    return int(cursor.lastrowid)


class DbPortfolioView:
    """Read-only portfolio view (PRD 14.3) backed by the active portfolio.

    Loaded once per job run: exposure snapshots are read eagerly so the decision
    engine sees a consistent picture during a monitor pass. All amounts are USD
    Decimals.
    """

    def __init__(
        self,
        *,
        portfolio_id: int,
        cash: Decimal,
        equity: Decimal,
        open_count: int,
        wallet_exposure: dict[str, Decimal],
        category_exposure: dict[str, Decimal],
        event_exposure: dict[str, Decimal],
        open_theses: set[tuple[str, str, str]],
        copies_today: dict[str, int],
    ) -> None:
        self.portfolio_id = portfolio_id
        self._cash = cash
        self._equity = equity
        self._open_count = open_count
        self._wallet = wallet_exposure
        self._category = category_exposure
        self._event = event_exposure
        self._theses = open_theses
        self._copies_today = copies_today

    def available_cash(self) -> Decimal:
        return self._cash

    def open_position_count(self) -> int:
        return self._open_count

    def open_position_for(self, wallet: str, condition_id: str, outcome: str) -> bool:
        return (wallet, condition_id, outcome) in self._theses

    def wallet_exposure(self, wallet: str) -> Decimal:
        return self._wallet.get(wallet, ZERO)

    def category_exposure(self, category: str) -> Decimal:
        return self._category.get(category, ZERO)

    def event_exposure(self, event_id: str) -> Decimal:
        return self._event.get(event_id, ZERO)

    def equity(self) -> Decimal:
        return self._equity

    def copies_today(self, wallet: str) -> int:
        return self._copies_today.get(wallet, 0)


async def load_portfolio_view(
    conn: aiosqlite.Connection, *, name: str = "default", today: str | None = None
) -> DbPortfolioView | None:
    """Build a DbPortfolioView from current DB state, or None if no portfolio."""
    portfolio_id = await get_active_portfolio_id(conn, name=name)
    if portfolio_id is None:
        return None

    cur = await conn.execute(
        "SELECT cash_balance FROM paper_portfolios WHERE id = ?", (portfolio_id,)
    )
    cash = micro_to_usd(int((await cur.fetchone())[0]))

    # Open positions with wallet / category / event / thesis / cost.
    cur = await conn.execute(
        """
        SELECT pt.wallet_address, pt.entry_cost, pt.unrealized_pnl,
               COALESCE(pt.current_mark, NULL), m.category, m.event_id,
               pt.market_id, pt.outcome, ot.condition_id
          FROM paper_trades pt
          LEFT JOIN markets m ON m.market_id = pt.market_id
          LEFT JOIN observed_trades ot ON ot.id = pt.observed_trade_id
         WHERE pt.paper_portfolio_id = ? AND pt.status = 'open'
        """,
        (portfolio_id,),
    )
    rows = await cur.fetchall()
    open_count = len(rows)
    wallet_exp: dict[str, Decimal] = {}
    cat_exp: dict[str, Decimal] = {}
    evt_exp: dict[str, Decimal] = {}
    theses: set[tuple[str, str, str]] = set()
    open_cost = ZERO
    unrealized = ZERO
    for wallet, cost_micro, unreal_micro, _mark, category, event_id, _mid, outcome, cond in rows:
        cost = micro_to_usd(int(cost_micro or 0))
        open_cost += cost
        if unreal_micro is not None:
            unrealized += micro_to_usd(int(unreal_micro))
        if wallet:
            wallet_exp[wallet] = wallet_exp.get(wallet, ZERO) + cost
        if category:
            cat_exp[category] = cat_exp.get(category, ZERO) + cost
        if event_id:
            evt_exp[event_id] = evt_exp.get(event_id, ZERO) + cost
        theses.add((wallet or "", cond or "", outcome or ""))

    equity = cash + open_cost + unrealized

    # Copies today per wallet (filtered cohort paper trades opened today).
    day = (today or utcnow_iso())[:10]
    cur = await conn.execute(
        """
        SELECT wallet_address, COUNT(*) FROM paper_trades
         WHERE paper_portfolio_id = ? AND is_admin = 0
           AND substr(opened_at, 1, 10) = ?
         GROUP BY wallet_address
        """,
        (portfolio_id, day),
    )
    copies = {row[0]: int(row[1]) for row in await cur.fetchall() if row[0]}

    return DbPortfolioView(
        portfolio_id=portfolio_id,
        cash=cash,
        equity=equity,
        open_count=open_count,
        wallet_exposure=wallet_exp,
        category_exposure=cat_exp,
        event_exposure=evt_exp,
        open_theses=theses,
        copies_today=copies,
    )
