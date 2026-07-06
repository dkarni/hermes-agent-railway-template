"""Daily bounded self-improvement (PRD sec 17), gated by RULE_UPDATE_ENABLED.

Order of operations (deterministic):
  1. Gather the evidence window: final, eligible-for-learning, non-demo,
     non-admin outcome_reviews joined to decision_journal, since the last rule
     change (or last 30d), counted per decision-quality label.
  2. Rollback FIRST: evaluate the newest pending rule_changes row's
     rollback_rule_json against the new window. If it trips, clone the parent
     payload into a fresh active rule_set (transactionally retiring the current
     active one as 'rolled_back') and mark the change 'rolled_back'. If the
     window is sufficient and it does not trip, mark it 'kept'.
  3. Evidence gates: >=20 judged since last change AND >=10 relevant to the
     target family AND no unresolved critical data_quality_events in the window.
  4. propose -> apply_proposal -> append a new draft rule_set, then transactional
     activation (parent -> superseded, new -> active) + a rule_changes row
     (outcome_status='pending').

Everything is append-only: parameters_json of an existing rule_set is never
UPDATEd. Only status / activated_at / deactivated_at columns change.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config import Config
from ..db import json_dumps, utcnow_iso
from ..domain import rules
from .alerts import alert
from .runner import JobContext

# Labels that count as "relevant" to the binding_gate family (the only family
# the current propose() heuristic targets): copy/skip decisions drive the copy
# gate. Watch labels are counted as judged but not as gate-relevant.
_BINDING_GATE_RELEVANT = frozenset(
    {"good_copy", "bad_copy", "good_skip", "missed_winner"}
)

DEFAULT_WINDOW_DAYS = 30


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_days_ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


async def _window_start(conn: aiosqlite.Connection, strategy: str) -> str:
    """Evidence window start: newest rule_change created_at, else 30d ago."""
    cur = await conn.execute(
        """
        SELECT rc.created_at
          FROM rule_changes rc
          JOIN rule_sets rs ON rs.id = rc.rule_set_id
         WHERE rs.strategy = ?
         ORDER BY rc.id DESC LIMIT 1
        """,
        (strategy,),
    )
    row = await cur.fetchone()
    if row and row[0]:
        return row[0]
    return _iso_days_ago(DEFAULT_WINDOW_DAYS)


async def _gather_evidence(conn: aiosqlite.Connection, since_iso: str) -> dict:
    """Count final judged reviews per label since ``since_iso``.

    Only final, eligible_for_learning=1, non-demo reviews on non-demo,
    non-admin decisions count. Admin-closed paper trades are excluded via the
    paper_trades.is_admin flag; unjudgeable reviews are excluded by the
    eligible_for_learning filter (they carry eligible=0).
    """
    cur = await conn.execute(
        """
        SELECT orv.decision_quality_label, COUNT(*)
          FROM outcome_reviews orv
          JOIN decision_journal dj ON dj.id = orv.decision_journal_id
     LEFT JOIN paper_trades pt ON pt.decision_journal_id = dj.id
         WHERE orv.review_checkpoint = 'final'
           AND orv.eligible_for_learning = 1
           AND orv.is_demo = 0
           AND dj.is_demo = 0
           AND COALESCE(pt.is_admin, 0) = 0
           AND orv.created_at >= ?
         GROUP BY orv.decision_quality_label
        """,
        (since_iso,),
    )
    counts: dict[str, int] = {}
    for label, n in await cur.fetchall():
        counts[label or "unlabeled"] = int(n)
    judged = sum(counts.values())
    relevant = sum(counts.get(k, 0) for k in _BINDING_GATE_RELEVANT)
    evidence = dict(counts)
    evidence["judged"] = judged
    evidence["relevant"] = relevant
    # Convenience keys the domain heuristics read directly.
    evidence.setdefault("missed_winner", counts.get("missed_winner", 0))
    evidence.setdefault("bad_copy", counts.get("bad_copy", 0))
    evidence.setdefault("good_copy", counts.get("good_copy", 0))
    evidence.setdefault("good_skip", counts.get("good_skip", 0))
    evidence.setdefault("unjudgeable", 0)
    return evidence


async def _critical_dq_in_window(conn: aiosqlite.Connection, since_iso: str) -> int:
    cur = await conn.execute(
        """
        SELECT COUNT(*) FROM data_quality_events
         WHERE severity = 'critical' AND resolved_at IS NULL
           AND detected_at >= ?
        """,
        (since_iso,),
    )
    row = await cur.fetchone()
    return int(row[0] or 0)


async def _active_rule_set(conn: aiosqlite.Connection, strategy: str) -> tuple[int, int, dict] | None:
    cur = await conn.execute(
        "SELECT id, version, parameters_json FROM rule_sets WHERE strategy = ? AND status = 'active'",
        (strategy,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]), json.loads(row[2])


async def _newest_pending_change(
    conn: aiosqlite.Connection, strategy: str
) -> tuple[int, int, dict] | None:
    """(rule_change_id, rule_set_id, rollback_rule) for the newest pending change."""
    cur = await conn.execute(
        """
        SELECT rc.id, rc.rule_set_id, rc.rollback_rule_json
          FROM rule_changes rc
          JOIN rule_sets rs ON rs.id = rc.rule_set_id
         WHERE rs.strategy = ? AND rc.outcome_status = 'pending'
         ORDER BY rc.id DESC LIMIT 1
        """,
        (strategy,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    rollback = json.loads(row[2]) if row[2] else {}
    return int(row[0]), int(row[1]), rollback


async def _do_rollback(
    ctx: JobContext, conn: aiosqlite.Connection, strategy: str, change_id: int, changed_rule_set_id: int
) -> int | None:
    """Clone the parent payload into a new active rule set; retire the current one.

    Transactional + append-only: the tripped active row -> 'rolled_back', a NEW
    rule_sets row cloning the parent's parameters_json becomes active, and the
    rule_changes row is marked 'rolled_back'.
    """
    # Find the parent (pre-change) payload for the changed rule set.
    cur = await conn.execute(
        "SELECT parent_rule_set_id FROM rule_sets WHERE id = ?", (changed_rule_set_id,)
    )
    prow = await cur.fetchone()
    if prow is None or prow[0] is None:
        return None
    parent_id = int(prow[0])
    cur = await conn.execute(
        "SELECT version, parameters_json FROM rule_sets WHERE id = ?", (parent_id,)
    )
    parent = await cur.fetchone()
    if parent is None:
        return None
    parent_payload = json.loads(parent[1])

    # New version number = current max + 1.
    cur = await conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM rule_sets WHERE strategy = ?", (strategy,)
    )
    next_version = int((await cur.fetchone())[0]) + 1
    restored = dict(parent_payload)
    restored["version"] = next_version
    now = utcnow_iso()

    # Retire the currently-active rule set (the tripped one) then insert the clone.
    await conn.execute(
        "UPDATE rule_sets SET status = 'rolled_back', deactivated_at = ? WHERE strategy = ? AND status = 'active'",
        (now, strategy),
    )
    cur = await conn.execute(
        """
        INSERT INTO rule_sets
            (strategy, version, status, parameters_json, checksum,
             parent_rule_set_id, activated_at, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (strategy, next_version, json.dumps(restored), rules.checksum(restored), parent_id, now, now),
    )
    new_id = int(cur.lastrowid)
    await conn.execute(
        "UPDATE rule_changes SET outcome_status = 'rolled_back', evaluated_at = ? WHERE id = ?",
        (now, change_id),
    )
    await conn.commit()
    ctx.written()
    await alert(
        conn,
        type="rule_rollback",
        severity="warning",
        dedupe_key=f"rule_rollback:{change_id}",
        message=f"Rule change {change_id} rolled back; restored rule set v{next_version}.",
        metadata={"rule_change_id": change_id, "restored_rule_set_id": new_id},
    )
    return new_id


async def _activate_change(
    ctx: JobContext,
    conn: aiosqlite.Connection,
    strategy: str,
    parent_id: int,
    parent_version: int,
    new_payload: dict,
    proposal: rules.Proposal,
    sample_size: int,
) -> int:
    """Append a draft rule set, activate it transactionally, record rule_changes."""
    now = utcnow_iso()
    new_version = int(new_payload["version"])
    checksum = rules.checksum(new_payload)

    # Insert as draft first (create before activate — PRD 17.5).
    cur = await conn.execute(
        """
        INSERT INTO rule_sets
            (strategy, version, status, parameters_json, checksum,
             parent_rule_set_id, created_at)
        VALUES (?, ?, 'draft', ?, ?, ?, ?)
        """,
        (strategy, new_version, json.dumps(new_payload), checksum, parent_id, now),
    )
    new_id = int(cur.lastrowid)

    # Transactional activation: parent -> superseded, draft -> active.
    await conn.execute(
        "UPDATE rule_sets SET status = 'superseded', deactivated_at = ? WHERE id = ?",
        (now, parent_id),
    )
    await conn.execute(
        "UPDATE rule_sets SET status = 'active', activated_at = ? WHERE id = ?",
        (now, new_id),
    )
    await conn.execute(
        """
        INSERT INTO rule_changes
            (rule_set_id, parent_rule_set_id, parameter_family, parameter_path,
             old_value_json, new_value_json, sample_size, target_metric,
             baseline_value, expected_value, rollback_rule_json, outcome_status,
             evaluated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            new_id,
            parent_id,
            proposal.family,
            proposal.path,
            json_dumps(proposal.old_value),
            json_dumps(proposal.new_value),
            sample_size,
            proposal.target_metric,
            proposal.baseline_value,
            proposal.expected_value,
            json_dumps(proposal.rollback_rule),
            now,
            now,
        ),
    )
    await conn.commit()
    ctx.written()
    await alert(
        conn,
        type="rule_change",
        severity="info",
        dedupe_key=f"rule_change:{new_id}",
        message=(
            f"Activated rule set v{new_version} ({proposal.family}: "
            f"{proposal.old_value}->{proposal.new_value})."
        ),
        metadata={"rule_set_id": new_id, "family": proposal.family},
    )
    return new_id


async def _days_since_first_portfolio(conn) -> int | None:
    cur = await conn.execute("SELECT MIN(started_at) FROM paper_portfolios")
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    from datetime import datetime, timezone

    started = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).days


async def run_rule_eval(ctx: JobContext, config: Config, *, strategy: str = "default") -> dict:
    conn = ctx.conn
    if not config.rule_update_enabled:
        return {"status": "disabled", "reason": "RULE_UPDATE_ENABLED=false"}

    # Burn-in gate (PRD phase 7): no automatic rule changes until the paper
    # system has run for RULE_UPDATE_MIN_DAYS. The sample-size gates alone
    # could be satisfied within days by a burst of judged decisions.
    days = await _days_since_first_portfolio(conn)
    if days is not None and days < config.rule_update_min_days:
        return {
            "status": "skipped",
            "reason": f"burn_in: day {days} of {config.rule_update_min_days} minimum",
        }

    active = await _active_rule_set(conn, strategy)
    if active is None:
        return {"status": "skipped", "reason": "no_active_rule_set"}
    active_id, active_version, payload = active

    since = await _window_start(conn, strategy)
    evidence = await _gather_evidence(conn, since)

    # 1. Rollback check FIRST.
    pending = await _newest_pending_change(conn, strategy)
    rollback_kept = False
    if pending is not None:
        change_id, changed_rule_set_id, rollback_rule = pending
        if rules.rollback_trips(rollback_rule, evidence):
            restored = await _do_rollback(ctx, conn, strategy, change_id, changed_rule_set_id)
            if restored is not None:
                return {
                    "status": "rolled_back",
                    "rule_change_id": change_id,
                    "restored_rule_set_id": restored,
                    "judged": evidence["judged"],
                }
        else:
            # Only mark 'kept' once the window is large enough to have judged it.
            window_min = int(rollback_rule.get("window_min_judged", 20))
            if evidence["judged"] >= window_min:
                now = utcnow_iso()
                await conn.execute(
                    "UPDATE rule_changes SET outcome_status = 'kept', evaluated_at = ? WHERE id = ?",
                    (now, change_id),
                )
                await conn.commit()
                rollback_kept = True

    # 2. Evidence gates.
    bounds = payload["rule_evaluator_bounds"]
    min_judged = int(bounds["min_judged_decisions"])
    min_relevant = int(bounds["min_relevant_decisions"])
    if evidence["judged"] < min_judged:
        return {"status": "skipped", "reason": "insufficient_judged",
                "judged": evidence["judged"], "required": min_judged}
    if evidence["relevant"] < min_relevant:
        return {"status": "skipped", "reason": "insufficient_relevant",
                "relevant": evidence["relevant"], "required": min_relevant}
    critical = await _critical_dq_in_window(conn, since)
    if critical > 0:
        return {"status": "skipped", "reason": "critical_data_quality_events",
                "critical_events": critical}

    # 3. Propose + apply + activate.
    proposal = rules.propose(evidence, payload)
    if proposal is None:
        return {"status": "no_change", "reason": "no_proposal", "judged": evidence["judged"]}
    try:
        new_payload = rules.apply_proposal(payload, proposal)
    except rules.ProposalRejected as exc:
        return {"status": "rejected", "reason": str(exc)}

    new_id = await _activate_change(
        ctx, conn, strategy, active_id, active_version, new_payload, proposal,
        sample_size=evidence["judged"],
    )
    return {
        "status": "changed",
        "rule_set_id": new_id,
        "family": proposal.family,
        "old_value": proposal.old_value,
        "new_value": proposal.new_value,
        "judged": evidence["judged"],
        "rollback_kept": rollback_kept,
    }


async def run_manual_rollback(
    ctx: JobContext, version: int, *, strategy: str = "default"
) -> dict:
    """Admin-triggered rollback of a specific rule-set version (PRD 20.9).

    Locates the rule_change that activated ``version`` and reuses the same
    deterministic ``_do_rollback`` path (clone parent -> new active row, retire
    the tripped active set, mark the change rolled_back). Requires ``version`` to
    be the currently-active version so history stays coherent.
    """
    conn = ctx.conn
    cur = await conn.execute(
        "SELECT id, status FROM rule_sets WHERE strategy = ? AND version = ?",
        (strategy, version),
    )
    row = await cur.fetchone()
    if row is None:
        return {"status": "not_found", "version": version}
    rule_set_id, status = int(row[0]), row[1]
    if status != "active":
        return {"status": "not_active", "version": version, "current_status": status}
    cur = await conn.execute(
        "SELECT id FROM rule_changes WHERE rule_set_id = ? ORDER BY id DESC LIMIT 1",
        (rule_set_id,),
    )
    change_row = await cur.fetchone()
    if change_row is None:
        return {"status": "no_change_record", "version": version}
    change_id = int(change_row[0])
    restored = await _do_rollback(ctx, conn, strategy, change_id, rule_set_id)
    if restored is None:
        return {"status": "no_parent", "version": version}
    return {
        "status": "rolled_back",
        "rolled_back_version": version,
        "rule_change_id": change_id,
        "restored_rule_set_id": restored,
    }
