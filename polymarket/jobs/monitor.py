"""Continuous monitor loop body (PRD sec 9, 12, 13).

For each `track` wallet: poll trades since its cursor minus an overlap window,
dedupe into observed_trades using the PRD 12.2 idempotency key, compute detection
delay, then for each *new* observed trade: ensure market metadata (gamma),
capture a market_snapshot (clob book, is_stale from source timestamps), run trade
scoring + decision, and insert a decision_journal row.

SELL handling (PRD 12.3): no shorts. A SELL is recorded and journalled with a
position-close / analysis-only reason; actual position closing activates Wave 3.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import aiosqlite

from ..adapters.clob import ClobAdapter, estimate_fill
from ..adapters.dataapi import DataApiAdapter
from ..adapters.gamma import GammaAdapter
from ..adapters.models import Market, OrderBook, WalletTrade
from ..config import Config
from ..db import (
    json_dumps,
    json_or_none,
    micro_to_px,
    px_to_micro,
    usd_to_micro,
    utcnow_iso,
)
from ..domain import decisions as dec
from ..domain import trade_scoring as ts
from ..domain.categories import canonical_category
from ..domain.decisions import NullPortfolioView
from ..jobs.common import (
    load_active_rule_set,
    now_ts,
    observed_idempotency_key,
    seconds_to_resolution,
    upsert_market,
)
from ..jobs.runner import JobContext


MAX_SIGNAL_AGE_SECONDS = 900


async def run_monitor(
    ctx: JobContext,
    config: Config,
    dataapi: DataApiAdapter,
    gamma: GammaAdapter,
    clob: ClobAdapter,
    *,
    portfolio: dec.PortfolioView | None = None,
    on_paper_copy=None,
) -> dict:
    portfolio = portfolio or NullPortfolioView()
    rule_set_id, version, payload = await load_active_rule_set(ctx.conn)
    wallets = await _tracked_wallets(ctx.conn)
    if not wallets:
        return {"tracked": 0, "new_signals": 0, "decisions": 0}

    total_new = 0
    total_decisions = 0
    for wallet in wallets:
        cursor_ts, overlap = await _cursor(ctx.conn, wallet)
        since = (cursor_ts - overlap) if cursor_ts else 0
        trades = await dataapi.iter_user_trades(
            wallet, stop_predicate=lambda t: t.timestamp < since if since else False
        )
        ctx.read(len(trades))
        newest_ts = cursor_ts or 0
        # Cold-start guard: trades older than the max signal age are backlog
        # (ingest-to-promotion gap), not live detections — a copy decision on
        # an hours-old trade is meaningless (production p50 delay was 6.3h).
        # They advance the cursor but never become observed signals.
        signal_cutoff = now_ts() - MAX_SIGNAL_AGE_SECONDS
        for trade in sorted(trades, key=lambda t: t.timestamp):
            newest_ts = max(newest_ts, trade.timestamp)
            if trade.timestamp < signal_cutoff:
                ctx.skipped()
                continue
            observed_id = await _dedupe_observed(ctx, wallet, trade)
            if observed_id is None:
                ctx.skipped()
                continue
            total_new += 1
            ctx.written()
            made = await _process_signal(
                ctx, config, gamma, clob, payload, rule_set_id, version,
                wallet, trade, observed_id, portfolio, on_paper_copy=on_paper_copy,
            )
            if made:
                total_decisions += 1
        await _save_cursor(ctx.conn, wallet, newest_ts, config, overlap)

    await ctx.conn.commit()
    return {"tracked": len(wallets), "new_signals": total_new, "decisions": total_decisions}


async def _tracked_wallets(conn: aiosqlite.Connection) -> list[str]:
    cursor = await conn.execute(
        "SELECT wallet_address FROM wallet_profiles WHERE status = 'track'"
    )
    return [row[0] for row in await cursor.fetchall()]


async def _cursor(conn: aiosqlite.Connection, wallet: str) -> tuple[int, int]:
    cursor = await conn.execute(
        "SELECT last_trade_ts, overlap_seconds FROM monitor_cursors WHERE wallet_address = ?",
        (wallet,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, 120
    return int(row[0] or 0), int(row[1] or 120)


async def _save_cursor(
    conn: aiosqlite.Connection, wallet: str, newest_ts: int, config: Config, overlap: int
) -> None:
    await conn.execute(
        """
        INSERT INTO monitor_cursors (wallet_address, last_trade_ts, last_polled_at, overlap_seconds)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet_address) DO UPDATE SET
            last_trade_ts = MAX(monitor_cursors.last_trade_ts, excluded.last_trade_ts),
            last_polled_at = excluded.last_polled_at,
            overlap_seconds = excluded.overlap_seconds
        """,
        (wallet, newest_ts, utcnow_iso(), overlap),
    )


async def _dedupe_observed(
    ctx: JobContext, wallet: str, trade: WalletTrade
) -> int | None:
    """Insert an observed_trade if new; return its id, or None if duplicate."""
    key = observed_idempotency_key(
        source="data-api",
        wallet=wallet,
        transaction_hash=trade.transaction_hash,
        asset_id=trade.asset_id,
        side=trade.side,
        price=trade.price,
        size=trade.size,
        timestamp=trade.timestamp,
    )
    detected = now_ts()
    delay = max(0, detected - trade.timestamp)
    try:
        price_micro = px_to_micro(trade.price)
    except ValueError:
        price_micro = None
    cursor = await ctx.conn.execute(
        """
        INSERT OR IGNORE INTO observed_trades (
            source, wallet_address, condition_id, asset_id, transaction_hash,
            source_side, outcome, outcome_index, source_price, source_size,
            idempotency_key, detected_at, detection_delay_seconds,
            source_trade_timestamp, ingestion_run_id, title, slug, event_slug, raw_json
        ) VALUES ('data-api', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet,
            trade.condition_id or None,
            trade.asset_id or None,
            trade.transaction_hash or None,
            trade.side,
            trade.outcome or None,
            trade.outcome_index,
            price_micro,
            usd_to_micro(trade.size),
            key,
            utcnow_iso(),
            delay,
            _iso_from_ts(trade.timestamp),
            ctx.job_run_id,
            trade.title or None,
            trade.slug or None,
            trade.event_slug or None,
            json.dumps(trade.raw, separators=(",", ":")),
        ),
    )
    if not cursor.rowcount:
        return None
    return cursor.lastrowid


async def _process_signal(
    ctx: JobContext,
    config: Config,
    gamma: GammaAdapter,
    clob: ClobAdapter,
    payload: dict,
    rule_set_id: int,
    version: int,
    wallet: str,
    trade: WalletTrade,
    observed_id: int,
    portfolio: dec.PortfolioView,
    *,
    on_paper_copy=None,
) -> bool:
    market = await _ensure_market(ctx, gamma, trade)
    snapshot_id, book, is_stale, source_ts = await _capture_snapshot(ctx, clob, trade, market)

    tier_usd = _max_tier_usd(payload)
    fill_est = None
    if book is not None and book.asks:
        fill_est = estimate_fill(
            book,
            side="BUY",
            usd_amount_micro=usd_to_micro(tier_usd),
            slippage_limit=Decimal(str(payload["hard_gates"]["max_slippage"])),
        )

    inputs = await _build_inputs(
        ctx.conn, payload, wallet, trade, market, book, is_stale, fill_est
    )
    score = ts.score_trade(inputs)
    decision = dec.decide(inputs, score, portfolio, event_id=(market.event_id if market else ""))

    profile_ts = await _profile_timestamp(ctx.conn, wallet)
    explanation = dec.build_explanation(
        inputs, score, decision,
        rule_set_version=version,
        market_data_timestamp=source_ts,
        wallet_profile_timestamp=profile_ts,
    )
    journal_id = await _insert_journal(
        ctx, observed_id, wallet, market, snapshot_id, rule_set_id, version,
        inputs, score, decision, explanation,
    )
    if trade.side == "BUY":
        from .benchmarks_job import record_blind_benchmark
        await record_blind_benchmark(
            ctx, observed_trade_id=observed_id, side=trade.side, book=book, payload=payload,
        )
    if (
        on_paper_copy is not None
        and journal_id is not None
        and decision.decision == dec.DECISION_PAPER_COPY
    ):
        await on_paper_copy(
            ctx,
            decision_journal_id=journal_id,
            observed_trade_id=observed_id,
            wallet=wallet,
            market=market,
            asset_id=(trade.asset_id or None),
            outcome=(trade.outcome or ""),
            condition_id=(trade.condition_id or ""),
            target_usd=decision.expected_position_usd,
            rule_set_id=rule_set_id,
            snapshot_id=snapshot_id,
            book=book,
            payload=payload,
        )
    return True


async def _ensure_market(ctx: JobContext, gamma: GammaAdapter, trade: WalletTrade) -> Market | None:
    if not trade.condition_id:
        return None
    cursor = await ctx.conn.execute(
        "SELECT metadata_updated_at FROM markets WHERE condition_id = ?", (trade.condition_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        markets = await gamma.get_markets_by_condition_ids([trade.condition_id])
        if not markets:
            await ctx.add_data_quality_event(
                severity="warning", event_type="unmapped_market", source="gamma",
                entity_type="condition", entity_id=trade.condition_id,
            )
            return None
        for m in markets:
            await upsert_market(ctx.conn, m)
        return markets[0]
    # reuse stored market
    return await _load_market(ctx.conn, trade.condition_id)


async def _load_market(conn: aiosqlite.Connection, condition_id: str) -> Market | None:
    cursor = await conn.execute(
        "SELECT raw_json, category FROM markets WHERE condition_id = ?", (condition_id,)
    )
    row = await cursor.fetchone()
    if row is None or not row[0]:
        return None
    from ..adapters.gamma import parse_market

    market = parse_market(json.loads(row[0]))
    category = canonical_category(row[1]) or canonical_category(market.category)
    if category != market.category:
        return replace(market, category=category)
    return market


async def _capture_snapshot(
    ctx: JobContext, clob: ClobAdapter, trade: WalletTrade, market: Market | None
) -> tuple[int | None, OrderBook | None, bool, str | None]:
    if not trade.asset_id:
        return None, None, True, None
    try:
        book = await clob.get_book(trade.asset_id)
    except Exception:  # noqa: BLE001
        await ctx.add_data_quality_event(
            severity="warning", event_type="book_fetch_failed", source="clob",
            entity_type="asset", entity_id=trade.asset_id,
        )
        return None, None, True, None

    source_ts = _book_source_iso(book)
    is_stale = _is_stale(book)
    best_bid = book.best_bid.price if book.best_bid else None
    best_ask = book.best_ask.price if book.best_ask else None
    spread = book.spread
    market_id = market.market_id if market else None
    cursor = await ctx.conn.execute(
        """
        INSERT INTO market_snapshots (
            market_id, asset_id, best_bid, best_ask, spread, midpoint,
            data_source, source_timestamp, collected_at, is_stale, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'clob', ?, ?, ?, ?)
        """,
        (
            market_id,
            trade.asset_id,
            px_to_micro(best_bid) if best_bid is not None else None,
            px_to_micro(best_ask) if best_ask is not None else None,
            px_to_micro(spread) if spread is not None and spread >= 0 else None,
            None,
            source_ts,
            utcnow_iso(),
            1 if is_stale else 0,
            json_or_none(book.raw),
        ),
    )
    if is_stale:
        await ctx.add_data_quality_event(
            severity="warning", event_type="stale_book", source="clob",
            entity_type="asset", entity_id=trade.asset_id, details={"source_ts": source_ts},
        )
    return cursor.lastrowid, book, is_stale, source_ts


def _book_source_iso(book: OrderBook) -> str | None:
    if not book.timestamp:
        return None
    try:
        ts_val = int(book.timestamp)
    except (ValueError, TypeError):
        return book.timestamp
    # CLOB timestamps are epoch millis.
    return _iso_from_ts(ts_val // 1000 if ts_val > 10_000_000_000 else ts_val)


def _is_stale(book: OrderBook) -> bool:
    if not book.timestamp:
        return not (book.bids or book.asks)
    try:
        ts_val = int(book.timestamp)
    except (ValueError, TypeError):
        return False
    ts_secs = ts_val // 1000 if ts_val > 10_000_000_000 else ts_val
    return (now_ts() - ts_secs) > 120


async def _build_inputs(
    conn, payload, wallet, trade, market, book, is_stale, fill_est
) -> ts.TradeScoreInputs:
    seconds_ttr = (
        seconds_to_resolution(market.end_date, now_ts=now_ts()) if market else None
    )
    category = canonical_category(market.category if market else "")
    proven, cat_score, w_status, w_score, w_dq, profile_age = await _load_wallet_facts(
        conn, wallet, category
    )
    market_facts = ts.MarketFacts(
        condition_id=trade.condition_id or "",
        market_id=(market.market_id if market else ""),
        category=category,
        closed=(market.closed if market else True),
        resolved=(market.resolved if market else False),
        paused=False,
        seconds_to_resolution=seconds_ttr,
        has_valid_outcome_asset=bool(trade.asset_id),
    )
    book_facts = ts.BookFacts(
        best_bid=(book.best_bid.price if book and book.best_bid else None),
        best_ask=(book.best_ask.price if book and book.best_ask else None),
        spread=(book.spread if book else None),
        book_age_seconds=_book_age_seconds(book),
        is_stale=is_stale,
    )
    if fill_est is not None:
        fill_facts = ts.FillFacts(
            filled_usd=fill_est.filled_usd,
            target_usd=_max_tier_usd(payload),
            avg_price=fill_est.avg_price,
            fully_filled=fill_est.fully_filled,
            stopped_reason=fill_est.stopped_reason,
        )
    else:
        fill_facts = ts.FillFacts(Decimal(0), _max_tier_usd(payload), None, False, "empty_book")

    wallet_facts = ts.WalletFacts(
        wallet_address=wallet,
        status=w_status,
        global_score=w_score,
        data_quality_score=w_dq,
        profile_age_seconds=profile_age,
        median_detection_delay_seconds=None,
        proven_categories=proven,
    )
    observed_facts = ts.ObservedFacts(
        side=trade.side,
        source_price=trade.price,
        detection_delay_seconds=max(0, now_ts() - trade.timestamp),
        is_duplicate=False,
        category=category,
        outcome=trade.outcome or "",
    )
    return ts.TradeScoreInputs(
        wallet=wallet_facts,
        category_stat_score=cat_score,
        observed=observed_facts,
        market=market_facts,
        book=book_facts,
        fill=fill_facts,
        payload=payload,
    )


async def _load_wallet_facts(
    conn: aiosqlite.Connection, wallet: str, category: str
) -> tuple[tuple[str, ...], Decimal | None, str, Decimal, Decimal, int | None]:
    """Return (proven_categories, category_score, status, global_score, dq, profile_age)."""
    cursor = await conn.execute(
        """
        SELECT status, global_score, data_quality_score, calculated_at
          FROM wallet_profiles WHERE wallet_address = ?
        """,
        (wallet,),
    )
    row = await cursor.fetchone()
    if row is None:
        return (), None, "insufficient_data", Decimal(0), Decimal(0), None
    status = row[0]
    gscore = Decimal(row[1] or 0)
    dq = Decimal(row[2] or 0)
    profile_age = None
    if row[3]:
        try:
            profile_age = max(0, now_ts() - int(_parse_iso_ts(row[3])))
        except ValueError:
            profile_age = None
    cur2 = await conn.execute(
        """
        SELECT category, category_score FROM wallet_category_stats
         WHERE wallet_address = ?
           AND category IS NOT NULL
           AND TRIM(category) != ''
           AND UPPER(TRIM(category)) != 'UNKNOWN'
        """,
        (wallet,),
    )
    proven: list[str] = []
    cat_score = None
    for c in await cur2.fetchall():
        proven_cat = canonical_category(c[0])
        if not proven_cat:
            continue
        proven.append(proven_cat)
        if category and proven_cat == category:
            cat_score = Decimal(c[1] or 0)
    return tuple(proven), cat_score, status, gscore, dq, profile_age


def _parse_iso_ts(value: str) -> float:
    from datetime import datetime
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def _book_age_seconds(book: OrderBook | None) -> int | None:
    if book is None or not book.timestamp:
        return None
    try:
        ts_val = int(book.timestamp)
    except (ValueError, TypeError):
        return None
    ts_secs = ts_val // 1000 if ts_val > 10_000_000_000 else ts_val
    return max(0, now_ts() - ts_secs)


def _max_tier_usd(payload: dict) -> Decimal:
    tiers = payload["confidence_tiers"]
    return max(Decimal(str(t["size_usd"])) for t in tiers)


async def _profile_timestamp(conn: aiosqlite.Connection, wallet: str) -> str | None:
    cursor = await conn.execute(
        "SELECT calculated_at FROM wallet_profiles WHERE wallet_address = ?", (wallet,)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _insert_journal(
    ctx: JobContext,
    observed_id: int,
    wallet: str,
    market: Market | None,
    snapshot_id: int | None,
    rule_set_id: int,
    version: int,
    inputs: ts.TradeScoreInputs,
    score: ts.TradeScore,
    decision: dec.Decision,
    explanation: dict,
) -> int | None:
    now = utcnow_iso()
    exec_price = score.executable_entry_price
    await ctx.conn.execute(
        """
        INSERT OR IGNORE INTO decision_journal (
            strategy, observed_trade_id, wallet_address, market_id, rule_set_id,
            decision, total_score, component_scores_json, decision_reason_code,
            reasons_json, risks_json, hard_gates_json, market_snapshot_id,
            wallet_profile_version, source_entry_price, executable_entry_price,
            price_move_absolute, price_move_percent, expected_position_usd,
            portfolio_limit_result, data_quality_score, market_data_timestamp,
            wallet_profile_timestamp, idempotency_key, created_at
        ) VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_id,
            wallet,
            market.market_id if market else None,
            rule_set_id,
            decision.decision,
            int(score.total_score),
            json_dumps(explanation["component_scores"]),
            decision.decision_reason_code,
            json_dumps(list(decision.reasons)),
            json_dumps(list(decision.risks)),
            json_dumps(explanation["hard_gates"]),
            snapshot_id,
            version,
            px_to_micro(inputs.observed.source_price) if inputs.observed.source_price else None,
            px_to_micro(exec_price) if exec_price is not None else None,
            usd_to_micro(score.price_move_absolute),
            usd_to_micro(score.price_move_percent),
            usd_to_micro(decision.expected_position_usd),
            explanation["portfolio_limit_result"],
            int(inputs.wallet.data_quality_score),
            explanation["market_data_timestamp"],
            explanation["wallet_profile_timestamp"],
            f"decision:{observed_id}",
            now,
        ),
    )
    cursor = await ctx.conn.execute(
        "SELECT id FROM decision_journal WHERE strategy = 'default' AND observed_trade_id = ?",
        (observed_id,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else None


def _iso_from_ts(ts_val: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
