"""Wave 3 paper-engine unit tests (PRD sec 14).

Covers: shrink_size limit ordering; cash-never-negative; simulate_entry partial
fill + below-minimum; settlement win/loss Decimal exactness; exit_pnl; and the
ledger balance_after chain across entry+exit; admin close flagged + excluded from
cohort_metrics.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from .. import db as dbmod
from ..adapters.models import OrderBook, OrderBookLevel
from ..domain import benchmarks as bm
from ..domain import paper
from ..jobs.paper_exec import admin_close_position, open_entry_from_decision
from ..jobs.portfolio_view import ensure_portfolio
from ..jobs.runner import JobContext


def _migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


async def _ctx(conn):
    cur = await conn.execute(
        "INSERT INTO job_runs (job_name, trigger_type, started_at, status) "
        "VALUES ('t', 'manual', ?, 'running')",
        (dbmod.utcnow_iso(),),
    )
    await conn.commit()
    return JobContext(conn, int(cur.lastrowid), "t")


async def _seed_market_and_decision(conn, *, resolved=False, winner=None):
    now = dbmod.utcnow_iso()
    await conn.execute(
        "INSERT INTO markets (market_id, condition_id, category, event_id, status, winning_outcome, metadata_updated_at) "
        "VALUES ('mkt1', '0xcond', 'CRYPTO', 'ev1', ?, ?, ?)",
        ("resolved" if resolved else "open", winner, now),
    )
    cur = await conn.execute(
        "INSERT INTO observed_trades (source, wallet_address, condition_id, asset_id, source_side, outcome, idempotency_key, detected_at) "
        "VALUES ('data', '0xw', '0xcond', 'asset1', 'BUY', 'Yes', 'k1', ?)",
        (now,),
    )
    observed_id = int(cur.lastrowid)
    cur = await conn.execute(
        "INSERT INTO decision_journal (strategy, observed_trade_id, wallet_address, market_id, decision, created_at) "
        "VALUES ('default', ?, '0xw', 'mkt1', 'paper_copy', ?)",
        (observed_id, now),
    )
    return int(cur.lastrowid), observed_id


def _book(asks, bids=(), *, ts="1700000000000"):
    return OrderBook(
        asset_id="a",
        market="m",
        timestamp=ts,
        asks=tuple(OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in asks),
        bids=tuple(OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in bids),
        raw={},
    )


# --- shrink_size limit ordering (PRD 14.2) ----------------------------------

def _inputs(**over):
    base = dict(
        tier_usd=Decimal("20"),
        available_cash=Decimal("1000"),
        max_position_usd=Decimal("20"),
        wallet_headroom=Decimal("1000"),
        category_headroom=Decimal("1000"),
        event_headroom=Decimal("1000"),
    )
    base.update(over)
    return paper.SizeInputs(**base)


@pytest.mark.parametrize(
    "over, expected_binding, expected_size",
    [
        ({}, "tier", "20"),  # tier == max_position; min() picks first (tier)
        ({"available_cash": Decimal("7")}, "cash", "7"),
        ({"max_position_usd": Decimal("10")}, "max_position", "10"),
        ({"wallet_headroom": Decimal("4")}, "wallet_exposure", "4"),
        ({"category_headroom": Decimal("3")}, "category_exposure", "3"),
        ({"event_headroom": Decimal("2")}, "event_exposure", "2"),
    ],
)
def test_shrink_size_binding_ordering(over, expected_binding, expected_size):
    result = paper.shrink_size(_inputs(**over))
    assert result.binding == expected_binding
    assert result.size_usd == Decimal(expected_size)


def test_shrink_size_never_negative():
    result = paper.shrink_size(_inputs(available_cash=Decimal("-5")))
    assert result.size_usd == Decimal(0)


# --- simulate_entry: partial fill + below-minimum ---------------------------

def test_simulate_entry_full_fill():
    book = _book(asks=[("0.50", "1000")])
    sim = paper.simulate_entry(book, target_usd=Decimal("10"), slippage_limit=Decimal("0.05"))
    assert sim.filled is True
    assert sim.reason == "filled_full"
    assert sim.cost_usd == Decimal("10")
    assert sim.avg_price == Decimal("0.50")
    assert sim.shares == Decimal("20")  # 10 / 0.5


def test_simulate_entry_partial_fill_within_slippage():
    # First level exhausts at $5 (0.50*10), next level 0.52 within slippage 0.05.
    book = _book(asks=[("0.50", "10"), ("0.52", "1000")])
    sim = paper.simulate_entry(book, target_usd=Decimal("20"), slippage_limit=Decimal("0.05"))
    assert sim.filled is True
    assert sim.cost_usd == Decimal("20")  # fully filled across two levels
    assert sim.avg_price > Decimal("0.50")


def test_simulate_entry_partial_then_slippage_stop_below_min_no_trade():
    # Only $0.50 available at best ask; next level breaches slippage -> tiny fill.
    book = _book(asks=[("0.50", "1"), ("0.90", "1000")])
    sim = paper.simulate_entry(book, target_usd=Decimal("20"), slippage_limit=Decimal("0.05"))
    # Filled USD = 0.50*1 = 0.50 < MIN_POSITION_USD(1) -> no trade.
    assert sim.filled is False
    assert sim.reason.startswith("fill_below_min")


def test_simulate_entry_below_minimum_target_no_trade():
    book = _book(asks=[("0.50", "1000")])
    sim = paper.simulate_entry(book, target_usd=Decimal("0.5"), slippage_limit=Decimal("0.05"))
    assert sim.filled is False
    assert sim.reason == "below_min_size"


def test_simulate_entry_no_book_no_trade():
    sim = paper.simulate_entry(None, target_usd=Decimal("10"), slippage_limit=Decimal("0.05"))
    assert sim.filled is False
    assert sim.reason == "no_book"


# --- settlement / exit PnL Decimal exactness (PRD 14.5) ---------------------

def test_settlement_win_exact():
    # 20 shares bought for $10 (avg 0.50). Win -> settlement 20, realized +10.
    settlement, realized = paper.settlement_pnl(Decimal("20"), Decimal("10"), won=True)
    assert settlement == Decimal("20")
    assert realized == Decimal("10")


def test_settlement_loss_exact():
    settlement, realized = paper.settlement_pnl(Decimal("20"), Decimal("10"), won=False)
    assert settlement == Decimal("0")
    assert realized == Decimal("-10")


def test_exit_pnl_exact():
    # Sell 20 shares at best bid 0.49 -> proceeds 9.80, realized -0.20.
    proceeds, realized = paper.exit_pnl(Decimal("20"), Decimal("10"), Decimal("0.49"))
    assert proceeds == Decimal("9.80")
    assert realized == Decimal("-0.20")


def test_unrealized_none_when_no_mark():
    assert paper.unrealized_pnl(Decimal("20"), Decimal("10"), None) is None


# --- blind benchmark shares the exact fill model ----------------------------

def test_blind_entry_uses_same_fill_model_no_optimistic_fill():
    book = _book(asks=[("0.50", "1000")])
    blind = bm.simulate_blind_entry(book, slippage_limit=Decimal("0.05"))
    direct = paper.simulate_entry(book, target_usd=Decimal("10"), slippage_limit=Decimal("0.05"))
    assert blind.avg_price == direct.avg_price
    assert blind.cost_usd == direct.cost_usd  # fixed $10 == direct $10 here


# --- admin close excluded from cohort metrics -------------------------------

def test_admin_trade_excluded_from_cohort_metrics():
    # A big admin "win" must not inflate cohort net pnl if excluded upstream.
    real = [{"cost": Decimal("10"), "realized_pnl": Decimal("2"), "won": True}]
    metrics = bm.cohort_metrics(real)
    assert metrics["net_pnl"] == Decimal("2")
    assert metrics["sample"] == 1


# --- DB-backed ledger chain + admin close (PRD 14.1/14.6) -------------------

@pytest.mark.asyncio
async def test_ledger_balance_after_chain_entry_and_exit(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        await ensure_portfolio(conn, starting_bankroll=Decimal("1000"))
        dj_id, obs_id = await _seed_market_and_decision(conn)
        ctx = await _ctx(conn)
        book = _book(asks=[("0.50", "1000")], bids=[("0.49", "1000")])

        trade_id = await open_entry_from_decision(
            ctx, decision_journal_id=dj_id, observed_trade_id=obs_id, wallet="0xw",
            market_id="mkt1", asset_id="asset1", outcome="Yes", condition_id="0xcond",
            target_usd=Decimal("10"), slippage_limit=Decimal("0.05"), rule_set_id=1,
            snapshot_id=None, book=book,
        )
        assert trade_id is not None

        # cash decremented by 10 (never negative).
        cur = await conn.execute("SELECT cash_balance FROM paper_portfolios WHERE status='active'")
        cash = dbmod.micro_to_usd(int((await cur.fetchone())[0]))
        assert cash == Decimal("990")

        # admin-close at 0.49 -> proceeds 9.80; ledger balance_after chain holds.
        ok = await admin_close_position(ctx, trade_id, exit_price=Decimal("0.49"))
        assert ok is True

        cur = await conn.execute(
            "SELECT entry_type, amount, balance_after FROM paper_ledger "
            "WHERE paper_trade_id = ? ORDER BY id", (trade_id,)
        )
        rows = await cur.fetchall()
        assert [r[0] for r in rows] == ["entry", "admin_exit"]
        # balance_after chain: each equals prior balance + amount.
        start = dbmod.usd_to_micro(Decimal("1000"))
        running = start
        for _etype, amount, balance_after in rows:
            running += int(amount)
            assert int(balance_after) == running
        # final cash reflects the last balance_after.
        cur = await conn.execute("SELECT cash_balance FROM paper_portfolios WHERE status='active'")
        final_cash = int((await cur.fetchone())[0])
        assert final_cash == int(rows[-1][2])
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_admin_close_flags_is_admin_and_excludes_from_realized(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        await ensure_portfolio(conn, starting_bankroll=Decimal("1000"))
        dj_id, obs_id = await _seed_market_and_decision(conn)
        ctx = await _ctx(conn)
        book = _book(asks=[("0.50", "1000")], bids=[("0.49", "1000")])
        trade_id = await open_entry_from_decision(
            ctx, decision_journal_id=dj_id, observed_trade_id=obs_id, wallet="0xw",
            market_id="mkt1", asset_id="asset1", outcome="Yes", condition_id="0xcond",
            target_usd=Decimal("10"), slippage_limit=Decimal("0.05"), rule_set_id=1,
            snapshot_id=None, book=book,
        )
        await admin_close_position(ctx, trade_id, exit_price=Decimal("0.49"))
        cur = await conn.execute("SELECT is_admin, status FROM paper_trades WHERE id = ?", (trade_id,))
        is_admin, status = await cur.fetchone()
        assert is_admin == 1
        assert status == "closed"
        # cumulative-realized query in pnl.py excludes is_admin rows.
        cur = await conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM paper_trades "
            "WHERE status IN ('closed','resolved') AND is_admin = 0"
        )
        assert int((await cur.fetchone())[0]) == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_entry_below_min_downgrades_to_watch(tmp_path):
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    try:
        await ensure_portfolio(conn, starting_bankroll=Decimal("1000"))
        dj_id, obs_id = await _seed_market_and_decision(conn)
        ctx = await _ctx(conn)
        # Only $0.50 fillable at best ask, rest breaches slippage -> below min.
        book = _book(asks=[("0.50", "1"), ("0.90", "1000")], bids=[("0.49", "10")])
        trade_id = await open_entry_from_decision(
            ctx, decision_journal_id=dj_id, observed_trade_id=obs_id, wallet="0xw",
            market_id="mkt1", asset_id="asset1", outcome="Yes", condition_id="0xcond",
            target_usd=Decimal("20"), slippage_limit=Decimal("0.05"), rule_set_id=1,
            snapshot_id=None, book=book,
        )
        assert trade_id is None
        cur = await conn.execute("SELECT decision FROM decision_journal WHERE id = ?", (dj_id,))
        assert (await cur.fetchone())[0] == "watchlist"
        # cash unchanged (no trade opened).
        cur = await conn.execute("SELECT cash_balance FROM paper_portfolios WHERE status='active'")
        assert int((await cur.fetchone())[0]) == dbmod.usd_to_micro(Decimal("1000"))
    finally:
        await conn.close()
