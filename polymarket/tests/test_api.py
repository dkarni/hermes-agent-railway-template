from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from .. import db as dbmod
from ..api import create_app
from ..config import load_config


def _migrations_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


@pytest.mark.asyncio
async def test_health_endpoint(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    try:
        app = create_app(conn, config)
        with TestClient(app) as client:
            resp = client.get("/api/polymarket/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["trading_mode"] == "paper"
            assert body["migrations_applied"] == [
                "0001_init.sql",
                "0002_wave2.sql",
                "0003_wave3.sql",
                "0004_wave3_reports.sql",
                "0005_remove_unknown_wallet_categories.sql",
            ]
            assert body["active_rule_set_version"] == 1
            assert body["scheduler_jobs"] == []  # no scheduler passed in this test
    finally:
        await conn.close()
