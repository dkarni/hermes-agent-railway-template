from __future__ import annotations

import time

import pytest

from .. import db as dbmod
from ..config import load_config
from ..jobs.profile_wallets import run_profile_wallets
from ..jobs.runner import run_job


@pytest.mark.asyncio
async def test_profile_replaces_stale_unknown_category_stats(tmp_path):
    config = load_config({"POLY_DATA_DIR": str(tmp_path), "TRADING_MODE": "paper"})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    now = dbmod.utcnow_iso()
    ts = int(time.time()) - 3600
    try:
        await conn.execute(
            "INSERT INTO wallet_profiles (wallet_address,status,history_complete,last_profiled_at) "
            "VALUES ('0xw','track',1,NULL)",
        )
        await conn.execute(
            "INSERT INTO wallet_category_stats (wallet_address,category,category_score,calculated_at) "
            "VALUES ('0xw','UNKNOWN',99,?)",
            (now,),
        )
        await conn.execute(
            "INSERT INTO markets (market_id,condition_id,category,status,metadata_updated_at) "
            "VALUES ('m1','c1','Crypto','open',?)",
            (now,),
        )
        await conn.execute(
            "INSERT INTO wallet_trades (proxy_wallet,condition_id,side,outcome,price_micro,size,ts,category,ingested_at) "
            "VALUES ('0xw','c1','BUY','Yes',500000,1000000,?,'UNKNOWN',?)",
            (ts, now),
        )
        await conn.commit()

        await run_job(conn, "profile_wallets", lambda ctx: run_profile_wallets(ctx, config, force_all=True))

        cur = await conn.execute(
            "SELECT category FROM wallet_category_stats WHERE wallet_address='0xw' ORDER BY category"
        )
        assert [row[0] for row in await cur.fetchall()] == ["CRYPTO"]
    finally:
        await conn.close()
