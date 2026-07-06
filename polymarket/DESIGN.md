# Polymarket Copy-Trading Research System — Architecture & Build Spec

**Status:** Approved architecture (amends `Hermes_Polymarket_PRD_v1.1.md`)
**Date:** 2026-07-06
**Mode:** Paper trading only. No keys, no signing, no orders. Ever.

This document is the binding spec for implementation. Where it conflicts with the
PRD, this document wins. Read the PRD for full product intent (scoring formulas,
dashboard fields, report contents); read this for how it is actually built.

---

## 1. Amendments to the PRD (architect decisions)

1. **SQLite, not PostgreSQL.** The PRD banned production SQLite because it assumed
   an ephemeral filesystem. This deployment has a persistent Railway volume at
   `/data`. Database lives at `/data/polymarket/polymarket.db` (WAL mode).
   Single-writer design: only the worker process opens the DB for writing.
   No Postgres service is added to Railway.
2. **Single Railway service, no cron services.** The whole system is one new
   long-running subprocess (`python -m polymarket.worker`) inside the existing
   "Hermes Agent" service, managed by `server.py`'s `ManagedProcess` (same
   pattern as `marco_gateway` / `track_bridge`). All scheduled jobs run on an
   in-process asyncio scheduler inside the worker.
3. **No WebSocket in v1.** REST polling only (tracked wallets every 60s, order
   books fetched on demand per signal + hourly PnL). The PRD's WS requirements
   (§8.6) are deferred; the adapter interface leaves room to add WS later.
4. **Dashboard is server-rendered Jinja2 + Tailwind/Alpine** (matching
   `templates/index.html` style), served by the worker on loopback and proxied
   by `server.py` behind its existing basic auth. No Next.js, no Vercel.
5. **Endpoint corrections (verified live 2026-07-06):**
   - Leaderboard: `GET https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50&offset=N[&category=X]`
     — **`/v1/`, not `/v2/`** (PRD §8.2 was wrong; `/v2/leaderboard` and bare
     `/leaderboard` both 404). Max 50 rows per page regardless of `limit`;
     `offset` pagination works; response rows:
     `{"rank":"1","proxyWallet":"0x..","userName":"..","xUsername":"","verifiedBadge":false,"vol":float,"pnl":float,"profileImage":".."}`.
     `category=POLITICS` etc. works.
   - User trades: `GET https://data-api.polymarket.com/trades?user=<proxyWallet>&limit=N&offset=N`
     — rows: `{"proxyWallet","side":"BUY|SELL","asset","conditionId","size":float,"price":float,"timestamp":unix,"title","slug","eventSlug","outcome","outcomeIndex","name","pseudonym","transactionHash",...}`.
   - Market metadata: `GET https://gamma-api.polymarket.com/markets?condition_ids=0x...`
     (also `?slug=`, `?closed=false&limit=`). Fields include `question`,
     `conditionId`, `slug`, `endDate`, `liquidity`, `outcomes`, `outcomePrices`,
     `clobTokenIds` (JSON-encoded string arrays), `closed`, `category`, `events`.
   - Order book: `GET https://clob.polymarket.com/book?token_id=<asset>` —
     `{"market","asset_id","timestamp","bids":[{"price","size"}],"asks":[...]}`.
     NOTE: bids/asks are each sorted worst-to-best (asks descending, bids
     ascending) — best ask is the LAST element of `asks`, best bid the LAST of
     `bids`. Normalize in the adapter and add a unit test for this.
     Also `GET /midpoint?token_id=`.
6. **Money & price representation:** all money and price columns are INTEGER
   micro-units (× 1,000,000). Prices in [0,1] → 0..1_000_000. Arithmetic uses
   `decimal.Decimal` in code; helpers `usd_to_micro / micro_to_usd` live in
   `polymarket/db.py`. Never store or compute money as float; adapter parses
   API floats via `Decimal(str(x))` at the boundary.
7. **Hermes integration = dedicated "polymarket" profile (owner decision
   2026-07-06).** Daniel created a Hermes profile named `polymarket` on the
   volume with its own Telegram bot and LLM engine, so this domain never mixes
   with the main/marco/max profiles. Integration mirrors the marco/max pattern:
   - `bootstrap_poly.py` targets `/data/.hermes/profiles/polymarket/`:
     copies `polymarket_server.py` into `<profile>/mcp/`, writes
     `<profile>/skills/polymarket-research/SKILL.md` (write-if-missing), and
     ensures `<profile>/config.yaml` contains the `mcp_servers.polymarket`
     entry — the profile already exists and is user-configured, so the config
     must be MERGED via yaml load/dump (add the key only if absent, preserve
     everything else; on parse error, warn and leave the file untouched).
   - `server.py` gains `poly_gateway = ManagedProcess("polymarket gateway",
     ["hermes", "-p", "polymarket", "gateway", "run", "--replace"],
     env_paths=(profile/.env, profile/hermes.env))`, started in lifespan when
     the profile's `config.yaml` exists — exactly like `max_gateway`.
   **No Telegram delivery (owner decision 2026-07-06):** daily/weekly reports
   and alerts are composed, stored, and rendered on the dashboard only
   (`delivery_channel='dashboard'`). Hermes can read them via MCP on request.
   The PRD's Telegram sections (§21.2, §22 delivery, `TELEGRAM_*` env vars) are
   dropped; `api.telegram.org` is not needed in the outbound allowlist.
8. **Local-dev note:** Daniel's ISP (Spain) DNS-hijacks `*.polymarket.com` to a
   regulator block page (wrong TLS cert). Live API tests from this machine must
   resolve via 1.1.1.1 (e.g. `curl --resolve host:443:$(dig +short @1.1.1.1 host | tail -1)`)
   or run on Railway. The test suite therefore uses recorded fixtures by
   default; live contract tests are opt-in (`POLY_LIVE_TESTS=1`).

## 2. Process & repo topology

```
Railway service "Hermes Agent" (existing, unchanged deploy flow: `railway up`)
└── server.py (Starlette, port $PORT, basic auth)
    ├── hermes gateway            (existing)
    ├── marco gateway + bridge    (existing)
    ├── max gateway               (existing)
    └── poly worker  ← NEW  ManagedProcess: ["python", "-m", "polymarket.worker"]
        • uvicorn on 127.0.0.1:8700 (loopback only — server.py proxy is the auth boundary)
        • asyncio scheduler (all jobs from PRD §9, polling instead of WS)
        • SQLite at /data/polymarket/polymarket.db
```

`server.py` additions (small, surgical):
- `poly_worker = ManagedProcess("poly worker", ["python", "-m", "polymarket.worker"], cwd="/app")`,
  started in lifespan when `os.environ.get("POLYMARKET_ENABLED", "1") != "0"`.
- Reverse proxy: `/polymarket` and `/polymarket/{path:path}` and
  `/api/polymarket/{path:path}` → `http://127.0.0.1:8700/...` via httpx
  (stream body, preserve method/query/content-type), wrapped in `require_auth`.
- `poly_worker.state` added to `/health`.

Repo layout (new files only):

```
polymarket/
  DESIGN.md                 (this file)
  __init__.py
  config.py                 env parsing+validation; hard TRADING_MODE=paper guard
  db.py                     sqlite (WAL, foreign_keys, busy_timeout), migration runner, micro-unit helpers
  migrations/0001_init.sql  full schema (PRD §18, micro-unit columns)
  http.py                   shared async client factory: host ALLOWLIST, per-host rate limiter,
                            retries w/ backoff+jitter, Retry-After, data_quality_events hooks
  adapters/
    gamma.py dataapi.py clob.py   (+ models.py: frozen dataclasses)
  domain/
    wallet_stats.py scoring.py trade_scoring.py decisions.py
    paper.py benchmarks.py outcomes.py rules.py
  jobs/
    runner.py               job_run recording, per-job asyncio locks, idempotency
    leaderboard_scan.py profile_wallets.py ingest_history.py monitor.py
    reconcile.py pnl.py reviews.py rule_eval.py reports.py
  scheduler.py
  worker.py                 entrypoint: init db → validate config → start scheduler + API
  api.py                    Starlette routes (PRD §19), JSON, no auth (loopback)
  ui/                       routes + templates/*.html (Jinja2)
  tests/                    pytest; fixtures/ recorded API payloads
bootstrap_poly.py           config.yaml merge + skill + MCP copy (write-if-missing semantics)
bootstrap_files/mcp/polymarket_server.py
bootstrap_files/poly/SKILL.md
docs: polymarket/README.md, SAFETY.md, OPERATIONS.md, DATA_DICTIONARY.md
```

Dockerfile: `COPY polymarket/ /app/polymarket/`, `COPY bootstrap_poly.py`,
bootstrap_files already copied. `start.sh`: add `python /app/bootstrap_poly.py`.
`requirements.txt`: add `aiosqlite`, `pyyaml`. Dev-only `requirements-dev.txt`:
`pytest`, `pytest-asyncio`.

## 3. Configuration (config.py)

All env vars optional except none; defaults chosen so zero Railway changes are
needed to boot (Telegram chat id being unset just skips delivery):

```
TRADING_MODE=paper              # anything else → sys.exit at startup (also a unit test)
POLY_DATA_DIR=/data/polymarket  # local dev: ./local-data/polymarket
POLY_PORT=8700
POLYMARKET_GAMMA_BASE_URL=https://gamma-api.polymarket.com
POLYMARKET_DATA_BASE_URL=https://data-api.polymarket.com
POLYMARKET_CLOB_BASE_URL=https://clob.polymarket.com
LEADERBOARD_WALLET_LIMIT=500
LEADERBOARD_CATEGORIES=OVERALL,POLITICS,SPORTS,CRYPTO   # first release subset
WALLET_LOOKBACK_DAYS=30
TRACKED_WALLET_LIMIT=30         # cap on `track` status wallets (rate-limit budget)
TRACKED_WALLET_POLL_SECONDS=60
MARKET_DATA_MAX_AGE_SECONDS=120
PAPER_STARTING_BANKROLL=1000    # USD
PAPER_MAX_OPEN_POSITIONS=25
PAPER_MAX_POSITION_USD=20
PAPER_MAX_WALLET_EXPOSURE_PERCENT=15
PAPER_MAX_CATEGORY_EXPOSURE_PERCENT=40
PAPER_MAX_EVENT_EXPOSURE_PERCENT=10
PAPER_MAX_COPIES_PER_WALLET_PER_DAY=3
RULE_UPDATE_ENABLED=true
REPORT_TIMEZONE=Europe/Madrid
DAILY_REPORT_TIME=21:00
DEMO_MODE=false
LOG_LEVEL=INFO
```

Strategy parameters (weights, thresholds, gates — PRD §11/§13/§14/§17) do NOT
live in env: they live in the seeded `rule_sets` row v1 (JSON payload matching
PRD initial weights), so the rule evaluator can version them.

## 4. Safety implementation (PRD §5, release-blocking)

- `config.py` refuses startup unless `TRADING_MODE == "paper"`.
- `http.py` allowlist: only the 3 Polymarket hosts.
  Any other host raises `DisallowedHostError` and records a data_quality_event.
- Static safety test (`tests/test_safety.py`): greps the `polymarket/` tree for
  forbidden imports/strings (`py_clob_client`, `eth_account`, `private_key`,
  `PRIVATE_KEY`, `signature`, `signTypedData`, `/order` POST paths, `web3`);
  asserts no route or schema column mentions keys; asserts allowlist contents.
- No secrets in logs: structured log helper redacts sensitive values.
- Demo data only via `DEMO_MODE`; every demo row carries `is_demo=1` and is
  excluded from metrics, learning and reports (WHERE clauses tested).

## 5. Database conventions

- Schema per PRD §18 with all entities, but SQLite dialect:
  `INTEGER PRIMARY KEY AUTOINCREMENT` ids (or TEXT ulid where natural),
  timestamps TEXT ISO-8601 UTC (`...Z`), money/prices INTEGER micro-units,
  JSON payloads TEXT. CHECK constraints for price bounds (0..1_000_000).
- All migrations numbered SQL files applied in order, recorded in
  `schema_migrations`. Never edit an applied migration.
- Writer discipline: only worker writes. WAL mode, `busy_timeout=5000`,
  `foreign_keys=ON`, one aiosqlite connection shared via db.py.
- Append-only for `rule_sets`, `rule_changes`, `paper_ledger`, `job_runs`,
  `decision_journal` (status/label columns may be updated, rows never deleted).

## 6. Scoring & decision engine (deterministic, unit-tested)

Implements PRD §10–§13 exactly (component weights, one-hit-wonder penalty bands,
status thresholds track≥70/watch 50–69/ignore<50, decision thresholds
copy≥75/watch 55–74/skip<55, hard gates list, freshness gates 120s/15m/12h,
confidence tiers 75–84→$5, 85–92→$10, 93–100→$20) — but every number comes from
the active rule_set JSON, not constants. Category shrinkage: category score =
weighted blend of category sample and wallet overall score with weight
`n/(n+k)`, k=10 (in rule set).

## 7. Paper engine (PRD §14) & benchmarks (§16)

- Fill simulation walks the (normalized) ask ladder for BUYs; slippage limit
  from rule set; partial-size reduction allowed; exits at resolution (winning
  outcome → $1/share) or qualifying source SELL. No shorts.
- Cohorts: `filtered` (the strategy) and `blind` (same execution model, same
  bankroll size, fixed $10 sizing, no quality filters) — blind is simulated in
  `benchmark_trades` (not in the real portfolio) using identical pricing snapshots.
- Reviews at 1h/6h/24h/final via scheduler scanning `decision_journal` for due
  checkpoints; labels per PRD §15.3.

## 8. Rule evaluator (PRD §17)

Deterministic: needs ≥20 judged decisions since last change (≥10 relevant),
no critical data_quality_event in window, one parameter family per day, ≤10%
relative numeric moves, weights renormalized to 100%, new rule_set row
(append-only) with rollback_rule_json; rollback path implemented + tested.
Runs daily after report cutoff; disabled when `RULE_UPDATE_ENABLED=false`.

## 9. Internal API & dashboard

Routes exactly as PRD §19 (mounted at `/api/polymarket/...` from the proxy's
perspective; the worker serves them without the prefix too — keep paths
identical to PRD by serving `/api/polymarket/*` on the worker and proxying
verbatim). Actions return `{job_run_id}` immediately. Dashboard pages
(PRD §20): overview, wallets, wallet detail, signals, paper-trades, journal,
performance, rules, reports, health — server-rendered, Tailwind via CDN +
Alpine.js like `templates/index.html`, "PAPER TRADING ONLY" badge in the header,
freshness/stale banners, every score expandable to components.

## 10. MCP server (bootstrap_files/mcp/polymarket_server.py)

FastMCP, stdio, loopback HTTP to `http://127.0.0.1:8700`. Tools (thin wrappers):
`poly_overview`, `poly_wallets(status,category,min_score,limit)`,
`poly_wallet(address)`, `poly_signals(decision,since,limit)`,
`poly_paper_trades(status,limit)`, `poly_performance(window)`,
`poly_rules()`, `poly_report(date)`, `poly_health()`, `poly_job_runs(limit)`,
and actions `poly_run_job(name)` (allowlisted job names), `poly_generate_report()`.
No tool accepts keys/credentials. Env pattern copied from po_server.py.

## 11. Build waves (each = one Opus implementation task)

1. **Foundation:** config, db+migrations (full schema), http allowlist/limiter,
   three adapters + fixtures, jobs/runner, safety tests. Exit: pytest green;
   `python -m polymarket.worker` boots locally, `/api/polymarket/health` OK.
2. **Wallet intelligence + signals:** leaderboard scan, history ingestion,
   wallet_stats, scoring, statuses, monitor loop, observed trades idempotency,
   trade scoring, hard gates, decision journal. Exit: e2e fixture test
   leaderboard→profile→detect→decide passes.
3. **Paper + learning:** portfolio, fills, ledger, PnL snapshots, exits,
   reviews (1h/6h/24h/final), labels, benchmarks, rule evaluator + rollback,
   daily/weekly reports (dashboard-only delivery). Exit: full-lifecycle fixture e2e passes.
4. **Surface + ship:** API routes, dashboard UI, server.py proxy + ManagedProcess,
   bootstrap_poly.py, MCP server + SKILL.md, Dockerfile/start.sh/requirements,
   docs (README/SAFETY/OPERATIONS/DATA_DICTIONARY), deploy via `railway up`,
   live verification on Railway. Exit: dashboard reachable, first real
   leaderboard scan stored, health green, Telegram test message.
