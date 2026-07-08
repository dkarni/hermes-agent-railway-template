# Hermes Polymarket Copy Trading Research System

## Product Requirements Document and Technical Specification

**Status:** Development-ready draft  
**Version:** 1.1 (amended — see below)  
**Date:** 6 July 2026  
**Deployment baseline:** Existing Hermes installation already running successfully on Railway  
**Safety mode:** Paper trading only

> **Architect amendments (v1.2, 2026-07-06):** `polymarket/DESIGN.md` is the
> binding technical spec and overrides this PRD where they conflict. Key changes:
> **(1)** SQLite on the persistent Railway `/data` volume replaces PostgreSQL —
> the §7.2 SQLite prohibition assumed an ephemeral filesystem this deployment
> does not have. **(2)** Single Railway service: the system runs as one managed
> worker subprocess inside the existing Hermes Agent service with an in-process
> scheduler; no cron services, queue, or second database. **(3)** WebSockets
> (§8.6) deferred to a later version; REST polling only. **(4)** Dashboard is
> server-rendered Jinja2 behind the existing basic auth, not Next.js.
> **(5)** §8.2's leaderboard endpoint was wrong: the working endpoint (verified
> live) is `data-api.polymarket.com/v1/leaderboard`. **(6)** Money/prices are
> stored as integer micro-units with `Decimal` arithmetic in code.

---

## 1. Executive summary

Build a self-improving Polymarket copy trading research system as a new domain inside the existing Hermes deployment on Railway.

The product does not blindly copy profitable wallets. It first identifies wallets whose performance appears repeatable and realistically copyable, then scores each detected trade at the price and market conditions available to the system. Qualified signals become simulated positions only. The system records every copy, watchlist, and skip decision, follows the outcome, compares the filtered strategy with blind copying, and updates a bounded set of paper-trading rules based on evidence.

Hermes remains the operating and reporting layer. Railway remains the production platform. The new system must reuse the existing Hermes repository, deployment conventions, authentication, logging, and Telegram integration wherever they already exist. It must not create a second Hermes installation or replace a working production setup.

The dashboard must answer three questions immediately:

1. Is the strategy profitable on paper?
2. Which wallets are realistically worth copying?
3. What did the system learn and change today?

Version one is a research system, not a live trading bot. It must not request or store private keys, create signatures, place orders, or execute transactions.

---

## 2. Background and source reconciliation

The complete build-prompt text supplied by the user is the canonical product source. It provides the major product modules, dashboard pages, core database models, safety constraints, test areas, commands, documentation requirements, and implementation order.

The video transcript adds several requirements that must be explicit in the implementation:

- Category-specific skill is mandatory. A wallet can be strong in politics and weak in crypto, or the reverse.
- A good skip is a measurable positive result, not an ignored event.
- Paper trading must simulate a realistic total bankroll, not merely create unlimited independent $5, $10, or $20 bets.
- Outcome reviews must occur after 1 hour, 6 hours, 24 hours, and final market resolution.
- Position sizes use confidence tiers: $5, $10, and $20, while respecting available paper cash and exposure limits.
- Self-improvement means a controlled decision loop with evidence, not unrestricted AI changes.
- Live autonomy is not earned until there are at least 30 days of paper evidence, at least 100 paper trades, a clear advantage over blind copying, no major data failures, and an acceptable drawdown profile.
- Even after those gates are met, live execution requires a separate product and security specification.

---

## 3. Existing-system constraint

### 3.1 Reuse the current Hermes deployment

Before implementation, the development agent must inspect the existing Hermes repository and Railway project and document:

- Application language and framework
- Current process start command
- Existing database and migration tooling
- Existing scheduler or job system
- Existing Telegram integration
- Existing authentication and dashboard shell
- Environment-variable conventions
- Logging and error-reporting conventions
- Current Railway services and private networking
- Current health checks and deployment workflow

The implementation must follow existing conventions unless a documented technical blocker makes that impossible.

### 3.2 Do not rebuild working infrastructure

The following are prohibited unless approved as a separate infrastructure change:

- Creating a second Hermes instance
- Replacing the current Railway deployment
- Replacing existing authentication
- Moving unrelated Hermes features into the new module
- Introducing a second database when the current production database can safely support the new schema
- Adding Redis, a message broker, or another paid dependency before it is needed

### 3.3 Integration boundary

The new domain should be isolated under a clear namespace such as:

```text
polymarket/
  adapters/
  application/
  domain/
  jobs/
  scoring/
  paper-trading/
  rules/
  reports/
  api/
  ui/
```

If the existing repository is a monorepo, keep the new domain in that monorepo. If the current Hermes service cannot safely run the continuous monitor, create a second Railway worker service from the same repository, not a new product or a duplicate Hermes installation.

---

## 4. Product goals

### 4.1 Primary goals

- Discover the top 500 Polymarket wallets and maintain a ranked wallet universe.
- Evaluate 30-day wallet performance by ROI, consistency, copyability, category skill, liquidity, timing, and sample quality.
- Detect new trades from selected wallets with low enough latency to evaluate copyability.
- Decide whether each detected trade should be paper copied, watched, or skipped.
- Simulate realistic execution using the price, spread, liquidity, and delay available at detection time.
- Track paper PnL hourly and at final resolution.
- Measure whether filtering adds value compared with blind copying.
- Learn from copied trades, watched trades, missed winners, avoided losers, and bad copies.
- Change only approved paper-trading rules, within hard safety bounds, and preserve full version history.
- Give Hermes the jobs, APIs, and summaries needed to operate the loop.
- Provide a focused dashboard inside the existing Hermes interface or as a compatible Railway-deployed Next.js application.

### 4.2 Non-goals for version one

- Real trading
- Wallet connection
- Private-key storage
- Order signing
- Deposits or withdrawals
- Custody of funds
- Guaranteed returns
- Unbounded autonomous strategy creation
- High-frequency trading
- Market making
- Copying private or authenticated user activity
- A general-purpose backtesting platform for arbitrary strategies

---

## 5. Safety requirements

The following are release-blocking requirements:

1. No private-key field, secret, environment variable, database column, API route, or UI element may exist for trading credentials.
2. No order-placement, cancellation, signing, bridge, deposit, withdrawal, relayer, or settlement function may be called.
3. The code must use only public read endpoints for market and wallet research.
4. A static test must fail if trading SDK functions or known order-placement endpoints are imported or referenced.
5. A runtime outbound-request allowlist must block unsupported hosts in production.
6. `TRADING_MODE` must be hard-coded or validated as `paper` in version one. Any other value must stop startup.
7. Demo data is allowed only under an explicit demo flag and must be visibly marked in the UI and reports.
8. API failures must be stored and shown as failures. The system must not replace missing live data with invented values.
9. Secrets must be redacted from logs, job payloads, UI error details, and Telegram messages.
10. A future live-trading module must be a separate project phase with new approval, threat modeling, wallet isolation, loss limits, and legal review.

---

## 6. Users and key workflows

### 6.1 Primary user

The owner or operator of the existing Hermes environment.

### 6.2 Core user journeys

**Morning review**

The operator opens the overview and sees paper PnL, active positions, strategy version, wallet upgrades or downgrades, data health, and the latest learning summary.

**Wallet review**

The operator opens rankings, filters by category, opens a wallet, and understands why it is tracked, watched, or ignored.

**Signal review**

The operator opens a detected trade and sees the wallet entry, detected executable price, price movement, spread, liquidity, score components, decision, and risks.

**Decision audit**

The operator opens the journal and sees what the system knew at decision time, which rule version was used, and whether the later outcome made the decision good or bad.

**Rule-change review**

The operator sees the old value, new value, evidence, sample size, expected improvement, and rollback status for every automatic rule change.

**End-of-day review**

Hermes sends one concise Telegram report containing the day's paper results, benchmark comparison, most important lesson, and any significant rule change.

---

## 7. System architecture

### 7.1 Recommended Railway topology

The exact topology must respect the existing deployment, but the production responsibilities should be separated logically as follows:

| Component | Responsibility | Process type |
|---|---|---|
| Existing Hermes API/operator | Orchestration, authenticated internal APIs, Telegram reporting, manual commands | Existing long-running Railway service |
| Polymarket monitor | Continuous tracked-wallet polling, WebSocket market data, signal detection | Long-running Railway worker service or a separate process in the existing service |
| Scheduled jobs | Leaderboard scans, wallet profiling batches, hourly PnL, reviews, daily and weekly reports | Railway cron services or the existing Hermes scheduler |
| Dashboard | Read-only operational interface and approved manual actions | Existing dashboard shell or Next.js service on Railway; Vercel-compatible but not required |
| PostgreSQL | Durable application state, audit history, PnL, rules, reports | Railway PostgreSQL or the existing production PostgreSQL |
| Optional queue | Retries and decoupled jobs if current Hermes already uses one or load requires it | Existing queue, or Redis added only after need is demonstrated |

Railway cron is suitable for fixed scheduled jobs that start, execute, and exit. Continuous trade detection must use a long-running worker because Railway cron has a minimum interval, execution may vary by a few minutes, and a new run is skipped when the previous run is still active.

### 7.2 Storage decision

- Use PostgreSQL in production.
- SQLite may be supported only for local development and tests.
- Do not use a production SQLite file inside an ephemeral Railway filesystem.
- If the existing Hermes installation already uses PostgreSQL, add namespaced migrations to the same database unless isolation or load testing justifies a separate database.
- Store all timestamps in UTC.
- Store money and prices as fixed precision decimal values, never floating-point binary values.

### 7.3 Application layers

**Adapters**

Normalize external API responses into internal models. All external APIs must be accessed through interfaces so endpoints can change without rewriting scoring logic.

**Domain services**

Wallet statistics, scoring, copyability, signal decisions, paper portfolio, benchmarks, outcome classification, and rule evaluation.

**Jobs**

Idempotent commands that can be retried safely and record run status.

**API**

Authenticated read APIs for the dashboard and restricted operator actions for rescans, reprocessing, and report generation.

**Dashboard**

A read-focused interface with data freshness, failure state, and source status visible on every page.

**Hermes operator layer**

Schedules jobs, summarizes results, sends Telegram messages, and provides natural-language operational commands. Hermes may explain evidence, but quantitative strategy changes must pass deterministic safety rules.

---

## 8. External data adapters

### 8.1 Polymarket API separation

The implementation must treat Polymarket's public services as separate adapters:

- Gamma API for market and event discovery, metadata, categories, status, and resolution context
- Data API for leaderboards, public wallet trades, positions, activity, and related wallet statistics
- CLOB public endpoints for order books, best bid and ask, spread, price history, and executable mark prices
- Public market WebSocket for near real-time order-book, price, trade-tick, and market-lifecycle events. Wallet identity detection remains a Data API polling responsibility.

Authenticated trading endpoints are out of scope and must not be implemented.

### 8.2 Leaderboard adapter

The adapter must:

- Request the leaderboard with `timePeriod=MONTH` and `orderBy=PNL`; do not rely on endpoint defaults.
- Retrieve 500 wallets using 10 pages of 50 records, matching the current endpoint maximum of 50 rows per request.
- Support category-specific scans for all official categories returned by the API. As verified on 6 July 2026, the leaderboard categories are `OVERALL`, `POLITICS`, `SPORTS`, `ESPORTS`, `CRYPTO`, `CULTURE`, `MENTIONS`, `WEATHER`, `ECONOMICS`, `TECH`, and `FINANCE`.
- Store the source rank, PnL, volume, display name, profile metadata, category, time period, and raw response.
- Deduplicate wallets across category scans.
- Record a complete scan run with expected count, actual count, errors, and duration.
- Fail the scan if the expected pagination is incomplete, unless a partial scan is explicitly marked and excluded from promotion or downgrade decisions.

### 8.3 Wallet-trade adapter

The adapter must:

- Fetch public trades by wallet address from the public Data API. Do not use the authenticated CLOB `/trades` endpoint, which is for the authenticated user.
- Paginate until the 30-day window is complete or the endpoint limit is reached.
- Handle BUY and SELL events.
- Use transaction hash and trade fields to construct a stable idempotency key.
- Save the original raw response for debugging.
- Maintain a cursor for each monitored wallet.
- Re-query a short overlap window on each poll to protect against delayed indexing, then deduplicate locally.
- Separate initial historical ingestion from incremental monitoring.

### 8.4 Market metadata adapter

The adapter must resolve and store:

- Market and condition identifiers
- Outcome token identifiers
- Question and event title
- Category and tags
- Open, closed, and resolved state
- Resolution time and actual resolution timestamp
- Winning outcome when resolved
- Market slug and event slug
- Any available fee or tick-size information required for paper execution

### 8.5 Market data adapter

The adapter must provide:

- Best bid
- Best ask
- Spread
- Last trade price
- Midpoint for display only
- Order-book depth near the proposed paper size
- Estimated executable price for a $5, $10, or $20 simulated order
- Market volume and liquidity indicators
- Data timestamp and freshness

For paper PnL, use an executable-side mark where possible. A long position should normally be marked at the best bid, not an optimistic midpoint.

### 8.6 WebSocket behavior

The continuous monitor should use the public market WebSocket for subscribed outcome assets when available.

Requirements:

- Reconnect with exponential backoff and jitter.
- Resubscribe after reconnect.
- Refresh subscriptions when tracked markets change.
- Persist the last event timestamp per asset.
- Detect stale streams and switch to REST fallback.
- Store market-resolved events and trigger outcome review.
- Never rely on WebSocket delivery alone for final resolution. Reconcile through REST.

### 8.7 Rate-limit behavior

- Use a shared rate limiter per external host.
- Keep request rates far below documented limits.
- Apply retry only to transient errors, using exponential backoff and jitter.
- Respect `Retry-After` when provided.
- Do not retry validation errors indefinitely.
- Cache market metadata and batch market-data requests where supported.
- Record throttling and delayed responses as data-quality events.

---

## 9. Job schedule and operational loop

All schedules are configurable through environment variables or the existing Hermes scheduler. Defaults are recommendations, not hard-coded business logic.

| Job | Default cadence | Notes |
|---|---:|---|
| Overall leaderboard scan | Daily | Pull top 500 monthly leaderboard wallets |
| Category leaderboard scan | Daily | Scan supported categories and merge wallet universe |
| Initial wallet history ingestion | Queue-based | Process newly discovered wallets with concurrency limits |
| Tracked-wallet monitoring | Every 60 to 120 seconds | Long-running worker, not Railway cron |
| Full tracked-wallet reconciliation | Every 15 minutes | Repairs missed or delayed events |
| Wallet profile refresh | Every 6 hours for tracked wallets; daily for watch/ignore | Recalculate 30-day metrics |
| Market snapshot updates | Event driven plus REST fallback | Persist on signal, hourly PnL, and important market events |
| Signal scoring | Immediately after trade ingestion | Must use fresh market data |
| Paper PnL update | Hourly | Update all open positions |
| 1-hour review | Scheduled from decision time | Applies to copy, watch, and skip decisions |
| 6-hour review | Scheduled from decision time | Same |
| 24-hour review | Scheduled from decision time | Same |
| Final outcome review | On resolution plus reconciliation | Required for all eligible decisions |
| Rule evaluation | Daily after report cut-off | Only if sample requirements are met |
| Daily report | Once daily | Local time defined by `REPORT_TIMEZONE` |
| Weekly report | Once weekly | Strategy and rule stability summary |
| Data reconciliation | Daily | Detect missing market data, duplicate events, and stale profiles |

Every job must be idempotent, have a unique run ID, use an advisory lock or equivalent concurrency guard, and record start time, finish time, status, counts, and errors.

---

## 10. Wallet-universe requirements

### 10.1 Wallet statuses

Each wallet has one operational status:

- `track`: continuously monitor for new trades
- `watch`: profile regularly but monitor at lower priority
- `ignore`: retain history, but do not monitor incrementally
- `insufficient_data`: not enough evidence for a reliable status
- `data_error`: profile is not trustworthy because ingestion is incomplete

### 10.2 Required 30-day metrics

For each wallet calculate:

- Gross and net realized PnL
- Mark-to-market PnL for relevant open positions
- Capital deployed
- ROI
- Trade count
- Resolved trade count
- Win rate on resolved positions
- Average and median trade size
- PnL per trade
- Positive-day ratio
- Profit concentration in top 1, top 3, and top 5 trades
- Category-level PnL, ROI, win rate, and sample size
- Average market liquidity
- Average spread at estimated copy time
- Average price movement between wallet trade and system detection
- Median detection delay
- Percentage of trades that remain executable at the configured size
- Entry timing relative to resolution
- Frequency and recency
- Drawdown estimate
- Blind-copy simulated performance
- Data completeness and sample-quality score

### 10.3 Category classification

Use official Polymarket categories when available. Store both the source category and normalized internal category.

A wallet may be:

- Globally copyable
- Copyable only in one or more categories
- Profitable but not copyable
- Too early to judge
- Not copyable because of liquidity, lateness, spread, concentration, or insufficient history

Category scoring must require a minimum sample and must shrink small-sample results toward the wallet's overall score rather than treating a few wins as a proven edge.

---

## 11. Wallet scoring specification

### 11.1 Score range

All component scores use a 0 to 100 range. Weights and thresholds are stored in the active rule set.

### 11.2 Initial component weights

The first rule version should use these starting weights, subject to testing and later controlled changes:

| Component | Weight |
|---|---:|
| ROI quality | 20% |
| Consistency and repeatability | 25% |
| Copyability | 30% |
| Category edge | 10% |
| Liquidity quality | 5% |
| Entry timing and detection delay | 5% |
| Resolved sample quality and frequency | 5% |

`walletGlobalScore = weighted component score - penalties`

Clamp the final score to 0 through 100.

### 11.3 ROI quality

ROI must not be evaluated alone. The ROI component should consider:

- Net PnL relative to capital deployed
- Realized versus unrealized contribution
- PnL after estimated copy spread and delay
- Recency weighting
- Sample sufficiency

Extreme ROI with very little capital or very few resolved trades must not receive a maximum score.

### 11.4 Consistency score

Consistency should combine:

- Positive-day ratio
- Stability across weeks
- Resolved win rate with sample adjustment
- Profit concentration
- Drawdown
- Number of independent markets
- Category stability

### 11.5 Copyability score

Copyability should combine:

- Detection delay
- Price movement before detection
- Average spread
- Available depth for the configured paper size
- Percentage of trades still executable
- Time remaining before resolution
- Trade frequency and monitoring feasibility
- Whether the wallet tends to enter before price discovery or after most movement has occurred

### 11.6 One-hit-wonder penalty

The system must explicitly penalize concentrated PnL.

Initial default bands:

| Share of 30-day profit from the top trade | Penalty |
|---|---:|
| Up to 25% | 0 |
| More than 25% to 40% | 5 to 10 |
| More than 40% to 60% | 10 to 25 |
| More than 60% | 25 to 40 |

Additional concentration penalties may apply when the top three trades account for most profit or when the profitable result comes from one old market with no recent confirmation.

The exact interpolation belongs in tested deterministic code and must be configurable in the rule set.

### 11.7 Initial status thresholds

- `track`: score 70 or above, sufficient data, and no hard copyability failure
- `watch`: score 50 to 69, or strong score with unresolved sample concerns
- `ignore`: below 50 or repeated hard copyability failures
- `insufficient_data`: fewer than the configured minimum resolved trades or incomplete history
- `data_error`: critical ingestion or market-mapping failure

No wallet may be promoted or downgraded from a partial scan or stale profile.

---

## 12. Trade detection and normalization

### 12.1 Detection

For tracked wallets, the monitor must detect new public trades and create one normalized `ObservedTrade` record per event.

### 12.2 Idempotency

A duplicate must not create a second signal or paper trade. Use an idempotency key based on available immutable fields, preferably:

```text
source + proxyWallet + transactionHash + assetId + side + price + size
```

When a transaction hash is missing, use a deterministic hash of the normalized event fields and timestamp.

### 12.3 BUY and SELL behavior

- A wallet BUY can create a copy, watch, or skip candidate.
- A wallet SELL should first be interpreted as reducing or closing an existing thesis.
- Version one must not open short paper positions merely because a source wallet sold.
- If the paper portfolio holds the same outcome for that wallet-market strategy, a qualifying SELL may close or reduce it according to exit rules.
- If there is no corresponding paper position, record the SELL for analysis but do not create a new short position.

### 12.4 Freshness gate

A signal must not be scored as copyable when required market data is stale. Initial maximum age:

- Order book and best bid/ask: 120 seconds
- Market metadata: 15 minutes for open-state checks, unless a resolution event occurred
- Wallet profile: 12 hours for tracked wallets

Stale data produces `watchlist` or `skip`, depending on severity, with an explicit data-quality reason.

---

## 13. Trade-scoring specification

### 13.1 Required inputs

- Wallet global score
- Wallet category score
- Wallet status and data quality
- Wallet's original entry price
- System detection time
- Current estimated executable entry price
- Absolute and percentage price movement since wallet entry
- Spread
- Order-book depth
- Liquidity
- Time to scheduled resolution
- Market state
- Category fit
- Copy latency
- Current portfolio exposure
- Active rule version
- Thesis clarity or market-context quality

### 13.2 Initial weights

| Component | Weight |
|---|---:|
| Wallet global quality | 25% |
| Category fit | 15% |
| Price movement and lateness | 15% |
| Executable liquidity and depth | 10% |
| Spread | 10% |
| Detection latency and wallet timing history | 10% |
| Time to resolution | 5% |
| Thesis and market-context clarity | 10% |

### 13.3 Hard gates

A trade cannot be `paper_copy` if any hard gate fails:

- Market is closed, resolved, paused, or cannot be mapped to a valid outcome asset
- Required source data is stale or missing
- Proposed position cannot be filled within the configured depth and slippage limit
- Spread exceeds the active maximum
- Price movement since source entry exceeds the active maximum
- Time to resolution is below the minimum, unless the rule set explicitly supports that market type
- Wallet is outside its proven category
- Wallet status is `ignore`, `insufficient_data`, or `data_error`
- Duplicate trade or duplicate active paper thesis
- Portfolio cash or exposure limits would be breached

### 13.4 Decision thresholds

Initial defaults:

- `paper_copy`: score 75 or above and all hard gates pass
- `watchlist`: score 55 to 74, or a potentially strong signal blocked by a recoverable condition
- `skip`: score below 55 or any non-recoverable hard gate failure

### 13.5 Decision explanation

Each decision must store:

- Final decision
- Total score
- Component scores
- Rule version
- Reasons in order of importance
- Risks
- Hard gates checked
- Market-data timestamp
- Wallet-profile timestamp
- Original wallet entry and detected executable entry
- Expected position size
- Portfolio-limit result

The stored explanation must be based on structured facts. Hermes may turn it into readable prose, but the underlying factors must remain queryable.

---

## 14. Paper portfolio and execution simulation

### 14.1 Paper bankroll

The system must simulate a finite bankroll configured by `PAPER_STARTING_BANKROLL`.

Requirements:

- Never allow negative available paper cash.
- Track cash, open-position cost, realized PnL, unrealized PnL, equity, and drawdown.
- Support resetting only through an explicit administrative action that creates a new portfolio version. Do not erase historical results.
- Record all ledger movements.

### 14.2 Confidence tiers

Initial position tiers:

- Score 75 to 84: $5
- Score 85 to 92: $10
- Score 93 to 100: $20

The actual size is the smallest of:

- Confidence tier
- Available paper cash
- Maximum per-position exposure
- Maximum wallet exposure
- Maximum category exposure
- Maximum correlated-event exposure
- Size supported by the order book within the slippage limit

### 14.3 Exposure controls

All are configurable and versioned:

- Maximum open positions
- Maximum percentage of equity in one position
- Maximum percentage by wallet
- Maximum percentage by category
- Maximum percentage by event
- Maximum daily new exposure
- Maximum number of copies from one wallet per day
- Cooldown after strategy drawdown or repeated data failures

### 14.4 Entry-price simulation

For a BUY:

1. Use the order book at detection time.
2. Walk asks to estimate the fill for the proposed dollar size.
3. Store weighted average simulated fill price.
4. Store slippage from best ask and from the source wallet's price.
5. Include applicable fees when determinable.
6. If full size cannot be filled within limits, reduce size if rules allow, otherwise watch or skip.

### 14.5 Position accounting

For a binary outcome token purchased at price `p` with paper cost `c`:

```text
shares = c / p
current_liquidation_value = shares * executable_exit_price
unrealized_pnl = current_liquidation_value - c - estimated_exit_fees
```

At resolution:

```text
settlement_value = shares * 1 if the outcome wins, otherwise 0
realized_pnl = settlement_value - c - entry_fees - exit_or_settlement_fees
```

Use decimal arithmetic and store the exact pricing source and timestamp.

### 14.6 Exit behavior

Version one supports:

- Final resolution
- A matching wallet SELL that qualifies as a position close or reduction
- Rule-defined safety exits in paper mode
- Administrative closure for data-repair testing, clearly labeled and excluded from strategy evaluation unless approved

Every exit must create a ledger entry and preserve the original decision.

---

## 15. Decision journal and outcome review

### 15.1 Journal coverage

Create a journal entry for every eligible observed trade, including copy, watchlist, and skip.

### 15.2 Review checkpoints

For each decision, capture:

- Price after 1 hour
- Price after 6 hours
- Price after 24 hours
- Best favorable movement before resolution
- Worst adverse movement before resolution
- Final winning outcome
- Hypothetical PnL using the decision-time executable price
- Actual paper PnL when copied
- Whether the decision was good or bad under the configured evaluation rule
- Lessons and candidate rule implications

### 15.3 Decision-quality labels

- `good_copy`: copied and profitable within evaluation policy
- `bad_copy`: copied and unprofitable
- `good_skip`: skipped trade would have lost or violated realistic execution constraints
- `missed_winner`: skipped trade would have been profitable under the same execution model
- `good_watch`: watch decision avoided a bad immediate entry or found a better later entry
- `missed_watch_entry`: a watch signal offered a later valid entry but was not acted on by the simulation
- `unjudgeable`: missing data, unresolved market, or insufficient executable data

Unjudgeable records must not be used as evidence for automatic rule changes.

---

## 16. Benchmarking requirements

### 16.1 Required strategy cohorts

1. Bot-filtered paper copies
2. Blind leaderboard copy
3. Watchlist cohort
4. Skip cohort

### 16.2 Fair baseline

Blind copying must use the same:

- Detection timestamp
- Executable pricing model
- Paper bankroll
- Position-size policy or a clearly documented fixed-size policy
- Fee assumptions
- Market availability
- Data-freshness requirements
- Maximum portfolio constraints

The baseline removes the bot's wallet and trade-quality filters, but must not assume impossible fills or unlimited money.

### 16.3 Metrics

- Net PnL
- ROI on capital deployed
- Win rate
- Average PnL per trade
- Profit factor
- Maximum drawdown
- Average and median holding time
- Exposure by wallet and category
- Missed winners
- Avoided losers
- Good skips
- Bad copies
- Late entries avoided
- Spread losses avoided
- Performance by rule version
- Performance by wallet and category

### 16.4 Strategy-value requirement

The dashboard and reports must clearly state whether the filtered strategy outperformed blind copy over:

- Today
- Last 7 days
- Last 30 days
- All available paper history

Do not claim an edge when the sample is below the configured minimum.

---

## 17. Self-improvement and rule management

### 17.1 Principle

Self-improvement is a bounded parameter-optimization process based on recorded outcomes. It is not permission for Hermes or an LLM to rewrite arbitrary trading logic in production.

### 17.2 Changeable parameters

Only whitelisted values may change automatically, for example:

- Minimum wallet score
- Minimum category score
- Maximum spread
- Minimum liquidity or order-book depth
- Maximum price movement from source entry
- Minimum time to resolution
- Component weights within approved bounds
- Wallet upgrade and downgrade thresholds
- Position-tier score thresholds
- Exposure limits within pre-approved paper ranges

### 17.3 Immutable parameters in version one

The automatic updater may not change:

- Paper-only mode
- External API host allowlist
- Database access controls
- Maximum $20 confidence tier without owner approval
- Code, schema, or deployment configuration
- Definition of resolved outcome
- Rule-history retention
- Minimum evidence requirements

### 17.4 Evidence requirements

Before changing a rule:

- Minimum 20 judged decisions overall since the last relevant change
- Minimum 10 judged decisions directly relevant to the parameter when applicable
- No critical data-quality incident in the evidence window
- Comparison against the previous rule version
- Expected improvement stated in a measurable metric
- Evidence excludes demo, administrative, and unjudgeable records

### 17.5 Change bounds

- Change at most one parameter family per daily evaluation.
- Change numeric thresholds by no more than 10% relative per version unless a safety rule becomes stricter.
- Keep all weights between documented minimum and maximum values.
- Preserve a normalized total weight of 100%.
- Create the new rule set before activating it.
- Activation must be transactional.
- Never edit an old rule set in place.

### 17.6 Rollback

Each automatic change must define a rollback test.

Rollback when the next evidence window shows a material deterioration in the target metric, excessive drawdown, or an unexpected increase in unjudgeable decisions.

### 17.7 Hermes role

Hermes may:

- Summarize evidence
- Explain the likely reason for a change
- Generate the daily learning narrative
- Propose a parameter change

A deterministic rule evaluator must:

- Validate sample size
- Validate data quality
- Check bounds
- Calculate before and after performance
- Activate or reject the proposal
- Create the immutable version record

---

## 18. Database requirements

The original models remain valid, with the following production refinements and additions.

### 18.1 Core entities

#### `leaderboard_scans`

- `id`
- `source`
- `category`
- `time_period`
- `order_by`
- `scanned_at`
- `expected_wallet_count`
- `actual_wallet_count`
- `lookback_days`
- `status`
- `is_partial`
- `duration_ms`
- `raw_summary_json`
- `error_json`
- `job_run_id`

#### `leaderboard_entries`

- `id`
- `leaderboard_scan_id`
- `wallet_address`
- `rank`
- `source_pnl`
- `source_volume`
- `user_name`
- `profile_image`
- `verified_badge`
- `raw_json`

Unique: scan plus wallet plus category.

#### `wallet_profiles`

- Original fields from the source prompt
- `data_quality_score`
- `profile_version`
- `status_reason_code`
- `blind_copy_pnl_30d`
- `max_drawdown_30d`
- `profit_concentration_top1`
- `profit_concentration_top3`
- `median_detection_delay_seconds`
- `executable_trade_ratio`
- `profile_window_start`
- `profile_window_end`

Unique: wallet address for current profile, with historical versions stored separately or in a profile-snapshot table.

#### `wallet_category_stats`

- `id`
- `wallet_address`
- `category`
- `trade_count`
- `resolved_trade_count`
- `pnl`
- `roi`
- `win_rate`
- `consistency_score`
- `copyability_score`
- `category_score`
- `sample_quality_score`
- `window_start`
- `window_end`
- `calculated_at`

#### `observed_trades`

- Original source-prompt fields
- `asset_id`
- `transaction_hash`
- `source_side`
- `idempotency_key`
- `detected_at`
- `detection_delay_seconds`
- `source_trade_timestamp`
- `ingestion_run_id`

Unique: `idempotency_key`.

#### `markets`

- `market_id`
- `condition_id`
- `event_id`
- `question`
- `event_title`
- `category`
- `source_category`
- `slug`
- `event_slug`
- `yes_asset_id`
- `no_asset_id`
- `scheduled_resolution_at`
- `resolved_at`
- `winning_outcome`
- `status`
- `metadata_updated_at`
- `raw_json`

#### `market_snapshots`

Use the source-prompt fields plus:

- `asset_id`
- `last_trade_price`
- `midpoint`
- `depth_5`
- `depth_10`
- `depth_20`
- `data_source`
- `source_timestamp`
- `is_stale`

Index: market plus collected time; asset plus collected time.

#### `decision_journal`

Use all source-prompt fields plus:

- `rule_set_id`
- `decision_reason_code`
- `hard_gates_json`
- `market_snapshot_id`
- `wallet_profile_version`
- `source_entry_price`
- `executable_entry_price`
- `price_move_absolute`
- `price_move_percent`
- `data_quality_score`
- `idempotency_key`

Unique: one decision per strategy and observed trade.

#### `paper_portfolios`

- `id`
- `name`
- `version`
- `starting_bankroll`
- `cash_balance`
- `status`
- `started_at`
- `ended_at`
- `created_at`

#### `paper_trades`

Use all source-prompt fields plus:

- `paper_portfolio_id`
- `asset_id`
- `shares`
- `entry_best_ask`
- `entry_slippage`
- `entry_fee`
- `exit_price`
- `exit_fee`
- `exit_reason`
- `rule_set_id`
- `benchmark_cohort`

#### `paper_ledger`

- `id`
- `paper_portfolio_id`
- `paper_trade_id`
- `entry_type`
- `amount`
- `balance_after`
- `created_at`
- `metadata_json`

#### `pnl_snapshots`

Use source-prompt fields plus:

- `paper_portfolio_id`
- `cash_balance`
- `open_cost`
- `unrealized_pnl`
- `realized_pnl`
- `equity`
- `drawdown`
- `price_source`
- `source_timestamp`

#### `outcome_reviews`

Use source-prompt fields plus:

- `review_checkpoint`: `1h`, `6h`, `24h`, `final`
- `hypothetical_pnl`
- `decision_quality_label`
- `market_snapshot_id`
- `data_quality_score`
- `eligible_for_learning`

#### `rule_sets`

Use source-prompt fields plus:

- `parent_rule_set_id`
- `status`: `draft`, `active`, `rolled_back`, `superseded`
- `activated_at`
- `deactivated_at`
- `checksum`

Only one active rule set per strategy.

#### `rule_changes`

Use source-prompt fields plus:

- `parameter_family`
- `sample_size`
- `target_metric`
- `baseline_value`
- `expected_value`
- `rollback_rule_json`
- `outcome_status`
- `evaluated_at`

#### `benchmark_trades`

- `id`
- `observed_trade_id`
- `cohort`
- `simulated_entry_price`
- `simulated_position_size`
- `final_pnl`
- `decision_quality_label`
- `created_at`

#### `daily_reports`

Use the source-prompt fields plus:

- `strategy_version`
- `blind_copy_pnl`
- `filtered_minus_blind_pnl`
- `max_drawdown`
- `data_health_json`
- `telegram_message_id`
- `delivery_error`

#### `job_runs`

- `id`
- `job_name`
- `trigger_type`
- `started_at`
- `finished_at`
- `status`
- `records_read`
- `records_written`
- `records_skipped`
- `retry_count`
- `lock_key`
- `error_json`
- `metadata_json`

#### `data_quality_events`

- `id`
- `severity`
- `source`
- `event_type`
- `entity_type`
- `entity_id`
- `detected_at`
- `resolved_at`
- `details_json`

#### `alerts`

- `id`
- `type`
- `severity`
- `dedupe_key`
- `message`
- `sent_at`
- `delivery_channel`
- `delivery_status`
- `metadata_json`

### 18.2 Database constraints

- Use foreign keys for all relational links.
- Use unique constraints for idempotency.
- Use check constraints for prices between 0 and 1 when applicable.
- Use decimal types for prices and money.
- Use JSON only for raw payloads and flexible explanations, not for fields needed in filters or metrics.
- Index wallet address, market ID, condition ID, asset ID, status, timestamps, and active rule set.
- Retain raw external payloads for a configurable period, with longer retention for records used in decisions.
- All strategy and rule history is append-only from the application perspective.

---

## 19. Internal API requirements

All routes must use existing Hermes authentication and authorization.

### 19.1 Read routes

```text
GET /api/polymarket/overview
GET /api/polymarket/wallets
GET /api/polymarket/wallets/:address
GET /api/polymarket/wallets/:address/trades
GET /api/polymarket/signals
GET /api/polymarket/signals/:id
GET /api/polymarket/paper-trades
GET /api/polymarket/paper-trades/:id
GET /api/polymarket/journal
GET /api/polymarket/performance
GET /api/polymarket/performance/benchmarks
GET /api/polymarket/rules
GET /api/polymarket/rules/:version
GET /api/polymarket/reports
GET /api/polymarket/reports/:date
GET /api/polymarket/health
GET /api/polymarket/job-runs
```

### 19.2 Restricted operator actions

```text
POST /api/polymarket/actions/scan-leaderboard
POST /api/polymarket/actions/profile-wallets
POST /api/polymarket/actions/reconcile-trades
POST /api/polymarket/actions/update-pnl
POST /api/polymarket/actions/review-outcomes
POST /api/polymarket/actions/evaluate-rules
POST /api/polymarket/actions/generate-report
POST /api/polymarket/actions/retry-job/:id
POST /api/polymarket/actions/rollback-rule/:version
```

Requirements:

- Operator actions require administrative permission.
- Use idempotency keys and concurrency locks.
- Return a job-run identifier, not a long blocking HTTP response.
- No route may accept private keys, wallet credentials, or trade-execution instructions.

---

## 20. Dashboard requirements

The dashboard should live inside the existing Hermes UI when feasible. If it is a separate Next.js app, it must share authentication or sit behind the same access control and remain deployable on Railway. It may remain Vercel-compatible.

### 20.1 Global UI requirements

- Clean, focused layout
- Desktop-first but usable on mobile
- Visible `Paper trading only` badge
- Visible active rule version
- Visible last-successful-data timestamps
- Data-stale and partial-scan banners
- No fake live numbers
- Filters reflected in the URL
- Tables support pagination, sorting, and CSV export where useful
- Every score can be opened to show its component calculation
- All money values identify paper versus source PnL

### 20.2 Overview

Show:

- Paper equity and total paper PnL
- Today's paper PnL
- Win rate with sample count
- Maximum drawdown
- Open positions
- Available paper cash
- Active tracked wallets
- Copy candidates today
- Filtered strategy versus blind-copy result
- Latest rule version and changes
- End-of-day report status
- Data-health status
- Paper PnL chart
- Short `What Hermes learned today` panel

### 20.3 Wallet rankings

Show:

- Top 500 scan status
- Wallet address and label
- Overall and category ranks
- ROI
- Consistency
- Copyability
- Category score
- One-hit-wonder penalty
- Data quality
- Best category
- Status
- Status reason
- Last profile time

Filters:

- Category
- Status
- Minimum score
- Minimum resolved trades
- Copyable only
- Exclude stale or incomplete data

### 20.4 Wallet profile

Show all source-prompt fields plus:

- Profit-concentration chart
- Category performance table
- Detection-delay distribution
- Executable-trade ratio
- Blind-copy simulation
- Wallet's paper-copy performance
- Recent upgrades and downgrades
- Data-completeness warnings

### 20.5 Trade signals

Show:

- Source wallet and category fit
- Market question and outcome
- Source entry price
- Detected executable price
- Current price
- Price movement
- Spread and depth
- Detection delay
- Time to resolution
- Decision
- Total score
- Top positive factors
- Top risk or skip factor
- Rule version

### 20.6 Paper trades

Show:

- Portfolio and cohort
- Simulated position size
- Entry price and source entry comparison
- Shares
- Current executable mark
- Unrealized and realized PnL
- Hourly history
- Status
- Entry reason
- Exit reason
- Linked wallet, market, signal, and rule version

### 20.7 Decision journal

Show:

- Copy, watch, or skip
- Full score breakdown
- Reasons and risks
- Data known at decision time
- 1-hour, 6-hour, 24-hour, and final review
- Decision-quality label
- Lesson
- Whether the record was eligible for rule learning

### 20.8 Performance

Show:

- Equity and PnL
- Drawdown
- Win rate
- Category performance
- Wallet performance
- Rule-version performance
- Filtered versus blind copy
- Missed winners
- Avoided losers
- Good skips
- Bad copies
- Late-entry and spread-loss avoidance

### 20.9 Rules

Show:

- Active rule version
- Full current thresholds and weights
- Version history
- Before and after values
- Evidence window and sample size
- Expected improvement
- Actual post-change result
- Rollback status
- Manual rollback action for an administrator

### 20.10 Reports

Show:

- Daily reports
- Weekly reports
- Delivery state
- Best and worst wallets
- Best and worst paper trades
- Important rule updates
- Data-health summary
- Tomorrow's watch items

### 20.11 Health and operations

Add an operational view for:

- External API status
- WebSocket status
- Last successful jobs
- Failed jobs
- Queue or worker lag
- Stale profiles
- Partial leaderboard scans
- Missing outcome reviews
- Telegram delivery failures

---

## 21. Hermes operator requirements

Hermes must be able to execute or call the following domain commands:

```text
scan leaderboard
profile wallet universe
monitor tracked wallets
reconcile recent trades
score pending signals
update paper pnl
review due outcomes
recalculate wallet rankings
evaluate rule changes
generate daily report
generate weekly report
summarize data health
```

### 21.1 Operator prompt behavior

Hermes instructions must state:

- Operate paper mode only.
- Never ask for wallet private keys.
- Never place or simulate a value not present in the database or adapter response.
- Use job APIs or command interfaces rather than editing the database directly.
- Report real errors and data gaps.
- Keep Telegram alerts minimal.
- Explain rule changes using stored evidence.
- Do not override deterministic rule-change safeguards.

### 21.2 Telegram messages

Minimum:

- One end-of-day report each day

Additional messages only for:

- Very high-confidence paper signal
- Major rule version change
- Material wallet upgrade or downgrade
- Drawdown warning
- Repeated data failure
- Worker or report failure requiring intervention

Use deduplication and quiet periods to avoid alert spam.

---

## 22. Reporting requirements

### 22.1 Daily report

Include:

- Paper PnL today
- Total paper PnL and equity
- Win rate and sample count
- Maximum drawdown
- Open positions
- Best and worst paper trades
- Best and worst wallets today
- Copies, watches, and skips
- Missed winners and avoided losers
- Filtered strategy versus blind copy
- Rule changes
- Top lesson
- Data-health summary
- What to watch tomorrow

### 22.2 Weekly report

Include:

- Weekly and total performance
- Benchmark comparison
- Drawdown and exposure
- Category and wallet contribution
- Rule-version performance
- Rule changes and rollbacks
- Data incidents
- Whether evidence quality is improving
- Progress toward autonomy gates

---

## 23. Configuration and environment variables

Names may be adapted to existing conventions.

```text
APP_ENV
TRADING_MODE=paper
DATABASE_URL
REPORT_TIMEZONE
DAILY_REPORT_TIME
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
POLYMARKET_GAMMA_BASE_URL
POLYMARKET_DATA_BASE_URL
POLYMARKET_CLOB_BASE_URL
POLYMARKET_MARKET_WS_URL
LEADERBOARD_WALLET_LIMIT=500
WALLET_LOOKBACK_DAYS=30
TRACKED_WALLET_POLL_SECONDS
MARKET_DATA_MAX_AGE_SECONDS
PAPER_STARTING_BANKROLL
PAPER_MAX_POSITION_USD=20
PAPER_MAX_OPEN_POSITIONS
PAPER_MAX_WALLET_EXPOSURE_PERCENT
PAPER_MAX_CATEGORY_EXPOSURE_PERCENT
PAPER_MAX_EVENT_EXPOSURE_PERCENT
RULE_UPDATE_ENABLED=true
DEMO_MODE=false
LOG_LEVEL
```

Requirements:

- Validate all variables at startup.
- Refuse production startup when `TRADING_MODE` is not `paper`.
- Redact sensitive variables.
- Store non-secret strategy parameters in versioned rule sets, not only environment variables.

---

## 24. Observability and failure handling

### 24.1 Structured logging

Every log entry for a job or decision should include relevant identifiers:

- Job run ID
- Wallet address
- Market or condition ID
- Observed trade ID
- Decision ID
- Paper trade ID
- Rule version
- External source

### 24.2 Health indicators

- API process health
- Worker heartbeat
- Last leaderboard scan
- Last tracked-wallet event
- WebSocket connected state
- Last hourly PnL run
- Last daily report
- Active rule set
- Database connectivity

### 24.3 Error policy

- Classify errors as transient, validation, data quality, or critical.
- Retry transient external failures with bounds.
- Do not retry invalid mapping or schema errors indefinitely.
- Store partial-run state.
- Exclude partial or stale data from scoring changes.
- Surface the real error in the operations UI.
- Send Telegram only when intervention is required or repeated attempts fail.

---

## 25. Security and privacy

- Reuse existing Hermes authentication.
- Restrict the application to the owner and approved operators.
- Use least-privilege database roles where supported.
- Dashboard reads should use a read-oriented service layer, not direct unrestricted database access.
- Administrative actions require explicit permission and audit entries.
- Do not expose full raw API payloads publicly.
- Sanitize wallet labels, market text, and external metadata before rendering.
- Apply content-security policy and standard web protections.
- Keep dependencies patched and lock versions.
- Run dependency and secret scans in CI.
- Scrub credentials from exceptions and reports.

---

## 26. Testing requirements

### 26.1 Unit tests

- ROI calculation
- Consistency score
- Copyability score
- Category score and sample shrinkage
- One-hit-wonder penalty
- Wallet global score
- Trade score
- Hard gates
- Confidence-tier sizing
- Portfolio exposure limits
- Decimal PnL calculations
- BUY and SELL handling
- Outcome classifications
- Benchmark calculations
- Rule-bound validation
- Rule versioning and rollback

### 26.2 Integration tests

- Leaderboard pagination to 500 wallets
- Wallet-trade pagination and cursor overlap
- API response normalization
- Idempotent observed-trade ingestion
- WebSocket reconnect and REST reconciliation
- Paper-trade creation
- Hourly PnL update
- 1-hour, 6-hour, 24-hour, and final review scheduling
- Daily report generation
- Telegram delivery adapter
- Job lock and retry behavior
- PostgreSQL migrations

### 26.3 Safety tests

- No private-key fields
- No signing libraries or order-placement calls
- Startup fails outside paper mode
- Unsupported outbound hosts are blocked
- Demo data is labeled
- Missing live data cannot silently become demo data
- No real order can be created through API or Hermes commands

### 26.4 End-to-end tests

Using fixed fixtures:

1. Import a leaderboard.
2. Profile wallets.
3. Detect a new trade.
4. Score it.
5. Create copy, watch, and skip examples.
6. Create a paper position.
7. Update PnL.
8. Resolve the market.
9. Review the decision.
10. Compare against the blind baseline.
11. Propose and activate a bounded rule change.
12. Render all dashboard pages and generate a daily report.

### 26.5 Test-data policy

- Test fixtures must be deterministic.
- Demo records must be clearly marked.
- Production metrics must exclude tests and demos.
- External API contract tests should run separately from the main unit suite.

---

## 27. Command requirements

Adapt package-manager syntax to the current repository. Required logical commands:

```text
run development environment
run database migrations
seed labeled demo data
scan leaderboard
profile wallets
monitor trades
reconcile trades
score trades
update paper pnl
review outcomes
evaluate rule changes
generate daily report
generate weekly report
run tests
run safety tests
```

Suggested npm-compatible aliases:

```text
npm run dev
npm run db:migrate
npm run seed
npm run scan:leaderboard
npm run scan:wallets
npm run monitor:trades
npm run reconcile:trades
npm run score:trades
npm run paper:update-pnl
npm run review:outcomes
npm run update:rules
npm run report:daily
npm run report:weekly
npm run test
npm run test:safety
```

---

## 28. Delivery phases

### Phase 0: Existing-system discovery

- Document current Hermes and Railway architecture.
- Confirm database, scheduler, Telegram, authentication, and deployment conventions.
- Produce an integration plan with no unnecessary infrastructure replacement.

**Exit:** approved integration map and local development setup.

### Phase 1: Safety and data foundation

- Add production schema and migrations.
- Build public Polymarket adapters.
- Add job-run and data-quality tracking.
- Add safety tests and paper-mode startup guard.

**Exit:** data can be fetched and stored without any trading capability.

### Phase 2: Leaderboard and wallet profiling

- Scan top 500.
- Ingest 30-day history.
- Calculate wallet and category metrics.
- Rank and assign statuses.

**Exit:** rankings are reproducible and explainable.

### Phase 3: Monitoring and signal decisions

- Monitor tracked wallets.
- Normalize and deduplicate new trades.
- Capture market snapshots.
- Score copy, watch, and skip decisions.
- Create decision journal.

**Exit:** every new tracked-wallet trade receives one auditable decision.

### Phase 4: Paper portfolio and reviews

- Add finite bankroll and exposure controls.
- Simulate executable fills.
- Update hourly PnL.
- Review at 1 hour, 6 hours, 24 hours, and final resolution.

**Exit:** complete paper lifecycle works with deterministic fixtures and live public data.

### Phase 5: Benchmarks and learning

- Implement blind-copy cohort.
- Classify good skips, missed winners, avoided losers, and bad copies.
- Add bounded rule evaluator, versioning, and rollback.

**Exit:** rule changes can be justified, activated, and rolled back safely.

### Phase 6: Dashboard and Hermes operations

- Build dashboard pages.
- Add internal APIs.
- Add Hermes commands and summaries.
- Add Telegram daily and weekly reports.
- Add operational health page.

**Exit:** owner can operate and audit the system without direct database access.

### Phase 7: Production hardening

- Load test top-500 profiling and tracked-wallet monitoring.
- Validate rate limiting and API failure behavior.
- Verify Railway process separation and schedules.
- Add backups, alerts, and deployment runbook.
- Run a 7-day burn-in before counting formal paper evidence.

**Exit:** stable paper-research production release.

---

## 29. Acceptance criteria

The release is accepted only when all criteria pass:

### Data

- A complete top-500 monthly leaderboard scan is stored with pagination evidence.
- At least one complete 30-day wallet profile is reproducible from stored source records.
- Category profiles and one-hit-wonder penalties are visible and tested.
- Incremental monitoring produces no duplicate decisions.

### Decisions

- Every detected tracked-wallet trade becomes one copy, watch, or skip journal entry.
- Decisions show score components, hard gates, timestamps, and rule version.
- Stale or incomplete data cannot produce a paper copy.

### Paper trading

- Paper trades respect the finite bankroll and all exposure limits.
- Simulated fills use executable order-book pricing.
- PnL is calculated with decimal arithmetic.
- Hourly and final PnL are stored.
- BUY and SELL behavior is tested.

### Learning

- 1-hour, 6-hour, 24-hour, and final reviews work.
- Good skips and missed winners are classified.
- Blind-copy benchmarking uses the same execution assumptions.
- A rule change cannot activate without sample and bounds checks.
- Rule history is immutable and rollback works.

### Dashboard and operations

- All required pages render correctly.
- The overview answers the three primary questions without opening another page.
- Data freshness and failures are visible.
- Hermes can run all required jobs through supported commands or APIs.
- Daily Telegram report is delivered and stored.

### Safety

- No private-key, signing, or order-placement path exists.
- Safety test suite passes.
- Production refuses to run outside paper mode.
- Real-data failures are never replaced with unlabeled demo data.

---

## 30. Autonomy evidence gates

These gates are tracked in the weekly report. They do not enable live trading automatically.

Minimum evidence before a separate live-execution proposal may even be considered:

- At least 30 completed days of paper operation
- Positive aggregate paper PnL over the evaluation period
- At least 100 judged paper-copy trades
- Clear outperformance of the blind-copy baseline using the same execution model
- No unresolved critical data-quality failures
- Stable job execution and reconciliation
- A drawdown profile within an owner-approved limit
- Rule changes that show stable or improved out-of-sample performance
- Manual review of the largest gains and losses

Passing these gates only authorizes creation of a new live-trading PRD. It does not authorize live trading.

---

## 31. Documentation deliverables

### README

Include:

- What the system does and does not do
- Existing Hermes integration
- Architecture diagram
- Local setup
- Environment variables
- Database migration
- Railway deployment and service commands
- Job schedules
- Adapter behavior
- Wallet and trade scoring
- Paper portfolio math
- Benchmark definitions
- Rule-change safeguards
- Dashboard guide
- Troubleshooting

### SAFETY.md

Include:

- Paper-only rationale
- Proof that execution is disabled
- Copy-trading risks
- Stale-data, spread, liquidity, latency, and concentration risks
- Why leaderboard profit alone is misleading
- Why private keys are prohibited
- What a separate live-trading review would require

### OPERATIONS.md

Include:

- Railway service map
- Health checks
- Cron schedule
- Worker restart behavior
- Manual job commands
- Retry and reconciliation procedures
- Data-backfill procedure
- Rule rollback
- Telegram failure recovery
- Backup and restore procedure

### DATA_DICTIONARY.md

Define all metrics, scores, decision labels, benchmark cohorts, timestamps, and PnL fields.

---

## 32. Open configuration decisions

These do not block the technical design, but must be set before production evidence begins:

1. Initial paper bankroll
2. Report timezone and daily report time
3. Maximum open positions and exposure percentages
4. Minimum resolved-trade sample for wallet promotion
5. Initial spread, liquidity, lateness, and time-to-resolution gates
6. Categories included in the first production release
7. Whether the dashboard is embedded in the current Hermes UI or deployed as a separate Railway service
8. Whether the existing scheduler is sufficient or separate Railway cron services are preferred
9. Drawdown limit for the autonomy evidence gate
10. Telegram chat and alert severity preferences

All choices must be stored in configuration or the active rule set, not scattered through code.

---

## 33. Final developer instruction

Implement this as an extension of the existing successful Hermes deployment on Railway. Inspect and reuse the current repository and infrastructure before creating new services. Build in phases, prove each phase with tests and stored data, and do not move to later automation while earlier data and scoring layers are unreliable.

At the end of each phase, report:

- Files and migrations added
- Commands run
- Tests passed and failed
- Live public API calls verified
- Data-quality or API blockers
- Railway changes required
- Environment variables required
- Manual setup remaining
- Safety checks performed
- What is working end to end
- What remains incomplete

The final result must be an auditable paper-trading research system that can demonstrate whether filtering wallet trades creates an edge before any real-money execution is considered.

---

## 34. Reference basis

This specification consolidates:

- The complete user-provided Hermes Polymarket Copy Trading Bot build-prompt text
- The user-provided YouTube transcript
- Official Polymarket documentation verified on 6 July 2026 for the public Data API, Gamma API, CLOB read APIs, trader leaderboard, user trades, market WebSocket, authentication boundaries, and rate limits
- Official Railway documentation verified on 6 July 2026 for persistent services, background workers, cron jobs, private networking, PostgreSQL, health checks, and deployment configuration

The implementation must still use adapter interfaces and contract tests because external APIs and platform behavior can change after this specification date.

---

## 35. Source requirement traceability

This matrix confirms that the complete source prompt and transcript are represented in the technical specification.

| Source requirement | Covered in this PRD |
|---|---|
| Paper-only safety; no keys, signing, spending, or real execution | Sections 4, 5, 25, 26, 29 |
| Pull leaderboard and scan top 500 wallets | Sections 8, 9, 10, 29 |
| Analyze 30 days and score ROI, consistency, copyability | Sections 10 and 11 |
| Penalize one-hit wonders and uncopyable wallets | Sections 10 and 11 |
| Rank globally and by category | Sections 10, 11, 20 |
| Detect new trades and score copy, watch, or skip | Sections 12 and 13 |
| Simulate $5, $10, and $20 paper positions | Section 14 |
| Use a finite realistic paper bankroll | Section 14 |
| Update PnL hourly and resolve outcomes | Sections 9, 14, 15 |
| Review at 1 hour, 6 hours, 24 hours, and final resolution | Sections 9 and 15 |
| Compare filtered strategy with blind leaderboard copying | Section 16 |
| Track missed winners, avoided losers, good skips, and bad copies | Sections 15, 16, 20, 22 |
| Automatic bounded rule updates with version history | Section 17 |
| End-of-day and weekly Hermes reports; minimal Telegram alerts | Sections 21 and 22 |
| Overview, rankings, wallet, signals, trades, journal, performance, rules, and reports pages | Section 20 |
| Original database models plus production additions | Section 18 |
| Local commands, tests, README, SAFETY.md, and implementation order | Sections 26, 27, 28, 31 |
| Existing Hermes deployment on Railway is the foundation | Sections 3, 7, 28, 33 |
| Vercel-compatible dashboard without forcing a second deployment platform | Sections 7 and 20 |
| Autonomy only after evidence; live trading remains a separate phase | Sections 5 and 30 |
