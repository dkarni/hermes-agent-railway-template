-- Polymarket research schema (PRD sec 18), SQLite dialect.
-- Conventions (DESIGN.md sec 5):
--   * money & prices are INTEGER micro-units (x 1_000_000); price columns carry
--     CHECK (col BETWEEN 0 AND 1000000).
--   * timestamps are TEXT ISO-8601 UTC (trailing Z).
--   * raw payloads / flexible explanations are TEXT JSON.
--   * is_demo INTEGER NOT NULL DEFAULT 0 marks demo rows (excluded from metrics).
-- Applied inside a single transaction by the migration runner.

-- 1. leaderboard_scans -------------------------------------------------------
CREATE TABLE leaderboard_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    time_period TEXT NOT NULL,
    order_by TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    expected_wallet_count INTEGER,
    actual_wallet_count INTEGER,
    lookback_days INTEGER,
    status TEXT NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    raw_summary_json TEXT,
    error_json TEXT,
    job_run_id INTEGER
);

-- 2. leaderboard_entries -----------------------------------------------------
CREATE TABLE leaderboard_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leaderboard_scan_id INTEGER NOT NULL REFERENCES leaderboard_scans(id),
    category TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    rank INTEGER,
    source_pnl INTEGER,          -- micro USD (signed)
    source_volume INTEGER,       -- micro USD
    user_name TEXT,
    profile_image TEXT,
    verified_badge INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,
    UNIQUE (leaderboard_scan_id, wallet_address, category)
);
CREATE INDEX idx_leaderboard_entries_wallet ON leaderboard_entries(wallet_address);
CREATE INDEX idx_leaderboard_entries_scan ON leaderboard_entries(leaderboard_scan_id);

-- 3. wallet_profiles (current row, one per wallet) ---------------------------
CREATE TABLE wallet_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    global_score INTEGER,                 -- 0..100 score, integer
    data_quality_score INTEGER,
    profile_version INTEGER NOT NULL DEFAULT 1,
    status_reason_code TEXT,
    pnl_30d INTEGER,                      -- micro USD (signed)
    blind_copy_pnl_30d INTEGER,          -- micro USD (signed)
    max_drawdown_30d INTEGER,            -- micro USD
    roi_30d INTEGER,                     -- micro (ratio x 1e6, signed)
    win_rate INTEGER,                    -- micro (ratio 0..1e6)
    resolved_trade_count INTEGER,
    trade_count INTEGER,
    profit_concentration_top1 INTEGER,   -- micro (share 0..1e6)
    profit_concentration_top3 INTEGER,   -- micro (share 0..1e6)
    median_detection_delay_seconds INTEGER,
    executable_trade_ratio INTEGER,      -- micro (share 0..1e6)
    profile_window_start TEXT,
    profile_window_end TEXT,
    calculated_at TEXT,
    raw_json TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_wallet_profiles_status ON wallet_profiles(status);

-- 3b. wallet_profile_snapshots (append-only history) -------------------------
CREATE TABLE wallet_profile_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    global_score INTEGER,
    data_quality_score INTEGER,
    status_reason_code TEXT,
    snapshot_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_wallet_profile_snapshots_wallet ON wallet_profile_snapshots(wallet_address);

-- 4. wallet_category_stats ---------------------------------------------------
CREATE TABLE wallet_category_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    category TEXT NOT NULL,
    trade_count INTEGER,
    resolved_trade_count INTEGER,
    pnl INTEGER,               -- micro USD (signed)
    roi INTEGER,               -- micro (signed)
    win_rate INTEGER,          -- micro (0..1e6)
    consistency_score INTEGER, -- 0..100
    copyability_score INTEGER, -- 0..100
    category_score INTEGER,    -- 0..100
    sample_quality_score INTEGER,
    window_start TEXT,
    window_end TEXT,
    calculated_at TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    UNIQUE (wallet_address, category)
);
CREATE INDEX idx_wallet_category_stats_wallet ON wallet_category_stats(wallet_address);
CREATE INDEX idx_wallet_category_stats_category ON wallet_category_stats(category);

-- 5. markets -----------------------------------------------------------------
CREATE TABLE markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    event_id TEXT,
    question TEXT,
    event_title TEXT,
    category TEXT,
    source_category TEXT,
    slug TEXT,
    event_slug TEXT,
    yes_asset_id TEXT,
    no_asset_id TEXT,
    scheduled_resolution_at TEXT,
    resolved_at TEXT,
    winning_outcome TEXT,
    status TEXT,
    metadata_updated_at TEXT,
    raw_json TEXT
);
CREATE INDEX idx_markets_condition ON markets(condition_id);
CREATE INDEX idx_markets_event ON markets(event_id);
CREATE INDEX idx_markets_yes_asset ON markets(yes_asset_id);
CREATE INDEX idx_markets_no_asset ON markets(no_asset_id);
CREATE INDEX idx_markets_status ON markets(status);

-- 6. market_snapshots --------------------------------------------------------
CREATE TABLE market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT REFERENCES markets(market_id),
    asset_id TEXT NOT NULL,
    best_bid INTEGER CHECK (best_bid IS NULL OR best_bid BETWEEN 0 AND 1000000),
    best_ask INTEGER CHECK (best_ask IS NULL OR best_ask BETWEEN 0 AND 1000000),
    spread INTEGER CHECK (spread IS NULL OR spread BETWEEN 0 AND 1000000),
    last_trade_price INTEGER CHECK (last_trade_price IS NULL OR last_trade_price BETWEEN 0 AND 1000000),
    midpoint INTEGER CHECK (midpoint IS NULL OR midpoint BETWEEN 0 AND 1000000),
    depth_5 INTEGER,             -- micro USD depth available for $5 size
    depth_10 INTEGER,            -- micro USD depth available for $10 size
    depth_20 INTEGER,            -- micro USD depth available for $20 size
    liquidity INTEGER,           -- micro USD
    volume INTEGER,              -- micro USD
    data_source TEXT,
    source_timestamp TEXT,
    collected_at TEXT NOT NULL,
    is_stale INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT
);
CREATE INDEX idx_market_snapshots_market_time ON market_snapshots(market_id, collected_at);
CREATE INDEX idx_market_snapshots_asset_time ON market_snapshots(asset_id, collected_at);

-- 7. observed_trades ---------------------------------------------------------
CREATE TABLE observed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    condition_id TEXT,
    market_id TEXT REFERENCES markets(market_id),
    asset_id TEXT,
    transaction_hash TEXT,
    source_side TEXT NOT NULL,          -- BUY | SELL
    outcome TEXT,
    outcome_index INTEGER,
    source_price INTEGER CHECK (source_price IS NULL OR source_price BETWEEN 0 AND 1000000),
    source_size INTEGER,                -- micro shares/USD size from source
    idempotency_key TEXT NOT NULL UNIQUE,
    detected_at TEXT,
    detection_delay_seconds INTEGER,
    source_trade_timestamp TEXT,
    ingestion_run_id INTEGER,
    title TEXT,
    slug TEXT,
    event_slug TEXT,
    raw_json TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_observed_trades_wallet ON observed_trades(wallet_address);
CREATE INDEX idx_observed_trades_condition ON observed_trades(condition_id);
CREATE INDEX idx_observed_trades_asset ON observed_trades(asset_id);
CREATE INDEX idx_observed_trades_detected ON observed_trades(detected_at);

-- 8. rule_sets (append-only; one active per strategy) ------------------------
CREATE TABLE rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL DEFAULT 'default',
    version INTEGER NOT NULL,
    status TEXT NOT NULL,              -- draft | active | rolled_back | superseded
    parameters_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    parent_rule_set_id INTEGER REFERENCES rule_sets(id),
    activated_at TEXT,
    deactivated_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_rule_sets_active
    ON rule_sets(strategy) WHERE status = 'active';
CREATE INDEX idx_rule_sets_version ON rule_sets(strategy, version);

-- 9. rule_changes (append-only) ----------------------------------------------
CREATE TABLE rule_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_set_id INTEGER NOT NULL REFERENCES rule_sets(id),
    parent_rule_set_id INTEGER REFERENCES rule_sets(id),
    parameter_family TEXT NOT NULL,
    parameter_path TEXT,
    old_value_json TEXT,
    new_value_json TEXT,
    sample_size INTEGER,
    target_metric TEXT,
    baseline_value TEXT,
    expected_value TEXT,
    rollback_rule_json TEXT,
    outcome_status TEXT,
    evaluated_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_rule_changes_rule_set ON rule_changes(rule_set_id);

-- 10. decision_journal (append-only) -----------------------------------------
CREATE TABLE decision_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL DEFAULT 'default',
    observed_trade_id INTEGER NOT NULL REFERENCES observed_trades(id),
    wallet_address TEXT NOT NULL,
    market_id TEXT REFERENCES markets(market_id),
    rule_set_id INTEGER REFERENCES rule_sets(id),
    decision TEXT NOT NULL,            -- paper_copy | watchlist | skip
    total_score INTEGER,              -- 0..100
    component_scores_json TEXT,
    decision_reason_code TEXT,
    reasons_json TEXT,
    risks_json TEXT,
    hard_gates_json TEXT,
    market_snapshot_id INTEGER REFERENCES market_snapshots(id),
    wallet_profile_version INTEGER,
    source_entry_price INTEGER CHECK (source_entry_price IS NULL OR source_entry_price BETWEEN 0 AND 1000000),
    executable_entry_price INTEGER CHECK (executable_entry_price IS NULL OR executable_entry_price BETWEEN 0 AND 1000000),
    price_move_absolute INTEGER,      -- micro (signed)
    price_move_percent INTEGER,       -- micro (signed ratio)
    expected_position_usd INTEGER,    -- micro USD
    portfolio_limit_result TEXT,
    data_quality_score INTEGER,
    market_data_timestamp TEXT,
    wallet_profile_timestamp TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0,
    UNIQUE (strategy, observed_trade_id)
);
CREATE INDEX idx_decision_journal_wallet ON decision_journal(wallet_address);
CREATE INDEX idx_decision_journal_decision ON decision_journal(decision);
CREATE INDEX idx_decision_journal_created ON decision_journal(created_at);

-- 11. paper_portfolios -------------------------------------------------------
CREATE TABLE paper_portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    starting_bankroll INTEGER NOT NULL,   -- micro USD
    cash_balance INTEGER NOT NULL,        -- micro USD
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL
);

-- 12. paper_trades -----------------------------------------------------------
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_portfolio_id INTEGER NOT NULL REFERENCES paper_portfolios(id),
    decision_journal_id INTEGER REFERENCES decision_journal(id),
    observed_trade_id INTEGER REFERENCES observed_trades(id),
    wallet_address TEXT,
    market_id TEXT REFERENCES markets(market_id),
    asset_id TEXT,
    outcome TEXT,
    status TEXT NOT NULL,               -- open | closed | resolved
    shares INTEGER,                     -- micro shares
    entry_price INTEGER CHECK (entry_price IS NULL OR entry_price BETWEEN 0 AND 1000000),
    entry_best_ask INTEGER CHECK (entry_best_ask IS NULL OR entry_best_ask BETWEEN 0 AND 1000000),
    entry_slippage INTEGER,             -- micro (signed)
    entry_fee INTEGER,                  -- micro USD
    entry_cost INTEGER,                 -- micro USD
    exit_price INTEGER CHECK (exit_price IS NULL OR exit_price BETWEEN 0 AND 1000000),
    exit_fee INTEGER,                   -- micro USD
    exit_reason TEXT,
    realized_pnl INTEGER,               -- micro USD (signed)
    rule_set_id INTEGER REFERENCES rule_sets(id),
    benchmark_cohort TEXT NOT NULL DEFAULT 'filtered',
    opened_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_paper_trades_portfolio ON paper_trades(paper_portfolio_id);
CREATE INDEX idx_paper_trades_status ON paper_trades(status);
CREATE INDEX idx_paper_trades_wallet ON paper_trades(wallet_address);
CREATE INDEX idx_paper_trades_market ON paper_trades(market_id);

-- 13. paper_ledger (append-only) ---------------------------------------------
CREATE TABLE paper_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_portfolio_id INTEGER NOT NULL REFERENCES paper_portfolios(id),
    paper_trade_id INTEGER REFERENCES paper_trades(id),
    entry_type TEXT NOT NULL,
    amount INTEGER NOT NULL,            -- micro USD (signed)
    balance_after INTEGER NOT NULL,     -- micro USD
    created_at TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX idx_paper_ledger_portfolio ON paper_ledger(paper_portfolio_id);
CREATE INDEX idx_paper_ledger_trade ON paper_ledger(paper_trade_id);

-- 14. pnl_snapshots ----------------------------------------------------------
CREATE TABLE pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_portfolio_id INTEGER NOT NULL REFERENCES paper_portfolios(id),
    cash_balance INTEGER NOT NULL,      -- micro USD
    open_cost INTEGER NOT NULL,         -- micro USD
    unrealized_pnl INTEGER NOT NULL,    -- micro USD (signed)
    realized_pnl INTEGER NOT NULL,      -- micro USD (signed)
    equity INTEGER NOT NULL,            -- micro USD
    drawdown INTEGER,                   -- micro USD
    price_source TEXT,
    source_timestamp TEXT,
    collected_at TEXT NOT NULL
);
CREATE INDEX idx_pnl_snapshots_portfolio_time ON pnl_snapshots(paper_portfolio_id, collected_at);

-- 15. outcome_reviews --------------------------------------------------------
CREATE TABLE outcome_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_journal_id INTEGER NOT NULL REFERENCES decision_journal(id),
    paper_trade_id INTEGER REFERENCES paper_trades(id),
    review_checkpoint TEXT NOT NULL,    -- 1h | 6h | 24h | final
    price_at_checkpoint INTEGER CHECK (price_at_checkpoint IS NULL OR price_at_checkpoint BETWEEN 0 AND 1000000),
    hypothetical_pnl INTEGER,           -- micro USD (signed)
    actual_pnl INTEGER,                 -- micro USD (signed)
    decision_quality_label TEXT,
    market_snapshot_id INTEGER REFERENCES market_snapshots(id),
    data_quality_score INTEGER,
    eligible_for_learning INTEGER NOT NULL DEFAULT 0,
    notes_json TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0,
    UNIQUE (decision_journal_id, review_checkpoint)
);
CREATE INDEX idx_outcome_reviews_decision ON outcome_reviews(decision_journal_id);

-- 16. benchmark_trades -------------------------------------------------------
CREATE TABLE benchmark_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_trade_id INTEGER NOT NULL REFERENCES observed_trades(id),
    cohort TEXT NOT NULL,               -- blind | watchlist | skip
    simulated_entry_price INTEGER CHECK (simulated_entry_price IS NULL OR simulated_entry_price BETWEEN 0 AND 1000000),
    simulated_position_size INTEGER,    -- micro USD
    final_pnl INTEGER,                  -- micro USD (signed)
    decision_quality_label TEXT,
    created_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_benchmark_trades_observed ON benchmark_trades(observed_trade_id);
CREATE INDEX idx_benchmark_trades_cohort ON benchmark_trades(cohort);

-- 17. daily_reports ----------------------------------------------------------
CREATE TABLE daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    strategy_version INTEGER,
    filtered_pnl INTEGER,               -- micro USD (signed)
    blind_copy_pnl INTEGER,             -- micro USD (signed)
    filtered_minus_blind_pnl INTEGER,   -- micro USD (signed)
    max_drawdown INTEGER,               -- micro USD
    summary_json TEXT,
    data_health_json TEXT,
    delivery_status TEXT,
    telegram_message_id TEXT,
    delivery_error TEXT,
    created_at TEXT NOT NULL
);

-- 18. job_runs ---------------------------------------------------------------
CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,               -- running | success | error
    records_read INTEGER NOT NULL DEFAULT 0,
    records_written INTEGER NOT NULL DEFAULT 0,
    records_skipped INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    lock_key TEXT,
    error_json TEXT,
    metadata_json TEXT
);
CREATE INDEX idx_job_runs_name ON job_runs(job_name);
CREATE INDEX idx_job_runs_started ON job_runs(started_at);

-- 19. data_quality_events ----------------------------------------------------
CREATE TABLE data_quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,             -- info | warning | critical
    source TEXT,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    details_json TEXT
);
CREATE INDEX idx_data_quality_events_severity ON data_quality_events(severity);
CREATE INDEX idx_data_quality_events_detected ON data_quality_events(detected_at);

-- 20. alerts -----------------------------------------------------------------
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    dedupe_key TEXT,
    message TEXT NOT NULL,
    sent_at TEXT,
    delivery_channel TEXT,
    delivery_status TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_alerts_dedupe ON alerts(dedupe_key);
