"""Market resolution + category backfill sweep.

Fixes the pipeline dead-end found in production 2026-07-07: gamma's /markets
excludes closed markets unless closed=true, so resolved history markets were
never stored, no trade could ever count as resolved, and no wallet could be
promoted. This job:

1. backfills markets referenced by wallet_trades but missing from ``markets``;
2. re-fetches non-resolved markets whose end date has passed and upserts the
   resolution (status/winning_outcome/resolved_at);
3. fills empty categories from the event's gamma tag labels (first tag,
   normalized to the leaderboard category names);
4. prunes wallet_trades older than the lookback window (+grace) — bulk history
   is re-fetchable public data and unbounded growth filled the volume once.
"""

from __future__ import annotations

import aiosqlite

from ..adapters.gamma import GammaAdapter
from ..config import Config
from ..db import utcnow_iso
from ..jobs.common import now_ts, upsert_market
from ..jobs.runner import JobContext

# First gamma tag label -> leaderboard category name (PRD 8.2). Unmapped labels
# keep the raw label in source_category with category OTHER.
TAG_CATEGORIES = {
    "politics": "POLITICS",
    "sports": "SPORTS",
    "esports": "ESPORTS",
    "crypto": "CRYPTO",
    "culture": "CULTURE",
    "pop culture": "CULTURE",
    "mentions": "MENTIONS",
    "weather": "WEATHER",
    "economics": "ECONOMICS",
    "economy": "ECONOMICS",
    "tech": "TECH",
    "science": "TECH",
    "business": "FINANCE",
    "finance": "FINANCE",
}

MISSING_BATCH = 200
RESOLVE_BATCH = 400
CATEGORY_BATCH = 200
PRUNE_GRACE_DAYS = 15


def normalize_category(labels: list[str]) -> tuple[str, str]:
    """(category, source_category) from gamma tag labels; first label wins."""
    if not labels:
        return "", ""
    source = labels[0]
    for label in labels:
        mapped = TAG_CATEGORIES.get(label.strip().lower())
        if mapped:
            return mapped, source
    return "OTHER", source


async def run_resolve_markets(
    ctx: JobContext, config: Config, gamma: GammaAdapter, *, budget_seconds: int = 600
) -> dict:
    """Sweeps in batches until done or the time budget is spent (a cold start
    has tens of thousands of missing/unresolved markets; one batch per run
    would take weeks)."""
    import time as _time

    # Per-phase budget slices: serial phases starved categories in production
    # (backfill alone consumed the whole budget for hours; categories stayed
    # 0% and the wallet_category gate failed on every signal).
    start = _time.monotonic()
    cat_deadline = start + budget_seconds * 0.25
    backfill_deadline = start + budget_seconds * 0.65
    deadline = start + budget_seconds
    backfilled = resolved = categorized = 0
    while _time.monotonic() < cat_deadline:
        n = await _fill_categories(ctx, gamma)
        categorized += n
        await ctx.conn.commit()
        if n == 0:
            break
    while _time.monotonic() < backfill_deadline:
        n = await _backfill_missing(ctx, gamma)
        backfilled += n
        await ctx.conn.commit()
        if n == 0:
            break
    seen_due: set[str] = set()
    while _time.monotonic() < deadline:
        due, newly_resolved = await _resolve_due(ctx, gamma)
        resolved += newly_resolved
        await ctx.conn.commit()
        if not due or set(due) <= seen_due:
            # nothing left, or only perpetually-open markets we already tried
            break
        seen_due.update(due)
    # Final category pass: catch markets backfilled/resolved in THIS run.
    while _time.monotonic() < deadline:
        n = await _fill_categories(ctx, gamma)
        categorized += n
        await ctx.conn.commit()
        if n == 0:
            break
    pruned = await _prune_history(ctx, config)
    await ctx.conn.commit()
    return {
        "markets_backfilled": backfilled,
        "markets_resolved": resolved,
        "categories_filled": categorized,
        "trades_pruned": pruned,
    }


async def _backfill_missing(ctx: JobContext, gamma: GammaAdapter) -> int:
    cur = await ctx.conn.execute(
        """
        SELECT DISTINCT wt.condition_id FROM wallet_trades wt
        WHERE wt.condition_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM markets m WHERE m.condition_id = wt.condition_id)
        LIMIT ?
        """,
        (MISSING_BATCH,),
    )
    missing = [r[0] for r in await cur.fetchall()]
    if not missing:
        return 0
    markets = await gamma.get_markets_by_condition_ids(missing)
    for market in markets:
        await upsert_market(ctx.conn, market)
        ctx.written()
    unmapped = set(missing) - {m.condition_id for m in markets}
    if unmapped:
        # Genuinely absent from gamma (delisted); park a stub so the sweep does
        # not refetch the same ids forever. Trades on them stay unresolvable.
        for condition_id in unmapped:
            await ctx.conn.execute(
                """
                INSERT OR IGNORE INTO markets (market_id, condition_id, question, status, metadata_updated_at)
                VALUES (?, ?, '', 'unmapped', ?)
                """,
                (condition_id, condition_id, utcnow_iso()),
            )
        await ctx.add_data_quality_event(
            severity="warning", event_type="markets_unmapped_in_gamma", source="gamma",
            details={"count": len(unmapped), "sample": sorted(unmapped)[:5]},
        )
    return len(markets)


async def _resolve_due(ctx: JobContext, gamma: GammaAdapter) -> tuple[list[str], int]:
    """Returns (due_condition_ids, newly_resolved); caller loops until the due
    set stops changing (still-open markets stay due until a later run)."""
    cur = await ctx.conn.execute(
        """
        SELECT condition_id FROM markets
        WHERE status NOT IN ('resolved', 'unmapped')
          AND condition_id IS NOT NULL AND condition_id != ''
          AND (scheduled_resolution_at IS NULL OR scheduled_resolution_at <= ?)
        ORDER BY metadata_updated_at LIMIT ?
        """,
        (utcnow_iso(), RESOLVE_BATCH),
    )
    due = [r[0] for r in await cur.fetchall()]
    if not due:
        return [], 0
    resolved = 0
    markets = await gamma.get_markets_by_condition_ids(due)
    for market in markets:
        await upsert_market(ctx.conn, market)
        if market.resolved:
            resolved += 1
    # Touch the rest so the sweep rotates instead of rescanning the same batch.
    refreshed = {m.condition_id for m in markets}
    stale = [c for c in due if c not in refreshed]
    if stale:
        now = utcnow_iso()
        await ctx.conn.executemany(
            "UPDATE markets SET metadata_updated_at = ? WHERE condition_id = ?",
            [(now, c) for c in stale],
        )
    return due, resolved


async def _fill_categories(ctx: JobContext, gamma: GammaAdapter) -> int:
    cur = await ctx.conn.execute(
        """
        SELECT DISTINCT event_id FROM markets
        WHERE (category IS NULL OR category = '')
          AND event_id IS NOT NULL AND event_id != ''
          AND status != 'unmapped'
        LIMIT ?
        """,
        (CATEGORY_BATCH,),
    )
    event_ids = [r[0] for r in await cur.fetchall()]
    if not event_ids:
        return 0
    tags = await gamma.get_event_tags(event_ids)
    filled = 0
    for event_id in event_ids:
        category, source = normalize_category(tags.get(event_id, []))
        if not category:
            # No tags for this event: mark OTHER so the sweep moves on.
            category, source = "OTHER", ""
        cur = await ctx.conn.execute(
            "UPDATE markets SET category = ?, source_category = ? "
            "WHERE event_id = ? AND (category IS NULL OR category = '')",
            (category, source, event_id),
        )
        filled += cur.rowcount or 0
    # Propagate onto stored history rows so wallet category stats see it.
    await ctx.conn.execute(
        """
        UPDATE wallet_trades SET category = (
            SELECT m.category FROM markets m WHERE m.condition_id = wallet_trades.condition_id
        )
        WHERE (category IS NULL OR category = '')
          AND condition_id IN (SELECT condition_id FROM markets WHERE category != '')
        """
    )
    return filled


async def _prune_history(ctx: JobContext, config: Config) -> int:
    cutoff = now_ts() - (config.wallet_lookback_days + PRUNE_GRACE_DAYS) * 86400
    cur = await ctx.conn.execute("DELETE FROM wallet_trades WHERE ts < ?", (cutoff,))
    return cur.rowcount or 0
