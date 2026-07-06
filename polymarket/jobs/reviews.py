"""Checkpoint reviews (PRD sec 15.2/15.3), scheduler pass every 5 min.

Finds decision_journal rows due for 1h/6h/24h checkpoints (measured from
created_at) and resolved markets needing a 'final' review. Captures the price at
checkpoint from a fresh clob book (falling back to the latest stored snapshot),
computes hypothetical PnL with the SAME execution model as the strategy, assigns
a label (domain/outcomes.py) and writes one outcome_reviews row per checkpoint.

Unjudgeable reviews are excluded from learning (eligible_for_learning=0).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..adapters.clob import ClobAdapter
from ..db import micro_to_px, micro_to_usd, px_to_micro, usd_to_micro, utcnow_iso
from ..domain import outcomes
from .common import now_ts
from .runner import JobContext

ZERO = Decimal(0)

CHECKPOINTS = [("1h", 3600), ("6h", 21600), ("24h", 86400)]


async def run_reviews(ctx: JobContext, clob: ClobAdapter) -> dict:
    interim = await _interim_reviews(ctx, clob)
    final = await _final_reviews(ctx)
    return {"interim_reviews": interim, "final_reviews": final}


async def _interim_reviews(ctx: JobContext, clob: ClobAdapter) -> int:
    """Create due 1h/6h/24h reviews for decisions past each checkpoint age."""
    cur = await ctx.conn.execute(
        """
        SELECT dj.id, dj.decision, dj.executable_entry_price, dj.expected_position_usd,
               dj.created_at, ot.asset_id, dj.market_snapshot_id
          FROM decision_journal dj
          JOIN observed_trades ot ON ot.id = dj.observed_trade_id
         WHERE dj.is_demo = 0
        """,
    )
    rows = await cur.fetchall()
    now = now_ts()
    created = 0
    for dj_id, decision, exec_px_micro, size_micro, created_at, asset_id, snap_id in rows:
        age = now - _iso_to_ts(created_at)
        for name, threshold in CHECKPOINTS:
            if age < threshold:
                continue
            if await _review_exists(ctx, dj_id, name):
                continue
            price = await _checkpoint_price(ctx, clob, asset_id, snap_id)
            exec_px = micro_to_px(int(exec_px_micro)) if exec_px_micro is not None else None
            size = micro_to_usd(int(size_micro or 0)) if size_micro else ZERO
            label, eligible = outcomes.label_checkpoint(
                decision=decision,
                executable_entry_price=exec_px,
                checkpoint_price=price,
                hypo_size_usd=(size if size > 0 else Decimal("10")),
            )
            hypo = None
            if price is not None and exec_px is not None and exec_px > 0:
                shares = (size if size > 0 else Decimal("10")) / exec_px
                hypo = shares * price - (size if size > 0 else Decimal("10"))
            await _insert_review(
                ctx, dj_id, name, price, hypo, None, label, eligible, snap_id,
            )
            created += 1
    return created


async def _final_reviews(ctx: JobContext) -> int:
    """Create 'final' reviews for decisions whose market resolved."""
    cur = await ctx.conn.execute(
        """
        SELECT dj.id, dj.decision, dj.executable_entry_price, dj.expected_position_usd,
               ot.outcome, m.winning_outcome, pt.id, pt.realized_pnl, pt.is_admin
          FROM decision_journal dj
          JOIN observed_trades ot ON ot.id = dj.observed_trade_id
          JOIN markets m ON m.market_id = dj.market_id
          LEFT JOIN paper_trades pt ON pt.decision_journal_id = dj.id
         WHERE dj.is_demo = 0 AND m.winning_outcome IS NOT NULL
        """,
    )
    rows = await cur.fetchall()
    created = 0
    for dj_id, decision, exec_px_micro, size_micro, outcome, winner, pt_id, realized_micro, is_admin in rows:
        if await _review_exists(ctx, dj_id, "final"):
            continue
        exec_px = micro_to_px(int(exec_px_micro)) if exec_px_micro is not None else None
        size = micro_to_usd(int(size_micro or 0)) if size_micro else ZERO
        won = (outcome or "") == (winner or "")
        actual = None
        if pt_id is not None and realized_micro is not None and not is_admin:
            actual = micro_to_usd(int(realized_micro))
        label, eligible = outcomes.label_final(
            decision=decision,
            executable_entry_price=exec_px,
            hypo_size_usd=(size if size > 0 else Decimal("10")),
            won=won,
            actual_realized=actual,
        )
        hypo = outcomes.hypothetical_pnl(
            executable_entry_price=exec_px,
            size_usd=(size if size > 0 else Decimal("10")),
            won=won,
        )
        await _insert_review(
            ctx, dj_id, "final",
            (Decimal(1) if won else ZERO), hypo, actual, label, eligible, None,
            paper_trade_id=pt_id,
        )
        created += 1
    return created


async def _checkpoint_price(
    ctx: JobContext, clob: ClobAdapter, asset_id: str | None, snapshot_id: int | None
) -> Decimal | None:
    """Fresh best bid, falling back to the decision-time snapshot's best bid."""
    if asset_id:
        try:
            book = await clob.get_book(asset_id)
            if book.best_bid is not None:
                return book.best_bid.price
        except Exception:  # noqa: BLE001
            pass
    if snapshot_id is not None:
        cur = await ctx.conn.execute(
            "SELECT best_bid FROM market_snapshots WHERE id = ?", (snapshot_id,)
        )
        row = await cur.fetchone()
        if row is not None and row[0] is not None:
            return micro_to_px(int(row[0]))
    return None


async def _review_exists(ctx: JobContext, dj_id: int, checkpoint: str) -> bool:
    cur = await ctx.conn.execute(
        "SELECT 1 FROM outcome_reviews WHERE decision_journal_id = ? AND review_checkpoint = ?",
        (dj_id, checkpoint),
    )
    return await cur.fetchone() is not None


async def _insert_review(
    ctx: JobContext,
    dj_id: int,
    checkpoint: str,
    price: Decimal | None,
    hypo: Decimal | None,
    actual: Decimal | None,
    label: str,
    eligible: bool,
    snapshot_id: int | None,
    *,
    paper_trade_id: int | None = None,
) -> None:
    now = utcnow_iso()
    await ctx.conn.execute(
        """
        INSERT OR IGNORE INTO outcome_reviews
            (decision_journal_id, paper_trade_id, review_checkpoint, price_at_checkpoint,
             hypothetical_pnl, actual_pnl, decision_quality_label, market_snapshot_id,
             eligible_for_learning, reviewed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dj_id,
            paper_trade_id,
            checkpoint,
            px_to_micro(price) if price is not None else None,
            usd_to_micro(hypo) if hypo is not None else None,
            usd_to_micro(actual) if actual is not None else None,
            label,
            snapshot_id,
            1 if eligible else 0,
            now,
            now,
        ),
    )
    await ctx.conn.commit()
    ctx.written()


def _iso_to_ts(value: str) -> int:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
