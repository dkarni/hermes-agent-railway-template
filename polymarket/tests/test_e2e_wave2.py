"""End-to-end Wave 2 fixture test (no network).

Proves: leaderboard scan -> ingest history -> profile+score to `track` ->
monitor pass detects one new trade and writes one full decision journal entry ->
re-run monitor is idempotent (no duplicate observed trade / journal row).

All HTTP is served by a synthetic httpx.MockTransport; the data is coherent so
one wallet reaches `track` deterministically.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from .. import db as dbmod
from ..adapters.clob import ClobAdapter
from ..adapters.dataapi import DataApiAdapter
from ..adapters.gamma import GammaAdapter
from ..config import load_config
from ..http import AllowlistClient
from ..jobs.ingest_history import run_ingest_history
from ..jobs.leaderboard_scan import run_leaderboard_scan
from ..jobs.monitor import run_monitor
from ..jobs.profile_wallets import run_profile_wallets
from ..jobs.runner import run_job

WALLET = "0x1111111111111111111111111111111111111111"
NOW = int(time.time())
DAY = 86400


def _cond(i: int) -> str:
    return "0x" + f"{i:064x}"


def _leaderboard_page(offset: int):
    if offset > 0:
        return []
    return [
        {
            "rank": "1",
            "proxyWallet": WALLET,
            "userName": "sharp",
            "xUsername": "",
            "verifiedBadge": False,
            "vol": 100000.0,
            "pnl": 5000.0,
            "profileImage": "",
        }
    ]


def _history_trades():
    """15 resolved winning buys + 1 fresh open buy (the monitor signal)."""
    trades = []
    for i in range(15):
        trades.append({
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": f"asset{i}",
            "conditionId": _cond(i),
            "size": 100.0,
            "price": 0.5,
            "timestamp": NOW - (20 - i) * DAY,
            "title": f"Market {i}",
            "slug": f"m{i}",
            "eventSlug": f"e{i}",
            "outcome": "Yes",
            "outcomeIndex": 0,
            "transactionHash": f"0xhist{i}",
        })
    return trades


FRESH_COND = _cond(999)
FRESH_ASSET = "asset999"


def _fresh_trade():
    return {
        "proxyWallet": WALLET,
        "side": "BUY",
        "asset": FRESH_ASSET,
        "conditionId": FRESH_COND,
        "size": 100.0,
        "price": 0.50,
        "timestamp": NOW - 30,
        "title": "Fresh Market",
        "slug": "fresh",
        "eventSlug": "fresh-event",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "transactionHash": "0xfresh1",
    }


def _market_row(condition_id: str, *, resolved: bool, winner: str | None):
    prices = ("1", "0") if (resolved and winner == "Yes") else (("0", "1") if resolved else ("0.5", "0.5"))
    end = NOW + 7 * DAY
    from datetime import datetime, timezone
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": condition_id,
        "conditionId": condition_id,
        "question": "Will it?",
        "slug": "q",
        "endDate": end_iso,
        "liquidity": "10000",
        "category": "CRYPTO",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(list(prices)),
        "clobTokenIds": json.dumps([f"tok-{condition_id}-yes", f"tok-{condition_id}-no"]),
        "closed": resolved,
        "resolved": resolved,
        "events": [{"id": "ev1", "title": "Event", "slug": "event"}],
    }


def _book(asset_id: str):
    return {
        "market": "m",
        "asset_id": asset_id,
        "timestamp": str(NOW * 1000),  # epoch millis, fresh
        "bids": [{"price": "0.49", "size": "500"}, {"price": "0.48", "size": "500"}],
        "asks": [{"price": "0.52", "size": "500"}, {"price": "0.51", "size": "500"}],
    }


class _State:
    def __init__(self):
        self.fresh_visible = False  # the monitor trade appears only after this flips


def _make_transport(state: _State):
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        if "leaderboard" in path:
            offset = int(url.params.get("offset", 0))
            return httpx.Response(200, json=_leaderboard_page(offset))
        if path.endswith("/trades"):
            offset = int(url.params.get("offset", 0))
            if offset > 0:
                return httpx.Response(200, json=[])
            trades = list(_history_trades())
            if state.fresh_visible:
                trades = [_fresh_trade()] + trades
            return httpx.Response(200, json=trades)
        if path.endswith("/markets"):
            cids = url.params.get_list("condition_ids")
            rows = []
            for cid in cids:
                if cid == FRESH_COND:
                    rows.append(_market_row(cid, resolved=False, winner=None))
                else:
                    rows.append(_market_row(cid, resolved=True, winner="Yes"))
            return httpx.Response(200, json=rows)
        if path.endswith("/book"):
            token = url.params.get("token_id", "")
            return httpx.Response(200, json=_book(token))
        if path.endswith("/midpoint"):
            return httpx.Response(200, json={"mid": "0.5"})
        return httpx.Response(404, json={"error": "unmocked", "path": path})

    return httpx.MockTransport(handler)


def _migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


@pytest.mark.asyncio
async def test_e2e_leaderboard_to_decision(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper",
                          "LEADERBOARD_WALLET_LIMIT": "1"})  # mock serves 1 wallet/scan: complete, not partial
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
    state = _State()
    client = AllowlistClient(config.allowed_hosts(), transport=_make_transport(state))
    dataapi = DataApiAdapter(client, config.data_base_url)
    gamma = GammaAdapter(client, config.gamma_base_url)
    clob = ClobAdapter(client, config.clob_base_url)

    try:
        # 1. leaderboard scan -> wallet universe
        await run_job(conn, "leaderboard_scan",
                      lambda ctx: run_leaderboard_scan(ctx, config, dataapi))
        cur = await conn.execute("SELECT status FROM wallet_profiles WHERE wallet_address = ?", (WALLET,))
        assert (await cur.fetchone())[0] == "insufficient_data"

        # 2. ingest history
        await run_job(conn, "ingest_history",
                      lambda ctx: run_ingest_history(ctx, config, dataapi, gamma, concurrency=2))
        cur = await conn.execute("SELECT COUNT(*) FROM wallet_trades WHERE proxy_wallet = ?", (WALLET,))
        assert (await cur.fetchone())[0] == 15
        cur = await conn.execute("SELECT history_complete FROM wallet_profiles WHERE wallet_address = ?", (WALLET,))
        assert (await cur.fetchone())[0] == 1

        # 3. profile + score -> track
        await run_job(conn, "profile_wallets",
                      lambda ctx: run_profile_wallets(ctx, config))
        cur = await conn.execute(
            "SELECT status, global_score FROM wallet_profiles WHERE wallet_address = ?", (WALLET,)
        )
        status, score = await cur.fetchone()
        assert status == "track", f"expected track, got {status} (score {score})"

        # 4. monitor pass with a fresh trade appearing
        state.fresh_visible = True
        await run_job(conn, "monitor",
                      lambda ctx: run_monitor(ctx, config, dataapi, gamma, clob))

        cur = await conn.execute(
            "SELECT COUNT(*) FROM observed_trades WHERE wallet_address = ?", (WALLET,)
        )
        assert (await cur.fetchone())[0] == 1

        cur = await conn.execute(
            "SELECT decision, component_scores_json, hard_gates_json, total_score "
            "FROM decision_journal WHERE wallet_address = ?", (WALLET,)
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        decision, comp_json, gates_json, total = rows[0]
        assert decision in ("paper_copy", "watchlist", "skip")
        comps = json.loads(comp_json)
        assert len(comps) == 8
        gates = json.loads(gates_json)
        assert len(gates) == 9
        assert total is not None

        # 5. re-run monitor -> idempotent, no duplicates
        await run_job(conn, "monitor",
                      lambda ctx: run_monitor(ctx, config, dataapi, gamma, clob))
        cur = await conn.execute(
            "SELECT COUNT(*) FROM observed_trades WHERE wallet_address = ?", (WALLET,)
        )
        assert (await cur.fetchone())[0] == 1
        cur = await conn.execute(
            "SELECT COUNT(*) FROM decision_journal WHERE wallet_address = ?", (WALLET,)
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await client.aclose()
        await conn.close()
