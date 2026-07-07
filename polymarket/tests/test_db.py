from __future__ import annotations

import json
from decimal import Decimal

import pytest

from .. import db as dbmod

EXPECTED_TABLES = {
    "leaderboard_scans",
    "leaderboard_entries",
    "wallet_profiles",
    "wallet_category_stats",
    "observed_trades",
    "markets",
    "market_snapshots",
    "decision_journal",
    "paper_portfolios",
    "paper_trades",
    "paper_ledger",
    "pnl_snapshots",
    "outcome_reviews",
    "rule_sets",
    "rule_changes",
    "benchmark_trades",
    "daily_reports",
    "job_runs",
    "data_quality_events",
    "alerts",
}


def test_micro_roundtrip_usd():
    for value in ["0", "1000", "12.345678", "0.000001", "999999.999999"]:
        micro = dbmod.usd_to_micro(value)
        assert isinstance(micro, int)
        assert dbmod.micro_to_usd(micro) == Decimal(value).quantize(Decimal("0.000001"))


def test_micro_roundtrip_price():
    for value in ["0", "0.5", "0.515", "1"]:
        micro = dbmod.px_to_micro(value)
        assert 0 <= micro <= 1_000_000
        assert dbmod.micro_to_px(micro) == Decimal(value).quantize(Decimal("0.000001"))


def test_price_out_of_range_rejected():
    with pytest.raises(ValueError):
        dbmod.px_to_micro("1.5")
    with pytest.raises(ValueError):
        dbmod.px_to_micro("-0.01")


def test_utcnow_iso_format():
    ts = dbmod.utcnow_iso()
    assert ts.endswith("Z")
    assert "T" in ts


@pytest.mark.asyncio
async def test_migration_runner_idempotent(tmp_path):
    db_path = str(tmp_path / "poly.db")
    conn = await dbmod.connect(db_path)
    try:
        first = await dbmod.run_migrations(conn, _migrations_dir())
        assert "0001_init.sql" in first
        assert "0002_wave2.sql" in first
        assert "0003_wave3.sql" in first
        second = await dbmod.run_migrations(conn, _migrations_dir())
        assert second == []  # nothing re-applied
        applied = await dbmod.applied_migrations(conn)
        assert applied == [
            "0001_init.sql",
            "0002_wave2.sql",
            "0003_wave3.sql",
            "0004_wave3_reports.sql",
            "0005_remove_unknown_wallet_categories.sql",
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_schema_has_all_tables(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in await cursor.fetchall()}
        missing = EXPECTED_TABLES - names
        assert not missing, f"missing tables: {missing}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_observed_trades_idempotency_unique(tmp_path):
    import aiosqlite

    conn = await _fresh_db(tmp_path)
    try:
        await conn.execute(
            "INSERT INTO observed_trades (source, wallet_address, source_side, idempotency_key) "
            "VALUES ('data', '0xabc', 'BUY', 'key-1')"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO observed_trades (source, wallet_address, source_side, idempotency_key) "
                "VALUES ('data', '0xabc', 'BUY', 'key-1')"
            )
            await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_price_check_constraint(tmp_path):
    import aiosqlite

    conn = await _fresh_db(tmp_path)
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO market_snapshots (asset_id, best_ask, collected_at) "
                "VALUES ('a', 2000000, '2026-01-01T00:00:00Z')"
            )
            await conn.commit()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_rule_set_idempotent_and_complete(tmp_path):
    conn = await _fresh_db(tmp_path)
    try:
        rid1 = await dbmod.seed_rule_set(conn)
        rid2 = await dbmod.seed_rule_set(conn)
        assert rid1 == rid2
        assert await dbmod.active_rule_set_version(conn) == 1

        cursor = await conn.execute("SELECT parameters_json, checksum FROM rule_sets WHERE id = ?", (rid1,))
        row = await cursor.fetchone()
        payload = json.loads(row[0])
        # every documented section present
        for section in (
            "wallet_score_weights",
            "one_hit_wonder_penalty_bands",
            "wallet_status_thresholds",
            "freshness_gates_seconds",
            "trade_score_weights",
            "hard_gates",
            "decision_thresholds",
            "confidence_tiers",
            "exposure_limits",
            "category_shrinkage_k",
            "rule_evaluator_bounds",
        ):
            assert section in payload, section
        assert sum(payload["wallet_score_weights"].values()) == 100
        assert sum(payload["trade_score_weights"].values()) == 100
        assert payload["hard_gates"]["max_spread"] == 0.05
        assert payload["hard_gates"]["max_price_move_absolute"] == 0.05
        assert payload["hard_gates"]["min_time_to_resolution_seconds"] == 3600
        assert payload["decision_thresholds"]["paper_copy_min_score"] == 75
        assert payload["decision_thresholds"]["watchlist_min_score"] == 55
        assert payload["category_shrinkage_k"] == 10
        assert len(row[1]) == 64  # sha256 hex
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_only_one_active_rule_set(tmp_path):
    import aiosqlite

    conn = await _fresh_db(tmp_path)
    try:
        await dbmod.seed_rule_set(conn)
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO rule_sets (strategy, version, status, parameters_json, checksum, created_at) "
                "VALUES ('default', 2, 'active', '{}', 'x', '2026-01-01T00:00:00Z')"
            )
            await conn.commit()
    finally:
        await conn.close()


def _migrations_dir() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")


async def _fresh_db(tmp_path):
    return await dbmod.init_db(str(tmp_path / "poly.db"), _migrations_dir())
