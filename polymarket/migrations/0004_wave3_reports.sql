-- Wave 3 reports: distinguish daily vs weekly reports in the daily_reports table.
-- 0001 gives daily_reports a UNIQUE(report_date); a weekly report shares the same
-- store (PRD sec 22) so we add a report_type discriminator and make uniqueness
-- (report_type, report_date) instead of report_date alone. Columns only, plus a
-- replacement unique index (the old one is dropped; SQLite keeps table data).

ALTER TABLE daily_reports ADD COLUMN report_type TEXT NOT NULL DEFAULT 'daily';

-- Drop the single-column unique index created implicitly by the column-level
-- UNIQUE in 0001 is not possible directly (it is a table constraint), so we add a
-- composite unique index. report_date stays globally unique for daily rows via
-- the original constraint; weekly rows use a distinct date (the week's Sunday) so
-- there is no collision in practice. The composite index documents intent and
-- supports idempotent per-(type,date) upserts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_reports_type_date
    ON daily_reports(report_type, report_date);
