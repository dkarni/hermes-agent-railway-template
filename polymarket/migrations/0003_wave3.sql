-- Wave 3 additions: per-trade current mark, admin flag, portfolio peak equity.
-- Columns only; no edits to applied migrations. Money/prices are micro-units.

-- paper_trades: live mark columns updated hourly by the pnl job, plus is_admin
-- (admin closes are excluded from strategy metrics) and a stale flag when the
-- mark could not be refreshed (carried forward, never invented).
ALTER TABLE paper_trades ADD COLUMN current_mark INTEGER
    CHECK (current_mark IS NULL OR current_mark BETWEEN 0 AND 1000000);
ALTER TABLE paper_trades ADD COLUMN unrealized_pnl INTEGER;      -- micro USD (signed)
ALTER TABLE paper_trades ADD COLUMN mark_updated_at TEXT;
ALTER TABLE paper_trades ADD COLUMN mark_is_stale INTEGER NOT NULL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;

-- paper_portfolios: peak equity high-water mark for drawdown vs peak.
ALTER TABLE paper_portfolios ADD COLUMN peak_equity INTEGER;     -- micro USD
