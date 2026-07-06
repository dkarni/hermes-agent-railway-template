from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from ..adapters import clob, gamma
from ..adapters.dataapi import DataApiAdapter
from ..adapters.models import OrderBook, OrderBookLevel
from ..http import AllowlistClient
from .conftest import load_fixture

HOST_DATA = "data-api.polymarket.com"


# --- leaderboard ------------------------------------------------------------

def test_leaderboard_parse_overall():
    from ..adapters.dataapi import _parse_leaderboard_row

    rows = load_fixture("leaderboard_overall.json")
    entry = _parse_leaderboard_row(rows[0], category="OVERALL", time_period="MONTH", order_by="PNL")
    assert entry.wallet_address.startswith("0x")
    assert entry.rank == 1
    assert isinstance(entry.pnl, Decimal)
    assert entry.category == "OVERALL"


def test_leaderboard_politics_fixture():
    from ..adapters.dataapi import _parse_leaderboard_row

    rows = load_fixture("leaderboard_politics.json")
    assert len(rows) == 5
    entry = _parse_leaderboard_row(rows[0], category="POLITICS", time_period="MONTH", order_by="PNL")
    assert entry.category == "POLITICS"


# --- trades -----------------------------------------------------------------

def test_wallet_trades_parse():
    from ..adapters.dataapi import _parse_trade_row

    rows = load_fixture("wallet_trades.json")
    trade = _parse_trade_row(rows[0])
    assert trade.side in ("BUY", "SELL")
    assert trade.asset_id
    assert trade.condition_id.startswith("0x")
    assert isinstance(trade.price, Decimal)
    assert isinstance(trade.size, Decimal)
    assert trade.timestamp > 0


# --- gamma ------------------------------------------------------------------

def test_gamma_market_parse():
    rows = load_fixture("gamma_market.json")
    market = gamma.parse_market(rows[0])
    assert market.condition_id.startswith("0x")
    # clobTokenIds is a JSON-encoded string inside JSON -> decoded to 2 asset ids
    assert len(market.clob_token_ids) == 2
    assert market.yes_asset_id == market.clob_token_ids[0]
    assert market.no_asset_id == market.clob_token_ids[1]
    assert market.outcomes  # decoded outcomes present
    assert all(isinstance(p, Decimal) for p in market.outcome_prices)
    assert market.closed is False


# --- clob order book --------------------------------------------------------

def test_orderbook_normalization_from_fixture():
    raw = load_fixture("clob_book.json")
    book = clob.normalize_book(raw)
    # best ask must be the lowest ask and strictly less than the worst ask
    assert book.best_ask is not None and book.best_ask.price < book.asks[-1].price
    # best bid must be the highest bid
    assert book.best_bid is not None and book.best_bid.price > book.bids[-1].price
    # spread positive
    assert book.spread is not None and book.spread > 0


def test_orderbook_normalization_reversed_order():
    # Fixture-style raw with documented reversed ordering (asks descending,
    # bids ascending). After normalization best_ask < worst_ask.
    raw = {
        "asset_id": "tok",
        "market": "m",
        "timestamp": "1",
        "asks": [
            {"price": "0.90", "size": "100"},
            {"price": "0.70", "size": "100"},
            {"price": "0.55", "size": "100"},  # best ask is last
        ],
        "bids": [
            {"price": "0.10", "size": "100"},
            {"price": "0.40", "size": "100"},
            {"price": "0.50", "size": "100"},  # best bid is last
        ],
    }
    book = clob.normalize_book(raw)
    assert book.best_ask.price == Decimal("0.55")
    assert book.best_ask.price < book.asks[-1].price
    assert book.best_bid.price == Decimal("0.50")
    assert book.best_bid.price > book.bids[-1].price


def _book(asks):
    return OrderBook(
        asset_id="t", market="m", timestamp="1",
        bids=(), asks=tuple(OrderBookLevel(Decimal(p), Decimal(s)) for p, s in asks), raw={},
    )


def test_estimate_fill_walks_ladder():
    # asks best-first: 0.50 x 10sh ($5), 0.60 x 100sh
    book = _book([("0.50", "10"), ("0.60", "100")])
    est = clob.estimate_fill(book, usd_amount_micro=10_000_000, side="BUY")  # $10
    # $5 at 0.50 -> 10 shares; remaining $5 at 0.60 -> 8.333 shares
    assert est.fully_filled
    assert est.stopped_reason == "complete"
    assert est.filled_usd == Decimal("10")
    assert est.avg_price is not None and Decimal("0.5") < est.avg_price < Decimal("0.6")


def test_estimate_fill_partial_book_exhausted():
    book = _book([("0.50", "10")])  # only $5 of capacity
    est = clob.estimate_fill(book, usd_amount_micro=10_000_000, side="BUY")
    assert not est.fully_filled
    assert est.stopped_reason == "book_exhausted"
    assert est.filled_usd == Decimal("5")
    assert est.filled_shares == Decimal("10")


def test_estimate_fill_slippage_limit_stop():
    book = _book([("0.50", "2"), ("0.70", "100")])  # $1 at best, then a jump
    est = clob.estimate_fill(
        book, usd_amount_micro=10_000_000, side="BUY", slippage_limit=Decimal("0.05")
    )
    # 0.70 > 0.50 + 0.05 -> stop after the first level
    assert est.stopped_reason == "slippage_limit"
    assert not est.fully_filled
    assert est.filled_usd == Decimal("1")


def test_estimate_fill_empty_book():
    book = _book([])
    est = clob.estimate_fill(book, usd_amount_micro=5_000_000, side="BUY")
    assert est.stopped_reason == "empty_book"
    assert est.avg_price is None


# --- pagination helper ------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_stops_at_lookback_and_dedupes():
    # Build 3 pages of 2 trades each; timestamps descend. Cutoff excludes ts<300.
    pages = {
        0: [
            {"proxyWallet": "0xw", "side": "BUY", "asset": "a", "conditionId": "0x1",
             "size": "1", "price": "0.5", "timestamp": 500, "transactionHash": "h1"},
            {"proxyWallet": "0xw", "side": "SELL", "asset": "a", "conditionId": "0x1",
             "size": "1", "price": "0.5", "timestamp": 450, "transactionHash": "h2"},
        ],
        2: [
            # overlap: h2 repeats (delayed indexing); ts=350 new; ts=250 below cutoff
            {"proxyWallet": "0xw", "side": "SELL", "asset": "a", "conditionId": "0x1",
             "size": "1", "price": "0.5", "timestamp": 450, "transactionHash": "h2"},
            {"proxyWallet": "0xw", "side": "BUY", "asset": "a", "conditionId": "0x1",
             "size": "1", "price": "0.5", "timestamp": 250, "transactionHash": "h4"},
        ],
    }
    page1 = [
        {"proxyWallet": "0xw", "side": "BUY", "asset": "a", "conditionId": "0x1",
         "size": "1", "price": "0.5", "timestamp": 400, "transactionHash": "h3"},
        {"proxyWallet": "0xw", "side": "SELL", "asset": "a", "conditionId": "0x1",
         "size": "1", "price": "0.5", "timestamp": 450, "transactionHash": "h2"},  # overlap dup (identical fields)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json=pages[0])
        if offset == 2:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = AllowlistClient([HOST_DATA], transport=transport, rate_per_second=1000)
    try:
        adapter = DataApiAdapter(client, "https://data-api.polymarket.com")
        trades = await adapter.iter_user_trades(
            "0xw", stop_predicate=lambda t: t.timestamp < 300, page_size=2
        )
    finally:
        await client.aclose()

    hashes = [t.transaction_hash for t in trades]
    # h1, h2, h3 collected; h2 not duplicated; h4 (ts 250) excluded by cutoff
    assert hashes.count("h2") == 1
    assert "h4" not in hashes
    assert set(hashes) == {"h1", "h2", "h3"}


@pytest.mark.asyncio
async def test_iter_user_trades_stops_at_offset_cap(fixtures):
    """The public /trades endpoint 400s past offset ~3000; the adapter must
    treat a 400 mid-pagination as the endpoint cap (PRD 8.3), not an error."""
    page = fixtures("wallet_trades.json")

    def handler(request):
        offset = int(request.url.params.get("offset", "0"))
        if offset > 0:
            return httpx.Response(400, text="Bad Request")
        return httpx.Response(200, json=page)

    client = AllowlistClient(
        frozenset({"data-api.polymarket.com"}),
        transport=httpx.MockTransport(handler),
    )
    adapter = DataApiAdapter(client, "https://data-api.polymarket.com")
    trades = await adapter.iter_user_trades("0xabc", page_size=len(page))
    assert len(trades) > 0  # first page collected, cap swallowed


@pytest.mark.asyncio
async def test_iter_user_trades_first_page_400_still_raises(fixtures):
    client = AllowlistClient(
        frozenset({"data-api.polymarket.com"}),
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="Bad Request")),
    )
    adapter = DataApiAdapter(client, "https://data-api.polymarket.com")
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.iter_user_trades("0xabc")
