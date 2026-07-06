"""Bounded self-improvement — pure rule logic (PRD sec 17).

This module holds the immutable whitelist/immutable lists, the deterministic
proposal heuristics, bound enforcement, weight renormalization, checksum, and the
rollback-trip check. It never touches the DB; jobs/rule_eval.py gathers evidence,
enforces sample gates, and performs the transactional append-only activation.

Whitelisted parameter FAMILIES (§17.2) — one may change per daily run:
  binding_gate     : decision_thresholds.paper_copy_min_score  (the copy gate)
  spread_gate      : hard_gates.max_spread
  price_move_gate  : hard_gates.max_price_move_absolute
  liquidity_gate   : hard_gates.min_depth_usd
  time_gate        : hard_gates.min_time_to_resolution_seconds
  wallet_score_gate: wallet_status_thresholds.track_min_score
  trade_weights    : trade_score_weights.*  (renormalized to 100)

IMMUTABLE (§17.3) — enforced here; a proposal touching these is rejected.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

# Whitelisted family -> dotted path(s) into the payload.
WHITELISTED_FAMILIES: dict[str, tuple[str, ...]] = {
    "binding_gate": ("decision_thresholds.paper_copy_min_score",),
    "spread_gate": ("hard_gates.max_spread",),
    "price_move_gate": ("hard_gates.max_price_move_absolute",),
    "liquidity_gate": ("hard_gates.min_depth_usd",),
    "time_gate": ("hard_gates.min_time_to_resolution_seconds",),
    "wallet_score_gate": ("wallet_status_thresholds.track_min_score",),
    "trade_weights": ("trade_score_weights",),
}

# Immutable parameters (§17.3): never auto-changed.
IMMUTABLE_PATHS = frozenset({
    "confidence_tiers",           # incl the $20 max tier
    "exposure_limits.max_position_usd",
    "rule_evaluator_bounds",      # minimum evidence requirements
    "evidence",
})


class ProposalRejected(Exception):
    """Raised when a proposal violates a hard rule (family/bounds/immutable)."""


@dataclass(frozen=True)
class Proposal:
    family: str
    path: str
    old_value: object
    new_value: object
    target_metric: str
    baseline_value: str
    expected_value: str
    rollback_rule: dict


def _get_path(payload: dict, path: str):
    node = payload
    for part in path.split("."):
        node = node[part]
    return node


def _set_path(payload: dict, path: str, value) -> None:
    parts = path.split(".")
    node = payload
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def checksum(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_change(old: Decimal, new: Decimal, max_rel: Decimal) -> bool:
    if old == 0:
        return new == 0
    return abs((new - old) / old) <= max_rel + Decimal("0.0000001")


# --- proposal generation (deterministic heuristics, §17.5) ------------------

def propose(evidence: dict, payload: dict) -> Proposal | None:
    """Deterministic proposal from labelled-decision evidence.

    Heuristic (documented, simple):
      * high missed_winner rate and low bad_copy rate -> LOOSEN the binding gate
        (paper_copy_min_score) by up to 10% (system is too strict).
      * high bad_copy rate -> TIGHTEN the binding gate by up to 10%.
    Only the ``binding_gate`` family is proposed by this heuristic — one family
    per run (§17.5). Returns None when no change is warranted.
    """
    total = evidence.get("judged", 0)
    if total <= 0:
        return None
    missed = evidence.get("missed_winner", 0)
    bad = evidence.get("bad_copy", 0)
    missed_rate = missed / total
    bad_rate = bad / total

    bounds = payload["rule_evaluator_bounds"]
    max_rel = Decimal(str(bounds["max_relative_change"]))
    path = WHITELISTED_FAMILIES["binding_gate"][0]
    old = Decimal(str(_get_path(payload, path)))

    if missed_rate >= Decimal("0.30") and bad_rate <= Decimal("0.10"):
        new = (old * (Decimal(1) - max_rel)).quantize(Decimal("1"))
        direction, metric = "loosen", "missed_winner_rate"
    elif bad_rate >= Decimal("0.30"):
        new = (old * (Decimal(1) + max_rel)).quantize(Decimal("1"))
        direction, metric = "tighten", "bad_copy_rate"
    else:
        return None

    if new == old:
        return None

    rollback_rule = {
        "metric": metric,
        # revert if the targeted rate gets materially worse (>25% relative) or
        # unjudgeable spikes in the next window.
        "worse_relative": 0.25,
        "window_min_judged": bounds["min_judged_decisions"],
        "baseline_rate": float(missed_rate if direction == "loosen" else bad_rate),
    }
    return Proposal(
        family="binding_gate",
        path=path,
        old_value=int(old),
        new_value=int(new),
        target_metric=metric,
        baseline_value=str(missed_rate if direction == "loosen" else bad_rate),
        expected_value=f"{direction} gate {int(old)}->{int(new)}",
        rollback_rule=rollback_rule,
    )


# --- applying a proposal with bound enforcement -----------------------------

def apply_proposal(payload: dict, proposal: Proposal) -> dict:
    """Return a NEW payload with the proposal applied, enforcing all bounds.

    Raises ProposalRejected for: non-whitelisted family, immutable path, or a
    numeric move beyond max_relative_change. Weight families are renormalized to
    the configured total.
    """
    if proposal.family not in WHITELISTED_FAMILIES:
        raise ProposalRejected(f"family not whitelisted: {proposal.family}")
    for imm in IMMUTABLE_PATHS:
        if proposal.path == imm or proposal.path.startswith(imm + "."):
            raise ProposalRejected(f"immutable path: {proposal.path}")

    bounds = payload["rule_evaluator_bounds"]
    max_rel = Decimal(str(bounds["max_relative_change"]))
    new_payload = copy.deepcopy(payload)

    if proposal.family == "trade_weights":
        weights = dict(new_payload["trade_score_weights"])
        if not isinstance(proposal.new_value, dict):
            raise ProposalRejected("trade_weights proposal must be a dict")
        for key, val in proposal.new_value.items():
            if key not in weights:
                raise ProposalRejected(f"unknown weight: {key}")
            if not _bounded_change(Decimal(str(weights[key])), Decimal(str(val)), max_rel):
                raise ProposalRejected(f"weight {key} move exceeds {max_rel}")
            weights[key] = Decimal(str(val))
        new_payload["trade_score_weights"] = _renormalize_weights(
            weights, Decimal(str(bounds["weights_total"])),
            Decimal(str(bounds["weight_min"])), Decimal(str(bounds["weight_max"])),
        )
        _set_path(new_payload, "version", int(payload["version"]) + 1)
        return new_payload

    old = Decimal(str(_get_path(new_payload, proposal.path)))
    new = Decimal(str(proposal.new_value))
    if not _bounded_change(old, new, max_rel):
        raise ProposalRejected(
            f"{proposal.path} move {old}->{new} exceeds {max_rel} relative"
        )
    # preserve int-ness of the original
    original = _get_path(new_payload, proposal.path)
    _set_path(new_payload, proposal.path, int(new) if isinstance(original, int) else float(new))
    _set_path(new_payload, "version", int(payload["version"]) + 1)
    return new_payload


def _renormalize_weights(
    weights: dict, total: Decimal, wmin: Decimal, wmax: Decimal
) -> dict:
    for key, val in weights.items():
        if val < wmin or val > wmax:
            raise ProposalRejected(f"weight {key}={val} outside [{wmin},{wmax}]")
    current = sum(weights.values(), Decimal(0))
    if current <= 0:
        raise ProposalRejected("weights sum to zero")
    scaled = {k: (v * total / current) for k, v in weights.items()}
    # round to ints and fix the largest bucket so the total is exact.
    rounded = {k: int(v.quantize(Decimal("1"))) for k, v in scaled.items()}
    drift = int(total) - sum(rounded.values())
    if drift != 0:
        top = max(rounded, key=lambda k: rounded[k])
        rounded[top] += drift
    return rounded


# --- rollback trip check ----------------------------------------------------

def rollback_trips(rollback_rule: dict, new_window: dict) -> bool:
    """True when the last change's rollback rule fires on the new evidence window.

    Trips when the targeted rate is materially worse than baseline, or when the
    unjudgeable share spikes above 40% of judged+unjudged.
    """
    judged = new_window.get("judged", 0)
    if judged < rollback_rule.get("window_min_judged", 20):
        return False  # not enough new evidence to judge the change yet

    metric = rollback_rule.get("metric")
    baseline = Decimal(str(rollback_rule.get("baseline_rate", 0)))
    worse_rel = Decimal(str(rollback_rule.get("worse_relative", 0.25)))

    if metric == "missed_winner_rate":
        current = Decimal(new_window.get("missed_winner", 0)) / Decimal(judged)
    elif metric == "bad_copy_rate":
        current = Decimal(new_window.get("bad_copy", 0)) / Decimal(judged)
    else:
        current = baseline

    if baseline > 0 and current > baseline * (Decimal(1) + worse_rel):
        return True

    total_records = judged + new_window.get("unjudgeable", 0)
    if total_records > 0:
        unj_rate = Decimal(new_window.get("unjudgeable", 0)) / Decimal(total_records)
        if unj_rate > Decimal("0.40"):
            return True
    return False
