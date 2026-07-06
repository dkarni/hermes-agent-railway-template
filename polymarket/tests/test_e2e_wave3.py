"""End-to-end Wave 3 lifecycle (no network; httpx.MockTransport).

Proves the full paper + learning loop:
  tracked-wallet trade -> paper_copy decision -> paper trade opened with the
  correct fill + ledger -> run_pnl writes a snapshot -> market resolves ->
  settlement + final review label + benchmark resolution -> compose_daily
  reflects it -> seed >=20 judged reviews -> a bounded rule change activates ->
  simulate a bad window -> rollback restores the parent payload.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from .. import db as dbmod
from ..adapters.clob import ClobAdapter
from ..adapters.dataapi import DataApiAdapter
from ..adapters.gamma import GammaAdapter
from ..config import load_config
from ..http import AllowlistClient
from ..jobs.monitor import run_monitor
from ..jobs.paper_exec import paper_copy_callback
from ..jobs.pnl import run_pnl
from ..jobs.portfolio_view import ensure_portfolio, load_portfolio_view
from ..jobs.reports import compose_daily
from ..jobs.reviews import run_reviews
from ..jobs import rule_eval as rule_eval_mod
from ..jobs.runner import run_job

NOW = int(time.time())
DAY = 86400
WALLET = "0x" + "1" * 40
COND = "0x" + f"{7:064x}"
ASSET = "asset7"


class _State:
    resolved = False


def _mkt(cid, state):
    end = NOW + 7 * DAY
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prices = ["1", "0"] if state.resolved else ["0.5", "0.5"]
    return {
        "id": cid, "conditionId": cid, "question": "Will BTC 100k?", "slug": "q",
        "endDate": end_iso, "liquidity": "100000", "category": "CRYPTO",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(prices),
        "clobTokenIds": json.dumps([ASSET, "n"]),
        "closed": state.resolved, "resolved": state.resolved,
        "events": [{"id": "ev1", "title": "E", "slug": "e"}],
    }


def _book(a):
    return {
        "market": "m", "asset_id": a, "timestamp": str(NOW * 1000),
        "bids": [{"price": "0.50", "size": "100000"}],
        "asks": [{"price": "0.51", "size": "100000"}],
    }


def _make_transport(state):
    def handler(req):
        p = req.url.path
        if p.endswith("/trades"):
            if int(req.url.params.get("offset", 0)) > 0:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{
                "proxyWallet": WALLET, "side": "BUY", "asset": ASSET, "conditionId": COND,
                "size": 100.0, "price": 0.50, "timestamp": NOW - 20, "title": "T",
                "slug": "s", "eventSlug": "es", "outcome": "Yes", "outcomeIndex": 0,
                "transactionHash": "0xf1",
            }])
        if p.endswith("/markets"):
            cids = req.url.params.get_list("condition_ids")
            return httpx.Response(200, json=[_mkt(c, state) for c in cids])
        if p.endswith("/book"):
            return httpx.Response(200, json=_book(req.url.params.get("token_id", "")))
        if p.endswith("/midpoint"):
            return httpx.Response(200, json={"mid": "0.505"})
        return httpx.Response(404, json={"path": p})
    return httpx.MockTransport(handler)


async def _seed_track_wallet(conn):
    now = dbmod.utcnow_iso()
    await conn.execute(
        "INSERT INTO wallet_profiles (wallet_address,status,global_score,data_quality_score,"
        "resolved_trade_count,trade_count,history_complete,calculated_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (WALLET, "track", 95, 95, 30, 40, now),
    )
    await conn.execute(
        "INSERT INTO wallet_category_stats (wallet_address,category,category_score,"
        "resolved_trade_count,calculated_at) VALUES (?,?,?,?,?)",
        (WALLET, "CRYPTO", 95, 30, now),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_e2e_wave3_full_lifecycle(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper",
                          "RULE_UPDATE_MIN_DAYS": "0"})  # e2e portfolio starts "today"; burn-in gate has its own test
    conn = await dbmod.init_db(str(tmp_path / "poly.db"), config.migrations_dir)
    await ensure_portfolio(conn, starting_bankroll=Decimal("1000"))
    await _seed_track_wallet(conn)
    state = _State()
    allow = AllowlistClient(config.allowed_hosts(), transport=_make_transport(state))
    dataapi = DataApiAdapter(allow, config.data_base_url)
    gamma = GammaAdapter(allow, config.gamma_base_url)
    clob = ClobAdapter(allow, config.clob_base_url)

    try:
        # --- monitor: detect trade, decide paper_copy, open a paper trade ---
        async def _monitor(ctx):
            pv = await load_portfolio_view(ctx.conn)
            return await run_monitor(
                ctx, config, dataapi, gamma, clob,
                portfolio=pv, on_paper_copy=paper_copy_callback,
            )
        await run_job(conn, "monitor", _monitor)

        cur = await conn.execute("SELECT decision, id, market_id FROM decision_journal")
        drow = await cur.fetchone()
        assert drow[0] == "paper_copy"
        dj_id = int(drow[1])

        cur = await conn.execute(
            "SELECT id, status, entry_cost, shares, entry_price FROM paper_trades"
        )
        t = await cur.fetchone()
        trade_id = int(t[0])
        assert t[1] == "open"
        assert dbmod.micro_to_usd(int(t[2])) == Decimal("10")   # tier $10
        assert dbmod.micro_to_px(int(t[4])) == Decimal("0.51")  # fill at best ask

        # ledger entry recorded, cash decremented to 990.
        cur = await conn.execute(
            "SELECT entry_type, balance_after FROM paper_ledger WHERE paper_trade_id=?", (trade_id,)
        )
        led = await cur.fetchall()
        assert [r[0] for r in led] == ["entry"]
        assert dbmod.micro_to_usd(int(led[0][1])) == Decimal("990")

        # blind benchmark recorded from the same book (idempotent).
        cur = await conn.execute("SELECT COUNT(*) FROM benchmark_trades WHERE cohort='blind'")
        assert int((await cur.fetchone())[0]) == 1

        # --- pnl snapshot: mark at best bid 0.50 ---
        await run_job(conn, "pnl", lambda ctx: run_pnl(ctx, clob))
        cur = await conn.execute(
            "SELECT equity, unrealized_pnl FROM pnl_snapshots ORDER BY id DESC LIMIT 1"
        )
        snap = await cur.fetchone()
        assert snap is not None
        # 19.607843 shares * 0.50 = 9.80 mark vs 10 cost -> unrealized < 0.
        assert dbmod.micro_to_usd(int(snap[1])) < 0

        # --- resolve the market (Yes wins) ---
        state.resolved = True
        from ..jobs.reconcile import run_reconcile

        async def _reconcile(ctx):
            pv = await load_portfolio_view(ctx.conn)
            return await run_reconcile(ctx, config, dataapi, gamma, clob, portfolio=pv)
        await run_job(conn, "reconcile", _reconcile)

        cur = await conn.execute("SELECT status, realized_pnl FROM paper_trades WHERE id=?", (trade_id,))
        strow = await cur.fetchone()
        assert strow[0] == "resolved"
        # Yes won: 19.607843 shares settle at 1 -> ~9.607843 realized.
        assert dbmod.micro_to_usd(int(strow[1])) > Decimal("9")

        # benchmark resolved too.
        cur = await conn.execute("SELECT final_pnl FROM benchmark_trades WHERE cohort='blind'")
        assert (await cur.fetchone())[0] is not None

        # --- final review labels the decision ---
        await run_job(conn, "reviews", lambda ctx: run_reviews(ctx, clob))
        cur = await conn.execute(
            "SELECT decision_quality_label, eligible_for_learning FROM outcome_reviews "
            "WHERE decision_journal_id=? AND review_checkpoint='final'", (dj_id,)
        )
        rev = await cur.fetchone()
        assert rev[0] == "good_copy"
        assert rev[1] == 1

        # --- compose_daily reflects the lifecycle ---
        day = dbmod.utcnow_iso()[:10]
        report = await compose_daily(conn, config, day)
        assert report["copies"] == 1
        assert report["total_pnl"] > Decimal("9")
        assert report["win_sample"] == 1

        # --- rule change: seed >=20 judged with high missed_winner -> loosen ---
        rule_set_id_before = await _active_id(conn)
        await _seed_judged_reviews(conn, missed=8, good_skip=14)
        result = await _run_return(conn, "rule_eval", lambda ctx: rule_eval_mod.run_rule_eval(ctx, config))
        assert result["status"] == "changed", result
        assert result["family"] == "binding_gate"
        new_id = result["rule_set_id"]

        cur = await conn.execute("SELECT id, status FROM rule_sets ORDER BY id")
        rs = {int(r[0]): r[1] for r in await cur.fetchall()}
        assert rs[rule_set_id_before] == "superseded"
        assert rs[new_id] == "active"
        cur = await conn.execute("SELECT parameters_json FROM rule_sets WHERE id=?", (new_id,))
        new_payload = json.loads((await cur.fetchone())[0])
        assert new_payload["decision_thresholds"]["paper_copy_min_score"] < 75
        cur = await conn.execute(
            "SELECT outcome_status, rollback_rule_json FROM rule_changes WHERE rule_set_id=?", (new_id,)
        )
        rc = await cur.fetchone()
        assert rc[0] == "pending"
        assert json.loads(rc[1])["metric"] == "missed_winner_rate"

        # --- bad window: rollback restores the parent payload ---
        await _seed_judged_reviews(conn, missed=18, good_skip=4)
        rb = await _run_return(conn, "rule_eval", lambda ctx: rule_eval_mod.run_rule_eval(ctx, config))
        assert rb["status"] == "rolled_back", rb
        restored_id = rb["restored_rule_set_id"]

        cur = await conn.execute("SELECT parameters_json, status FROM rule_sets WHERE id=?", (restored_id,))
        rrow = await cur.fetchone()
        restored_payload = json.loads(rrow[0])
        assert rrow[1] == "active"
        assert restored_payload["decision_thresholds"]["paper_copy_min_score"] == 75
        cur = await conn.execute("SELECT status FROM rule_sets WHERE id=?", (new_id,))
        assert (await cur.fetchone())[0] == "rolled_back"
        # original v1 row never mutated (append-only).
        cur = await conn.execute(
            "SELECT parameters_json FROM rule_sets WHERE id=?", (rule_set_id_before,)
        )
        v1_payload = json.loads((await cur.fetchone())[0])
        assert v1_payload["decision_thresholds"]["paper_copy_min_score"] == 75
    finally:
        await allow.aclose()
        await conn.close()


async def _active_id(conn):
    cur = await conn.execute("SELECT id FROM rule_sets WHERE status='active'")
    return int((await cur.fetchone())[0])


async def _run_return(conn, name, coro_fn):
    """Run a job via the runner and return its metadata dict from job_runs."""
    run_id = await run_job(conn, name, coro_fn)
    cur = await conn.execute("SELECT metadata_json, error_json FROM job_runs WHERE id=?", (run_id,))
    row = await cur.fetchone()
    assert row[1] is None, row[1]  # no job error
    return json.loads(row[0]) if row and row[0] else {}


_SEED_N = 0


async def _seed_judged_reviews(conn, *, missed: int, good_skip: int):
    """Seed final, eligible reviews on skip decisions for a fresh evidence window.

    ``missed`` -> missed_winner labels, ``good_skip`` -> good_skip labels. Each
    review hangs off its own skip decision + observed trade so joins stay clean.
    Created 'now' so they land after the last rule change (a fresh window).
    """
    global _SEED_N
    now = dbmod.utcnow_iso()
    for label, count in (("missed_winner", missed), ("good_skip", good_skip)):
        for _ in range(count):
            _SEED_N += 1
            key = f"seed-{_SEED_N}"
            oc = await conn.execute(
                "INSERT INTO observed_trades (source, wallet_address, source_side, idempotency_key, detected_at) "
                "VALUES ('data','0xw','BUY',?,?)", (key, now),
            )
            obs = int(oc.lastrowid)
            dc = await conn.execute(
                "INSERT INTO decision_journal (strategy, observed_trade_id, wallet_address, decision, created_at) "
                "VALUES ('default',?,'0xw','skip',?)", (obs, now),
            )
            dj = int(dc.lastrowid)
            await conn.execute(
                "INSERT INTO outcome_reviews (decision_journal_id, review_checkpoint, "
                "hypothetical_pnl, decision_quality_label, eligible_for_learning, created_at) "
                "VALUES (?, 'final', ?, ?, 1, ?)",
                (dj, dbmod.usd_to_micro(Decimal("1")), label, now),
            )
    await conn.commit()
