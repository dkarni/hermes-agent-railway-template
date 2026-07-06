"""Wave-4 API route tests (PRD sec 19): shape, money-as-string, CSV, 409, actions."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from .. import db as dbmod
from ..api import create_app
from ..config import load_config
from ._seed import seed


async def _app(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    await seed(conn)
    return conn, create_app(conn, config)


READ_ROUTES = [
    "/api/polymarket/overview",
    "/api/polymarket/wallets",
    "/api/polymarket/wallets/0xabc",
    "/api/polymarket/wallets/0xabc/trades",
    "/api/polymarket/signals",
    "/api/polymarket/paper-trades",
    "/api/polymarket/journal",
    "/api/polymarket/performance",
    "/api/polymarket/performance/benchmarks",
    "/api/polymarket/rules",
    "/api/polymarket/rules/1",
    "/api/polymarket/reports",
    "/api/polymarket/reports/2026-07-06",
    "/api/polymarket/health",
    "/api/polymarket/job-runs",
]


@pytest.mark.parametrize("path", READ_ROUTES)
async def test_read_route_200(tmp_path, path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text[:400])
            assert resp.headers["content-type"].startswith("application/json")
    finally:
        await conn.close()


async def test_signal_detail_and_paper_detail(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            sig = client.get("/api/polymarket/signals").json()["items"][0]
            resp = client.get(f"/api/polymarket/signals/{sig['id']}")
            assert resp.status_code == 200
            assert "reasons" in resp.json()  # full explanation JSON
            pt = client.get("/api/polymarket/paper-trades").json()["items"][0]
            detail = client.get(f"/api/polymarket/paper-trades/{pt['id']}").json()
            assert "ledger" in detail and "hourly_marks" in detail
    finally:
        await conn.close()


async def test_money_is_string(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            ov = client.get("/api/polymarket/overview").json()
            assert isinstance(ov["equity_usd"], str)
            assert isinstance(ov["comparison"]["filtered"]["net_pnl_usd"], str)
            pt = client.get("/api/polymarket/paper-trades").json()["items"][0]
            assert isinstance(pt["realized_pnl_usd"], str)
            assert isinstance(pt["entry_price"], str)
            assert pt["pnl_kind"] == "paper"
    finally:
        await conn.close()


async def test_wallets_filters_and_pagination(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            body = client.get("/api/polymarket/wallets?status=track&min_score=50&limit=10").json()
            assert body["total"] >= 1
            assert body["limit"] == 10 and body["offset"] == 0
            row = body["items"][0]
            assert "copied_paper_pnl_usd" in row and "score_components" in row
            # copyable_only + exclude_stale
            body2 = client.get("/api/polymarket/wallets?copyable_only=1&exclude_stale=1").json()
            assert body2["total"] >= 1
    finally:
        await conn.close()


async def test_csv_export(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            for path in ("/api/polymarket/wallets", "/api/polymarket/journal",
                         "/api/polymarket/paper-trades"):
                resp = client.get(f"{path}?format=csv")
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/csv")
                assert "attachment; filename=" in resp.headers.get("content-disposition", "")
    finally:
        await conn.close()


async def test_action_returns_job_run_id(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.post("/api/polymarket/actions/generate-report")
            assert resp.status_code == 202
            assert isinstance(resp.json()["job_run_id"], int)
    finally:
        await conn.close()


async def test_action_double_run_409(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        # Insert a running job for 'pnl' so update-pnl is blocked.
        await conn.execute(
            "INSERT INTO job_runs (job_name,trigger_type,started_at,status,lock_key) "
            "VALUES ('pnl','manual',?, 'running','pnl')",
            (dbmod.utcnow_iso(),),
        )
        await conn.commit()
        with TestClient(app) as client:
            resp = client.post("/api/polymarket/actions/update-pnl")
            assert resp.status_code == 409
            assert "job_run_id" in resp.json()
    finally:
        await conn.close()


async def test_unknown_action_404(tmp_path):
    conn, app = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            # rollback of a nonexistent version returns not_found (200 with status).
            resp = client.get("/api/polymarket/rules/999")
            assert resp.status_code == 404
    finally:
        await conn.close()
