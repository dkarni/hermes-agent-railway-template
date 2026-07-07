from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from .. import db as dbmod
from ..adapters.models import FillEstimate, OrderBook, OrderBookLevel, WalletTrade
from ..config import load_config
from ..db import initial_rule_set_payload
from ..domain import trade_scoring as ts
from ..jobs.monitor import _build_inputs, _load_market


@pytest.mark.asyncio
async def test_stored_market_uses_db_category_over_blank_raw_json(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    wallet = "0x" + "1" * 40
    condition_id = "0x" + "2" * 64
    asset_id = "asset-yes"
    now_ts = int(time.time())
    end_iso = datetime.fromtimestamp(now_ts + 7 * 86400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    raw_market = {
        "id": "m1",
        "conditionId": condition_id,
        "question": "Will BTC close above 100k?",
        "slug": "btc-100k",
        "endDate": end_iso,
        "category": "",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([asset_id, "asset-no"]),
        "closed": False,
        "resolved": False,
        "events": [{"id": "ev1", "title": "BTC", "slug": "btc"}],
    }
    try:
        await conn.execute(
            "INSERT INTO markets (market_id,condition_id,category,status,metadata_updated_at,raw_json) "
            "VALUES ('m1',?,'Crypto','open',?,?)",
            (condition_id, dbmod.utcnow_iso(), json.dumps(raw_market)),
        )
        await conn.execute(
            "INSERT INTO wallet_profiles (wallet_address,status,global_score,data_quality_score,"
            "calculated_at,history_complete) VALUES (?,?,?,?,?,1)",
            (wallet, "track", 90, 90, dbmod.utcnow_iso()),
        )
        await conn.execute(
            "INSERT INTO wallet_category_stats (wallet_address,category,category_score,calculated_at) "
            "VALUES (?,?,?,?)",
            (wallet, "CRYPTO", 95, dbmod.utcnow_iso()),
        )
        await conn.commit()

        market = await _load_market(conn, condition_id)
        assert market is not None
        assert market.category == "CRYPTO"

        trade = WalletTrade(
            wallet_address=wallet,
            side="BUY",
            asset_id=asset_id,
            condition_id=condition_id,
            size=Decimal("100"),
            price=Decimal("0.50"),
            timestamp=now_ts - 10,
            title="T",
            slug="s",
            event_slug="e",
            outcome="Yes",
            outcome_index=0,
            transaction_hash="0xt",
            raw={},
        )
        book = OrderBook(
            asset_id=asset_id,
            market="m1",
            timestamp=str(now_ts * 1000),
            bids=(OrderBookLevel(Decimal("0.49"), Decimal("1000")),),
            asks=(OrderBookLevel(Decimal("0.50"), Decimal("1000")),),
            raw={},
        )
        fill = FillEstimate(
            filled_shares=Decimal("40"),
            filled_usd=Decimal("20"),
            avg_price=Decimal("0.50"),
            fully_filled=True,
            stopped_reason="complete",
        )

        inputs = await _build_inputs(
            conn, initial_rule_set_payload(), wallet, trade, market, book, False, fill
        )
        score = ts.score_trade(inputs)
        wallet_category = next(g for g in score.hard_gates if g.name == "wallet_category")
        assert inputs.observed.category == "CRYPTO"
        assert inputs.category_stat_score == Decimal(95)
        assert wallet_category.passed
    finally:
        await conn.close()
