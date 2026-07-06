# Data Dictionary

Concise reference for the scores, metrics, labels, cohorts, and table field
groups. **Money & prices are stored as INTEGER micro-units (×1,000,000)** and
returned by the API as decimal **strings** in USD (prices in [0,1]). Timestamps
are ISO-8601 UTC (`...Z`).

## Scores

| Score | Range | Meaning |
|-------|-------|---------|
| `global_score` (wallet) | 0–100 | Weighted blend: ROI quality (20), consistency (25), copyability (30), category edge (10), liquidity (5), entry timing (5), resolved-sample quality (5); minus one-hit-wonder penalty. |
| `data_quality_score` | 0–100 | Completeness/reliability of the wallet's ingested history. |
| `category_score` | 0–100 | Per-category quality; shrinkage-blended with overall via `n/(n+k)`, k=10. |
| `consistency_score` | 0–100 | Steadiness of returns across resolved trades. |
| `copyability_score` | 0–100 | How executable the wallet's trades are for a lagged copier (liquidity, speed, spread). |
| `sample_quality_score` | 0–100 | Adequacy of the resolved-trade sample. |
| `total_score` (signal) | 0–100 | Weighted signal factors; drives the decision. |
| Component scores | — | Stored JSON per wallet (`raw_json.score_components`) and per decision (`component_scores_json`); expandable in the UI. |

**One-hit-wonder penalty:** derived from profit concentration
(`profit_concentration_top1/top3`, share 0..1). Penalty bands escalate as more
profit concentrates in one/few markets (0 up to 40 points).

## Statuses & decisions

| Field | Values | Thresholds |
|-------|--------|-----------|
| wallet `status` | `track` / `watch` / `ignore` / `data_error` | track ≥70, watch 50–69, ignore <50; min 10 resolved. |
| `status_reason_code` | e.g. `promoted`, `below_track`, `insufficient_sample` | Why the status was set. |
| decision `decision` | `paper_copy` / `watchlist` / `skip` | copy ≥75, watch 55–74, skip <55. |
| `decision_reason_code` | e.g. `gate_spread`, `low_score` | Primary reason. |
| confidence tier | 75–84 → $5, 85–92 → $10, 93–100 → $20 | Position sizing. |

**Hard gates** (`hard_gates_json`): max spread, min depth (USD), max price move,
min time-to-resolution, max slippage. A failed gate forces `skip`.

**Freshness gates (seconds):** order book 120, open market metadata 900, wallet
profile 43200 (12h). Data older than its gate is treated as stale.

## Decision-quality labels (final review)

| Label | Meaning |
|-------|---------|
| `good_copy` | Copied and it worked. |
| `bad_copy` | Copied and it lost. |
| `missed_winner` | Skipped/watched a trade that won. |
| `avoided_loser` | Skipped a trade that lost (good skip). |
| `good_skip` / `bad_skip` | Skip that was right / wrong. |

`eligible_for_learning` marks reviews the rule evaluator may use (final,
non-demo, non-admin, sufficient data).

## Cohorts

| Cohort | Definition |
|--------|-----------|
| `filtered` | The strategy: quality-filtered, confidence-sized, exposure-limited. Recorded in `paper_trades`. |
| `blind` | Every eligible observed BUY from a tracked-or-better wallet, fixed $10, no filters, identical fill model. Recorded in `benchmark_trades`. |

**Comparison verdict:** `filtered_better` / `blind_better` / `tie` /
`insufficient_sample`. An edge is only claimed when both cohorts reach the
minimum benchmark sample (default 20).

## Performance metrics (per cohort)

`sample`, `net_pnl`, `roi` (net/capital), `win_rate`, `avg_pnl`,
`profit_factor` (gross profit / gross loss), `max_drawdown` (peak-to-trough of
the cumulative realized-PnL curve), `wins`, `losses`, `capital_deployed`.

## Table field groups

- **leaderboard_scans / leaderboard_entries** — scan metadata (partial flag,
  counts) + per-wallet rank/PnL/volume rows.
- **wallet_profiles** — current per-wallet row (scores, status, ROI/win/PnL,
  concentration, detection delay, executable ratio, completeness flags).
- **wallet_profile_snapshots** — append-only history; consecutive rows yield
  status transitions (the "what changed" feed).
- **wallet_category_stats** — per-(wallet,category) metrics + scores.
- **markets / market_snapshots** — market metadata; order-book snapshots
  (bid/ask/spread/midpoint/depth, staleness).
- **observed_trades** — monitor-detected signals (side, outcome, source price,
  detection delay, idempotency key).
- **wallet_trades** — raw 30-day ingested history (distinct from observed).
- **rule_sets / rule_changes** — versioned strategy params (append-only) + the
  change log (family, path, old→new, sample size, target metric, baseline vs
  expected, rollback rule, outcome status).
- **decision_journal** — one row per scored signal: decision, total + component
  scores, reasons, risks, gates, prices, portfolio-limit result, data known at
  decision time.
- **paper_portfolios / paper_trades / paper_ledger** — portfolio (bankroll, cash,
  peak equity, version), simulated positions (shares, entry/exit, slippage, fee,
  current mark, realized/unrealized PnL, cohort, admin flag), append-only ledger.
- **pnl_snapshots** — hourly cash/open-cost/unrealized/realized/equity/drawdown.
- **outcome_reviews** — 1h/6h/24h/final checkpoints, hypothetical/actual PnL,
  label, learning eligibility, lesson notes.
- **benchmark_trades** — blind cohort simulated entries + final PnL.
- **daily_reports** — daily/weekly reports (`report_type` discriminator),
  filtered/blind/diff PnL, max drawdown, summary + data-health JSON, delivery
  status (`dashboard`).
- **job_runs** — per-run status/counts/error/metadata (drives freshness + retry).
- **data_quality_events** — severity/source/type; unresolved criticals block
  rule changes.
- **alerts** — dashboard alerts (deduped, quiet periods), `delivery_channel`.

Every metrics table excludes `is_demo = 1` rows.
