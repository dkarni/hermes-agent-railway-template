"""Wave 3 rule-logic unit tests (PRD sec 17) — pure domain.

Covers propose/apply: refuses below sample (propose returns None), caps at 10%
relative, refuses non-whitelisted family, refuses immutable path, renormalizes
weights to 100, one family per run; rollback_trips. The transactional/append-only
activation + end-to-end rollback are covered in test_e2e_wave3.py against a DB.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from .. import db as dbmod
from ..domain import rules


def _payload():
    return copy.deepcopy(dbmod.initial_rule_set_payload())


# --- propose ----------------------------------------------------------------

def test_propose_none_when_no_judged():
    assert rules.propose({"judged": 0}, _payload()) is None


def test_propose_loosen_on_high_missed_low_bad():
    payload = _payload()
    evidence = {"judged": 20, "missed_winner": 8, "bad_copy": 1}
    proposal = rules.propose(evidence, payload)
    assert proposal is not None
    assert proposal.family == "binding_gate"
    # loosen = lower the copy gate by up to 10% (75 -> 68).
    assert proposal.new_value < proposal.old_value
    assert proposal.old_value == 75


def test_propose_tighten_on_high_bad():
    payload = _payload()
    evidence = {"judged": 20, "missed_winner": 0, "bad_copy": 8}
    proposal = rules.propose(evidence, payload)
    assert proposal is not None
    assert proposal.new_value > proposal.old_value


def test_propose_none_when_no_signal():
    payload = _payload()
    evidence = {"judged": 20, "missed_winner": 2, "bad_copy": 2}
    assert rules.propose(evidence, payload) is None


# --- apply_proposal bounds --------------------------------------------------

def test_apply_caps_at_10_percent_relative():
    payload = _payload()
    # An oversized manual proposal (75 -> 60 is -20%, beyond 10%) must be rejected.
    bad = rules.Proposal(
        family="binding_gate",
        path="decision_thresholds.paper_copy_min_score",
        old_value=75, new_value=60, target_metric="x", baseline_value="0",
        expected_value="", rollback_rule={},
    )
    with pytest.raises(rules.ProposalRejected):
        rules.apply_proposal(payload, bad)


def test_apply_within_bounds_increments_version_and_sets_value():
    payload = _payload()
    proposal = rules.propose({"judged": 20, "missed_winner": 8, "bad_copy": 1}, payload)
    new_payload = rules.apply_proposal(payload, proposal)
    assert new_payload["version"] == payload["version"] + 1
    assert new_payload["decision_thresholds"]["paper_copy_min_score"] == proposal.new_value
    # original untouched (append-only intent at the domain level)
    assert payload["decision_thresholds"]["paper_copy_min_score"] == 75


def test_apply_refuses_non_whitelisted_family():
    payload = _payload()
    bad = rules.Proposal(
        family="not_a_family", path="whatever", old_value=1, new_value=1,
        target_metric="", baseline_value="", expected_value="", rollback_rule={},
    )
    with pytest.raises(rules.ProposalRejected):
        rules.apply_proposal(payload, bad)


def test_apply_refuses_immutable_path():
    payload = _payload()
    bad = rules.Proposal(
        family="binding_gate", path="exposure_limits.max_position_usd",
        old_value="20", new_value="21", target_metric="", baseline_value="",
        expected_value="", rollback_rule={},
    )
    with pytest.raises(rules.ProposalRejected):
        rules.apply_proposal(payload, bad)


# --- weight renormalization -------------------------------------------------

def test_trade_weights_renormalize_to_100_one_family():
    payload = _payload()
    # Nudge two weights within 10% and expect renormalization back to 100.
    weights = dict(payload["trade_score_weights"])
    proposal = rules.Proposal(
        family="trade_weights", path="trade_score_weights",
        old_value=weights,
        new_value={"wallet_global_quality": 27, "category_fit": 14},
        target_metric="", baseline_value="", expected_value="", rollback_rule={},
    )
    new_payload = rules.apply_proposal(payload, proposal)
    assert sum(new_payload["trade_score_weights"].values()) == 100
    # only the weight family changed; gates untouched.
    assert new_payload["hard_gates"] == payload["hard_gates"]


def test_renormalize_rejects_weight_out_of_bounds():
    with pytest.raises(rules.ProposalRejected):
        rules._renormalize_weights(
            {"a": Decimal("150"), "b": Decimal("10")},
            Decimal("100"), Decimal("0"), Decimal("100"),
        )


# --- rollback_trips ---------------------------------------------------------

def test_rollback_trips_when_metric_worsens():
    rule = {"metric": "missed_winner_rate", "worse_relative": 0.25,
            "window_min_judged": 20, "baseline_rate": 0.20}
    # new window: 20 judged, 8 missed -> 0.40 rate > 0.20*1.25=0.25 -> trip.
    assert rules.rollback_trips(rule, {"judged": 20, "missed_winner": 8}) is True


def test_rollback_does_not_trip_when_stable():
    rule = {"metric": "missed_winner_rate", "worse_relative": 0.25,
            "window_min_judged": 20, "baseline_rate": 0.20}
    assert rules.rollback_trips(rule, {"judged": 20, "missed_winner": 4}) is False


def test_rollback_waits_for_enough_evidence():
    rule = {"metric": "missed_winner_rate", "worse_relative": 0.25,
            "window_min_judged": 20, "baseline_rate": 0.20}
    # Only 5 judged so far -> cannot judge yet.
    assert rules.rollback_trips(rule, {"judged": 5, "missed_winner": 5}) is False


def test_rollback_trips_on_unjudgeable_spike():
    rule = {"metric": "bad_copy_rate", "worse_relative": 0.25,
            "window_min_judged": 20, "baseline_rate": 0.10}
    # bad_copy fine (2/20=0.10) but unjudgeable dominates -> trip.
    window = {"judged": 20, "bad_copy": 2, "unjudgeable": 20}
    assert rules.rollback_trips(rule, window) is True


def test_checksum_stable_and_changes():
    p = _payload()
    assert rules.checksum(p) == rules.checksum(copy.deepcopy(p))
    p2 = _payload()
    p2["version"] = 999
    assert rules.checksum(p) != rules.checksum(p2)
