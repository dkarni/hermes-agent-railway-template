# Polymarket Copy-Trading Research System

**Paper trading only. No keys, no signing, no orders. Ever.**

A research system that watches top Polymarket wallets, scores them, decides which
of their trades a disciplined copier *would* take, simulates those trades on a
paper portfolio, reviews the outcomes, and learns bounded rule changes from the
evidence — then compares the filtered strategy against blind copying. Everything
is surfaced on a dashboard and readable by a dedicated Hermes profile over MCP.

## What it does

- Scans the Polymarket leaderboard (v1 API) across categories.
- Ingests each candidate wallet's ~30-day history and profiles it (ROI quality,
  consistency, copyability, category edge, one-hit-wonder penalty, data quality).
- Monitors tracked wallets (REST polling every 60s), detects new trades, scores
  each signal against hard gates + weighted factors, and records a decision.
- Simulates fills on the real order book ladder into a paper portfolio with
  exposure limits; runs a parallel **blind** cohort (fixed $10, no filters).
- Snapshots PnL hourly, reviews decisions at 1h/6h/24h/final, labels them.
- Runs a daily, bounded, evidence-gated rule evaluator (with rollback).
- Composes daily/weekly reports (dashboard-only delivery).

## What it does NOT do

- It never places, signs, or simulates a *real* order. There are no signing
  dependencies (`py_clob_client`, `eth_account`, `web3`) anywhere in the tree.
- It does not accept wallet private keys or credentials via any route or tool.
- It does not deliver over Telegram from the worker (reports live on the
  dashboard; the dedicated Hermes profile reads them via MCP).
- No WebSocket in v1 (REST polling only).

## Architecture

```
Railway service "Hermes Agent"
└── server.py (Starlette, $PORT, basic auth)
    ├── hermes / marco / max gateways        (existing)
    ├── poly worker  ── python -m polymarket.worker
    │     • uvicorn 127.0.0.1:8700 (loopback; server.py proxy is the auth boundary)
    │     • asyncio scheduler (11 jobs)
    │     • SQLite /data/polymarket/polymarket.db (WAL)
    └── polymarket gateway ── hermes -p polymarket gateway run
          • dedicated Hermes profile (own Telegram bot + LLM engine)
          • MCP: polymarket_server.py → HTTP → 127.0.0.1:8700

Browser ──(basic auth)──▶ server.py ──proxy──▶ 127.0.0.1:8700
  /polymarket/*  and  /api/polymarket/*   (path preserved verbatim)
```

`server.py` proxies `/polymarket`, `/polymarket/{path}`, and
`/api/polymarket/{path}` to the worker after enforcing basic auth (401 before
proxy; 502 JSON if the worker is down).

## Environment variables

All optional except that `TRADING_MODE` must be `paper` (anything else refuses to
start). Defaults let the worker boot with zero Railway changes.

| Var | Default | Notes |
|-----|---------|-------|
| `TRADING_MODE` | `paper` | Hard guard; non-`paper` → exit at startup. |
| `POLYMARKET_ENABLED` | `1` | Set `0` to skip starting the worker. |
| `POLY_DATA_DIR` | `/data/polymarket` | DB dir. Local dev: `./local-data/polymarket`. |
| `POLY_PORT` | `8700` | Loopback API port. |
| `POLY_WORKER_URL` | `http://127.0.0.1:8700` | server.py proxy target. |
| `LEADERBOARD_WALLET_LIMIT` | `500` | Candidate universe cap. |
| `LEADERBOARD_CATEGORIES` | `OVERALL,POLITICS,SPORTS,CRYPTO` | |
| `WALLET_LOOKBACK_DAYS` | `30` | Profiling window. |
| `TRACKED_WALLET_LIMIT` | `30` | Rate-limit budget cap. |
| `TRACKED_WALLET_POLL_SECONDS` | `60` | Monitor cadence. |
| `PAPER_STARTING_BANKROLL` | `1000` | USD. |
| `RULE_UPDATE_ENABLED` | `true` | Disable to freeze rules. |
| `RULE_UPDATE_MIN_DAYS` | `7` | Burn-in: no automatic rule changes before this many days of paper operation. |
| `REPORT_TIMEZONE` | `Europe/Madrid` | |
| `DAILY_REPORT_TIME` | `21:00` | Local cutoff. |
| `DEMO_MODE` | `false` | Demo rows excluded from metrics. |

Strategy parameters (weights, thresholds, gates, exposure limits) do **not** live
in env — they live in the seeded `rule_sets` v1 payload so the evaluator can
version them.

## Local development

```bash
# Boot the worker directly:
POLY_DATA_DIR=$PWD/local-data/polymarket POLY_PORT=8700 TRADING_MODE=paper \
  python -m polymarket.worker
# Dashboard: http://127.0.0.1:8700/polymarket
# API:       http://127.0.0.1:8700/api/polymarket/health

# Tests:
python -m pytest polymarket/tests/ -q
```

### Spain DNS-block workaround

Daniel's ISP (Spain) DNS-hijacks `*.polymarket.com` to a regulator block page
(wrong TLS cert). Live API calls from that machine must resolve via 1.1.1.1:

```bash
curl --resolve data-api.polymarket.com:443:$(dig +short @1.1.1.1 data-api.polymarket.com | tail -1) \
  "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50"
```

The test suite uses recorded fixtures by default; live contract tests are opt-in
(`POLY_LIVE_TESTS=1`). Or just run on Railway.

## Job schedule

| Job | Cadence | Purpose |
|-----|---------|---------|
| `leaderboard_scan` | daily 03:00 | Refresh candidate universe. |
| `ingest_history` | every 600s | Pull 30-day wallet histories. |
| `profile_wallets` | every 1800s | Recompute scores + statuses. |
| `monitor` | every 60s | Detect new trades, score, decide, paper-copy. |
| `reconcile` | every 900s | Catch missed trades / resolutions. |
| `pnl` | every 3600s | Mark open positions, snapshot equity/drawdown. |
| `reviews` | every 300s | 1h/6h/24h/final checkpoints + labels. |
| `health` | every 3600s | Drawdown breach + repeated-failure alerts. |
| `daily_report` | daily `DAILY_REPORT_TIME` | Compose + store daily report. |
| `weekly_report` | daily +2m (Sundays only) | Weekly + autonomy gates. |
| `rule_eval` | daily +5m | Bounded rule change or rollback. |

## Scoring summary

- **Wallet score** (0–100): weighted blend of ROI quality, consistency,
  copyability, category edge, liquidity, entry timing, resolved-sample quality;
  minus a one-hit-wonder penalty from profit concentration. Status: track ≥70,
  watch 50–69, ignore <50 (min 10 resolved).
- **Signal score** (0–100): wallet quality, category fit, price-move lateness,
  executable liquidity, spread, detection latency, time-to-resolution, thesis
  clarity — subject to hard gates (spread, depth, price move, time, slippage).
  Decision: copy ≥75, watch 55–74, skip <55. Size by confidence tier.
- Category shrinkage blends the category sample with the overall score using
  weight `n/(n+k)`, `k=10`. Every number comes from the active rule set.

## Dashboard guide

`/polymarket` (overview — three questions above the fold: profitable vs blind?
which wallets to copy? what changed today?), `/wallets` (+ `/{address}`),
`/signals`, `/paper-trades`, `/journal`, `/performance`, `/rules`, `/reports`,
`/health`. Every page shows the PAPER TRADING ONLY badge, active rule version,
and data-freshness line. Scores expand to their component breakdown. Money is
labelled paper vs source vs blind. CSV export on wallets/journal/paper-trades.

## Troubleshooting

- **Worker won't start:** check `TRADING_MODE=paper`; check `/health` on the main
  server for `poly_worker` state; read worker logs.
- **502 on /polymarket:** the worker is down — server.py returns
  `{"error":"polymarket worker unavailable"}`. Restart the service.
- **Stale data banners:** a monitor/pnl job hasn't succeeded recently — see
  `/polymarket/health` for last-success times and failed jobs (retry there).
- **Partial scan warning:** the latest leaderboard scan for some category didn't
  get all pages — usually rate limiting. The banner clears once the next
  complete scan for that category lands. Partial scans only seed wallet
  discovery; statuses are computed exclusively from wallets with fully
  ingested history (`history_complete = 1`), so a partial scan can never
  promote or downgrade anyone.
- **No metrics:** with an empty DB most numbers are `—` until the first scans,
  profiles, and paper trades land. Filtered-vs-blind stays "insufficient sample"
  until both cohorts reach the minimum.

See `SAFETY.md`, `OPERATIONS.md`, and `DATA_DICTIONARY.md` for more.
