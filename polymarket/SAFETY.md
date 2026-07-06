# Safety

**This system is paper-only by construction, not by convention.**

## Why paper-only

This is a *research* system: it measures whether a disciplined, filtered
copy-trading strategy would beat blind copying on Polymarket, and whether an
automated learner can improve the rules without human sign-off. None of that
requires — or is allowed to touch — real funds. Live trading is a separate,
unapproved project phase with its own review.

## Proof that execution is disabled

Three independent guarantees, all release-blocking:

1. **No signing dependencies.** The tree contains no `py_clob_client`,
   `eth_account`, `web3`, `private_key`/`PRIVATE_KEY`, `signature`, or
   `signTypedData`. There is no code path that can construct, sign, or submit an
   order. `tests/test_safety.py` greps the whole `polymarket/` tree for these
   forbidden imports/strings and fails the build if any appear.
2. **Host allowlist.** `http.py` permits only the three read-only Polymarket
   hosts (gamma, data-api, clob). Any other host raises `DisallowedHostError`
   and records a `data_quality_event`. `api.telegram.org` is not in the list
   (no Telegram delivery from the worker). The static test asserts the allowlist
   contents.
3. **Config guard.** `config.py` refuses to start unless `TRADING_MODE == "paper"`
   (unit-tested). No route or MCP tool accepts private keys, wallet credentials,
   or trade-execution instructions — the static test asserts no route/schema
   column mentions keys, and the MCP action allowlist is fixed to the read/job
   action names.

The MCP action tool (`poly_run_job`) only accepts a fixed allowlist of
deterministic job names; `poly_rollback_rule` reuses the same bounded rollback
path the evaluator uses. No tool can place a trade.

## Copy-trading risks (why this is research, not advice)

- **Detection + execution lag.** You see a wallet's fill after it happened; by
  the time you could act, the price has usually moved. The system measures this
  as detection delay and price-move lateness and gates on it — a live copier
  faces the same lag with real slippage.
- **Adverse selection.** The trades easiest to copy (liquid, slow-moving) are
  often the least edgy; the profitable ones fill fast and move away.
- **Survivorship + one-hit wonders.** A leaderboard is survivorship-biased. A
  wallet can top it on a single lucky market. The one-hit-wonder penalty
  discounts profit concentrated in one or a few markets, but it cannot fully
  correct for a small, biased sample.
- **Regime change.** Past ROI does not predict future ROI, especially across
  event categories and market regimes.

## Why leaderboard PnL alone is misleading

Raw leaderboard PnL rewards size and luck, not repeatable, copyable edge. A
wallet with huge PnL from one illiquid market you could never have entered at
their price is worthless to a copier. That is why:

- The default wallet ranking is **copied paper-PnL contribution**, not the
  wallet's own PnL.
- Every strategy claim is measured against a **blind cohort** (fixed $10, no
  filters, identical fill model) and the system **refuses to claim an edge**
  below the minimum sample, surfacing an explicit insufficient-sample caveat.

## What a live-trading review would require (out of scope here)

Before any live phase could even be discussed: a signed-order execution layer
with its own isolated key handling and hardware/2FA controls; realistic fee and
slippage modelling validated against live fills; position and loss limits
enforced at the venue; a kill switch; a sustained paper track record beating
blind copy on a statistically adequate sample across regimes; and an independent
security + risk review. None of that exists in this repo, by design.
