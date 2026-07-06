# Operations

## Service map

| Component | Where | Notes |
|-----------|-------|-------|
| `server.py` | Railway "Hermes Agent", `$PORT` | Basic auth; proxies `/polymarket/*` + `/api/polymarket/*`. |
| poly worker | subprocess `python -m polymarket.worker` | uvicorn on `127.0.0.1:8700`; scheduler + API. |
| polymarket gateway | subprocess `hermes -p polymarket gateway run` | Dedicated Hermes profile (own Telegram + LLM). |
| MCP server | `<profile>/mcp/polymarket_server.py` | stdio; HTTP → `127.0.0.1:8700`. |
| Database | `/data/polymarket/polymarket.db` | SQLite WAL; single writer (the worker). |

The worker is started by `server.py` in lifespan when `POLYMARKET_ENABLED != 0`;
the polymarket gateway starts when `<profile>/config.yaml` exists. Both are
stopped on shutdown. `poly_worker` and `polymarket_gateway` states appear in the
main `/health` payload.

## Health checks

- Main server: `GET /health` (basic auth) → includes `poly_worker` and
  `polymarket_gateway` states.
- Worker API: `GET /api/polymarket/health` → migrations, active rule version,
  scheduler jobs (expect 11), open positions, equity, and an `ops` block
  (freshness, last-successful/failed jobs, stale profiles, open DQ events).
- Dashboard: `GET /polymarket/health` renders the same operationally.

## Manual actions

Via **dashboard** (buttons on `/polymarket/rules` and `/polymarket/health`),
**API** (`POST /api/polymarket/actions/*`), or **MCP** (`poly_run_job(name)`):

| Action | Job | Effect |
|--------|-----|--------|
| scan-leaderboard | `leaderboard_scan` | Refresh candidate universe. |
| profile-wallets | `profile_wallets` | Recompute scores/statuses. |
| reconcile-trades | `reconcile` | Backfill missed trades/resolutions. |
| update-pnl | `pnl` | Re-mark positions, snapshot equity. |
| review-outcomes | `reviews` | Run due checkpoints. |
| evaluate-rules | `rule_eval` | Bounded rule change or rollback. |
| generate-report | `daily_report` | Recompose + store today's report. |
| reset-portfolio | `reset_portfolio` | Retire active portfolio, start fresh version (admin). |
| retry-job/{id} | (re-dispatch) | Retry a failed run's action. |
| rollback-rule/{version} | `rule_eval` path | Manual rule rollback (admin). |

Actions return a `job_run_id` immediately (HTTP 202). If the underlying job is
already running, the API returns **409** with the running run's id — the
scheduler's per-name asyncio lock prevents overlap.

## Job cadences

See the schedule table in `README.md`. All jobs are in-process on the worker's
asyncio scheduler; there are no Railway cron services.

## Database + backup

- Location: `/data/polymarket/polymarket.db` (+ WAL/SHM) on the persistent
  Railway volume. Append-only tables: `rule_sets`, `rule_changes`,
  `paper_ledger`, `job_runs`, `decision_journal`.
- **Backup** = back up the Railway volume (snapshot `/data/polymarket/`). Because
  only the worker writes and it uses WAL, a volume snapshot is consistent enough
  for research recovery; for a clean copy, stop the worker
  (`POLYMARKET_ENABLED=0` + redeploy, or stop the service) before copying.
- To reset paper trading without losing history, use **reset-portfolio** (retires
  the active portfolio, preserves all prior rows, starts a new version).

## Rule rollback procedure

1. Open `/polymarket/rules`. The active version and its change history (before →
   after, evidence window, sample size, expected vs actual outcome) are shown.
2. To revert the active version to its parent, click **rollback** (admin) or
   `POST /api/polymarket/actions/rollback-rule/{version}` (or
   `poly_rollback_rule(version)` via MCP). This reuses the deterministic
   `_do_rollback` path: it clones the parent's parameters into a new active
   rule set, retires the tripped one as `rolled_back`, and marks the change
   `rolled_back` — all append-only.
3. Automatic rollback also happens in `rule_eval` when a pending change's stored
   `rollback_rule_json` trips against the new evidence window.

## Retry / reconcile

- **Failed job:** `/polymarket/health` lists failed runs with the error; click
  **retry** (or `POST /actions/retry-job/{id}`) to re-dispatch the same action.
- **Missed trades / resolutions:** run **reconcile-trades**; it re-scans recent
  history and market resolutions and fills gaps idempotently.
- **Stale marks:** run **update-pnl**; open positions whose mark could not be
  refreshed are flagged `mark_is_stale` and carried forward (never invented).
