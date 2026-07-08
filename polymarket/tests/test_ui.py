"""Wave-4 dashboard UI smoke tests (PRD sec 20): each page renders 200 + markers."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from .. import db as dbmod
from ..api import create_app
from ..config import load_config
from ._seed import seed


async def _client_conn(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    await seed(conn)
    return conn, create_app(conn, config)


PAGES = [
    ("/polymarket", "Paper trading only"),
    ("/polymarket", "Paper trading snapshot"),
    ("/polymarket/wallets", "0xabc"),
    ("/polymarket/wallets/0xabc", "Category performance"),
    ("/polymarket/signals", "Trade signals"),
    ("/polymarket/paper-trades", "Paper trades"),
    ("/polymarket/journal", "Decision journal"),
    ("/polymarket/performance", "Hermes strategy vs blind copy"),
    ("/polymarket/rules", "active v1"),
    ("/polymarket/reports", "2026-07-06"),
    ("/polymarket/health", "Data freshness"),
]


@pytest.mark.parametrize("path,marker", PAGES)
async def test_page_renders(tmp_path, path, marker):
    conn, app = await _client_conn(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text[:400])
            assert resp.headers["content-type"].startswith("text/html")
            assert "paper trading only" in resp.text.lower()  # global badge on every page
            assert marker.lower() in resp.text.lower(), path
    finally:
        await conn.close()


async def test_active_rule_version_in_header(tmp_path):
    conn, app = await _client_conn(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.get("/polymarket")
            assert "Active rule" in resp.text
            assert "v1" in resp.text
    finally:
        await conn.close()


async def test_equity_charts_use_chartjs_canvas(tmp_path):
    conn, app = await _client_conn(tmp_path)
    try:
        cur = await conn.execute("SELECT id FROM paper_portfolios LIMIT 1")
        pid = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO pnl_snapshots (paper_portfolio_id,cash_balance,open_cost,unrealized_pnl,"
            "realized_pnl,equity,drawdown,collected_at) VALUES "
            "(?,990000000,0,0,4000000,1004000000,0,'2026-07-07T00:00:00.000000Z')",
            (pid,),
        )
        await conn.commit()
        with TestClient(app) as client:
            overview = client.get("/polymarket")
            performance = client.get("/polymarket/performance")

        assert "chart.js@4.4.3" in overview.text
        assert 'id="overview-equity-chart"' in overview.text
        assert 'id="performance-equity-chart"' in performance.text
        assert "data-hermes-chart" in overview.text
        assert "data-hermes-chart" in performance.text
        assert "overviewEquityFill" not in overview.text
        assert "performanceEquityFill" not in performance.text
        assert "chart-hit" not in overview.text
    finally:
        await conn.close()


async def test_wallet_not_found_404(tmp_path):
    conn, app = await _client_conn(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.get("/polymarket/wallets/0xdeadbeef")
            assert resp.status_code == 404
            assert "Not found" in resp.text
    finally:
        await conn.close()


async def test_autoescape_on(tmp_path):
    """External strings must be HTML-escaped (Jinja2 autoescape)."""
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    from ..jobs.portfolio_view import ensure_portfolio

    await ensure_portfolio(conn, starting_bankroll=dbmod.micro_to_usd(1_000_000_000))
    await conn.execute(
        "INSERT INTO wallet_profiles (wallet_address,status,global_score,resolved_trade_count,"
        "trade_count,profile_version,is_demo,history_complete,raw_json) VALUES "
        "('0xxss','track',80,10,10,1,0,1,'{\"user_name\":\"<script>evil</script>\"}')",
    )
    await conn.commit()
    app = create_app(conn, config)
    try:
        with TestClient(app) as client:
            resp = client.get("/polymarket/wallets")
            assert "<script>evil</script>" not in resp.text
            assert "&lt;script&gt;" in resp.text
    finally:
        await conn.close()
