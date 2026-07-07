"""Resolution/category/prune sweep — the fix for the production pipeline
dead-end where gamma's closed-market filter left every market 'open' forever."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from .. import db as dbmod
from ..adapters.gamma import GammaAdapter
from ..config import load_config
from ..http import AllowlistClient
from ..jobs.resolve_markets import normalize_category, run_resolve_markets
from ..jobs.runner import JobContext, run_job

COND_OPEN = "0x" + "a" * 64
COND_CLOSED = "0x" + "b" * 64


def _gamma_market(cond: str, *, closed: bool, event_id: str = "777") -> dict:
    return {
        "id": f"m-{cond[:6]}",
        "conditionId": cond,
        "question": "Will it settle?",
        "slug": f"slug-{cond[:6]}",
        "endDate": "2020-01-01T00:00:00Z",
        "closed": closed,
        "umaResolutionStatus": "resolved" if closed else "unresolved",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["1", "0"]) if closed else json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps(["11", "22"]),
        "events": [{"id": event_id, "title": "E", "slug": "e"}],
    }


def _transport(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/events" in request.url.path:
            return httpx.Response(200, json=[
                {"id": "777", "tags": [{"label": "Sports"}, {"label": "Soccer"}]},
            ])
        closed = request.url.params.get("closed")
        conds = request.url.params.get_list("condition_ids")
        rows = []
        for cond in conds:
            if cond == COND_CLOSED and closed == "true":
                rows.append(_gamma_market(cond, closed=True))
            if cond == COND_OPEN and closed == "false":
                rows.append(_gamma_market(cond, closed=False))
        return httpx.Response(200, json=rows)

    return httpx.MockTransport(handler)


def test_normalize_category():
    assert normalize_category(["Sports", "Soccer"]) == ("SPORTS", "Sports")
    assert normalize_category(["Obscure Label"]) == ("OTHER", "Obscure Label")
    assert normalize_category([]) == ("", "")


@pytest.mark.asyncio
async def test_sweep_backfills_resolves_categorizes_prunes(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})
    conn = await dbmod.init_db(str(tmp_path / "t.db"), config.migrations_dir)
    try:
        # A trade on a market gamma only returns with closed=true (the bug).
        await conn.execute(
            "INSERT INTO wallet_trades (proxy_wallet, condition_id, side, price_micro, size, ts, ingested_at) "
            "VALUES ('0xw', ?, 'BUY', 500000, 1000000, ?, '2026-01-01T00:00:00Z')",
            (COND_CLOSED, int(time.time()) - 3600),  # recent: survives pruning
        )
        # A stale trade past lookback+grace: must be pruned.
        await conn.execute(
            "INSERT INTO wallet_trades (proxy_wallet, condition_id, side, price_micro, size, ts, ingested_at) "
            "VALUES ('0xw', ?, 'BUY', 500000, 1000000, 1, '2026-01-01T00:00:00Z')",
            (COND_OPEN,),
        )
        # An already-known open market whose end date passed: must resolve.
        await conn.execute(
            "INSERT INTO markets (market_id, condition_id, question, status, scheduled_resolution_at, event_id, metadata_updated_at) "
            "VALUES ('m-known', ?, 'q', 'open', '2020-01-01T00:00:00Z', '777', '2026-01-01T00:00:00Z')",
            (COND_OPEN,),
        )
        await conn.commit()

        seen: list = []
        client = AllowlistClient(
            frozenset({"gamma-api.polymarket.com"}), transport=_transport(seen)
        )
        gamma = GammaAdapter(client, "https://gamma-api.polymarket.com")
        await run_job(conn, "resolve_markets",
                      lambda ctx: run_resolve_markets(ctx, config, gamma))

        # Backfill: the closed market exists now, resolved with a winner.
        cur = await conn.execute(
            "SELECT status, winning_outcome, category FROM markets WHERE condition_id = ?",
            (COND_CLOSED,),
        )
        status, winner, category = await cur.fetchone()
        assert status == "resolved"
        assert winner == "Yes"
        assert category == "SPORTS"

        # Both closed states were queried (the actual bug regression check).
        assert any("closed=true" in u for u in seen)
        assert any("closed=false" in u for u in seen)

        # Prune: the ancient trade is gone, the recent one stays.
        cur = await conn.execute("SELECT COUNT(*) FROM wallet_trades")
        assert (await cur.fetchone())[0] == 1

        # Category propagated onto the surviving history row.
        cur = await conn.execute(
            "SELECT category FROM wallet_trades WHERE condition_id = ?", (COND_CLOSED,)
        )
        assert (await cur.fetchone())[0] == "SPORTS"
    finally:
        await conn.close()
