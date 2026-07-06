-- Wave 2: raw ingested history + monitor cursors.
-- Conventions unchanged from 0001 (micro-units, ISO-8601 UTC, JSON TEXT).

-- wallet_trades: raw 30-day ingested history, distinct from observed_trades
-- (which are only monitor-detected signals). Idempotency per PRD 8.3 / 12.2 on
-- immutable-ish fields so re-ingesting an overlap window is a no-op.
CREATE TABLE wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_wallet TEXT NOT NULL,
    condition_id TEXT,
    asset_id TEXT,
    transaction_hash TEXT,
    side TEXT NOT NULL,                 -- BUY | SELL
    outcome TEXT,
    outcome_index INTEGER,
    price_micro INTEGER CHECK (price_micro IS NULL OR price_micro BETWEEN 0 AND 1000000),
    size INTEGER,                       -- micro shares
    ts INTEGER NOT NULL,                -- unix seconds (source timestamp)
    title TEXT,
    slug TEXT,
    event_slug TEXT,
    category TEXT,
    ingestion_run_id INTEGER,
    ingested_at TEXT NOT NULL,
    raw_json TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    UNIQUE (proxy_wallet, transaction_hash, asset_id, side, price_micro, size, ts)
);
CREATE INDEX idx_wallet_trades_wallet ON wallet_trades(proxy_wallet);
CREATE INDEX idx_wallet_trades_condition ON wallet_trades(condition_id);
CREATE INDEX idx_wallet_trades_ts ON wallet_trades(ts);

-- monitor_cursors: per-wallet incremental polling cursor (PRD 8.3).
CREATE TABLE monitor_cursors (
    wallet_address TEXT PRIMARY KEY,
    last_trade_ts INTEGER,             -- unix seconds of newest processed trade
    last_polled_at TEXT,
    last_job_run_id INTEGER,
    overlap_seconds INTEGER NOT NULL DEFAULT 120
);

-- wallet_profiles bookkeeping that 0001 lacks: ingestion completeness + refresh
-- scheduling. history_complete drives the data_error status; last_profiled_at
-- and next_profile_due_at let profile_wallets select due wallets cheaply.
ALTER TABLE wallet_profiles ADD COLUMN history_complete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wallet_profiles ADD COLUMN history_ingested_at TEXT;
ALTER TABLE wallet_profiles ADD COLUMN last_profiled_at TEXT;
ALTER TABLE wallet_profiles ADD COLUMN next_profile_due_at TEXT;
ALTER TABLE wallet_profiles ADD COLUMN first_seen_at TEXT;
