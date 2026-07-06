"""SQLite access, migration runner, and money/price micro-unit helpers.

Money and prices are stored as INTEGER micro-units (x 1_000_000). All arithmetic
uses decimal.Decimal; API floats are parsed via Decimal(str(x)) at the adapter
boundary. Never use binary float for money.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

import aiosqlite

MICRO = Decimal(1_000_000)
PRICE_MIN_MICRO = 0
PRICE_MAX_MICRO = 1_000_000


# --- micro-unit helpers -----------------------------------------------------

def _to_decimal(value: Decimal | str | int) -> Decimal:
    if isinstance(value, Decimal):
        return value
    # str() guard so a stray float still round-trips through its decimal repr.
    return Decimal(str(value))


def usd_to_micro(value: Decimal | str | int) -> int:
    """USD amount -> integer micro-units, half-up rounded."""
    return int((_to_decimal(value) * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micro_to_usd(micro: int) -> Decimal:
    """Integer micro-units -> exact USD Decimal."""
    return (Decimal(int(micro)) / MICRO).quantize(Decimal("0.000001"))


def px_to_micro(value: Decimal | str | int) -> int:
    """Price in [0,1] -> integer micro-units, validated to price bounds."""
    micro = int((_to_decimal(value) * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if micro < PRICE_MIN_MICRO or micro > PRICE_MAX_MICRO:
        raise ValueError(f"price out of [0,1] range: {value!r} -> {micro} micro")
    return micro


def micro_to_px(micro: int) -> Decimal:
    """Integer micro-units -> exact price Decimal in [0,1]."""
    return (Decimal(int(micro)) / MICRO).quantize(Decimal("0.000001"))


def utcnow_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --- connection + migrations ------------------------------------------------

async def connect(db_path: str) -> aiosqlite.Connection:
    """Open a connection with WAL, foreign keys and a 5s busy timeout."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    return conn


def _migration_files(migrations_dir: str) -> list[tuple[str, str]]:
    files = sorted(glob.glob(os.path.join(migrations_dir, "*.sql")))
    out: list[tuple[str, str]] = []
    for path in files:
        name = os.path.basename(path)
        with open(path, "r", encoding="utf-8") as handle:
            out.append((name, handle.read()))
    return out


async def run_migrations(conn: aiosqlite.Connection, migrations_dir: str) -> list[str]:
    """Apply un-applied numbered migrations, each in its own transaction.

    Idempotent: already-applied filenames (tracked in schema_migrations) are
    skipped. Returns the list of filenames applied on this call.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    await conn.commit()

    cursor = await conn.execute("SELECT filename FROM schema_migrations")
    applied = {row[0] for row in await cursor.fetchall()}

    newly_applied: list[str] = []
    for name, sql in _migration_files(migrations_dir):
        if name in applied:
            continue
        try:
            await conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        except Exception:
            await conn.rollback()
            raise
        await conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
            (name, utcnow_iso()),
        )
        await conn.commit()
        newly_applied.append(name)
    return newly_applied


async def applied_migrations(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")
    return [row[0] for row in await cursor.fetchall()]


# --- rule set seed ----------------------------------------------------------

def _checksum(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def initial_rule_set_payload() -> dict:
    """The v1 strategy parameters (PRD sec 11/12/13/14/17).

    Structured by section. Money limits that mirror config env vars are stored
    here as strategy defaults so the rule evaluator can version them; the paper
    engine reads the active rule set, not env, for these. Prices are decimals in
    [0,1]; money is USD decimals encoded as strings for exactness.
    """
    return {
        "version": 1,
        "wallet_score_weights": {  # PRD 11.2 (sum to 100)
            "roi_quality": 20,
            "consistency": 25,
            "copyability": 30,
            "category_edge": 10,
            "liquidity_quality": 5,
            "entry_timing": 5,
            "resolved_sample_quality": 5,
        },
        "one_hit_wonder_penalty_bands": [  # PRD 11.6: [max_share, min_penalty, max_penalty]
            {"upper_share": 0.25, "penalty_min": 0, "penalty_max": 0},
            {"upper_share": 0.40, "penalty_min": 5, "penalty_max": 10},
            {"upper_share": 0.60, "penalty_min": 10, "penalty_max": 25},
            {"upper_share": 1.00, "penalty_min": 25, "penalty_max": 40},
        ],
        "wallet_status_thresholds": {  # PRD 11.7
            "track_min_score": 70,
            "watch_min_score": 50,
            "min_resolved_trades": 10,
        },
        "freshness_gates_seconds": {  # PRD 12.4
            "order_book": 120,
            "market_metadata_open": 900,
            "wallet_profile": 43200,
        },
        "trade_score_weights": {  # PRD 13.2 (sum to 100)
            "wallet_global_quality": 25,
            "category_fit": 15,
            "price_move_lateness": 15,
            "executable_liquidity": 10,
            "spread": 10,
            "detection_latency": 10,
            "time_to_resolution": 5,
            "thesis_clarity": 10,
        },
        "hard_gates": {
            # PRD 13.3. Defaults chosen per DESIGN.md sec 6 / task spec:
            # max_spread 0.05 and max_price_move_absolute 0.05 are explicit in the
            # task; min_depth_usd/min_time_to_resolution_seconds are sensible
            # starting defaults (depth must cover the largest $20 tier; 1h min).
            "max_spread": 0.05,
            "min_depth_usd": "20",
            "max_price_move_absolute": 0.05,
            "min_time_to_resolution_seconds": 3600,
            "max_slippage": 0.05,
        },
        "decision_thresholds": {  # PRD 13.4
            "paper_copy_min_score": 75,
            "watchlist_min_score": 55,
        },
        "confidence_tiers": [  # PRD 14.2: [min_score, max_score, usd]
            {"min_score": 75, "max_score": 84, "size_usd": "5"},
            {"min_score": 85, "max_score": 92, "size_usd": "10"},
            {"min_score": 93, "max_score": 100, "size_usd": "20"},
        ],
        "exposure_limits": {  # PRD 14.3 (defaults mirror config sec 3)
            "max_open_positions": 25,
            "max_position_usd": "20",
            "max_wallet_exposure_percent": 15,
            "max_category_exposure_percent": 40,
            "max_event_exposure_percent": 10,
            "max_copies_per_wallet_per_day": 3,
        },
        "category_shrinkage_k": 10,  # DESIGN.md sec 6
        "evidence": {  # PRD 16.4 / 17.4 sample floors
            "min_benchmark_sample": 20,
        },
        "rule_evaluator_bounds": {  # PRD 17.5
            "max_relative_change": 0.10,
            "min_judged_decisions": 20,
            "min_relevant_decisions": 10,
            "weight_min": 0,
            "weight_max": 100,
            "weights_total": 100,
            "max_parameter_families_per_day": 1,
        },
    }


async def seed_rule_set(conn: aiosqlite.Connection, *, strategy: str = "default") -> int | None:
    """Insert the v1 active rule set if none exists. Returns its id (or the
    existing active id). Idempotent."""
    cursor = await conn.execute(
        "SELECT id FROM rule_sets WHERE strategy = ? AND status = 'active'",
        (strategy,),
    )
    row = await cursor.fetchone()
    if row is not None:
        return row[0]

    payload = initial_rule_set_payload()
    now = utcnow_iso()
    cursor = await conn.execute(
        """
        INSERT INTO rule_sets
            (strategy, version, status, parameters_json, checksum,
             parent_rule_set_id, activated_at, created_at)
        VALUES (?, 1, 'active', ?, ?, NULL, ?, ?)
        """,
        (strategy, json.dumps(payload), _checksum(payload), now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def active_rule_set_version(conn: aiosqlite.Connection, *, strategy: str = "default") -> int | None:
    cursor = await conn.execute(
        "SELECT version FROM rule_sets WHERE strategy = ? AND status = 'active'",
        (strategy,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def init_db(db_path: str, migrations_dir: str) -> aiosqlite.Connection:
    """Open, migrate and seed. Returns an open connection owned by the caller."""
    conn = await connect(db_path)
    await run_migrations(conn, migrations_dir)
    await seed_rule_set(conn)
    return conn


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def json_dumps(value: object) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), sort_keys=True)


def json_or_none(value: object) -> str | None:
    return None if value is None else json_dumps(value)


def rows_to_dicts(rows: Iterable[aiosqlite.Row]) -> list[dict]:
    return [dict(row) for row in rows]
