"""Component calibration tests (domain + API).

The domain suite pins the Mann-Whitney AUC on a hand-computable case, tie
handling (0.5 contribution), the empty-class / empty-input None paths, band
boundary placement (0,25,50,75,100 with 100 in the top band), missing-component
skipping, the sufficiency flag, and the exclusion of non GOOD/BAD labels.

The API/query test seeds final, eligible, non-demo reviews and asserts the
endpoint's counts/AUC plus a demo row and a non-final checkpoint row are
excluded, and that ?format=csv returns text/csv.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from .. import db as dbmod
from ..api import create_app
from ..config import load_config
from ..domain import calibration as cal
from ..domain import outcomes
from ._seed import seed


C = "wallet_global_quality"  # component under test in most unit cases


def _rec(score, label, *, decision="skip", pnl=None, component=C):
    comps = {component: score} if score is not None else {}
    return cal.CalibrationRecord(components=comps, label=label, decision=decision, pnl=pnl)


# --- AUC / separation -------------------------------------------------------

def test_auc_known_value_perfect_separation():
    # Every good scores above every bad -> AUC = 1.0.
    records = [
        _rec(90, outcomes.GOOD_SKIP), _rec(80, outcomes.GOOD_COPY),
        _rec(20, outcomes.MISSED_WINNER), _rec(10, outcomes.BAD_COPY),
    ]
    results, summary = cal.component_calibration(records, min_sample=1)
    comp = results[C]
    assert comp.n_good == 2 and comp.n_bad == 2
    assert comp.auc == Decimal(1)
    assert comp.mean_good == Decimal(85)
    assert comp.mean_bad == Decimal(15)
    assert comp.separation == Decimal(70)
    assert summary.total_records == 4


def test_auc_hand_computable_mixed():
    # goods {60,40}, bads {50,20}. Pairs: 60>50,60>20,40<50,40>20 -> 3/4 = 0.75.
    records = [
        _rec(60, outcomes.GOOD_SKIP), _rec(40, outcomes.GOOD_WATCH),
        _rec(50, outcomes.BAD_COPY), _rec(20, outcomes.MISSED_WATCH_ENTRY),
    ]
    results, _ = cal.component_calibration(records, min_sample=1)
    assert results[C].auc == Decimal("0.75")


def test_auc_ties_count_half():
    # good {50}, bad {50} -> single tied pair -> 0.5.
    records = [_rec(50, outcomes.GOOD_SKIP), _rec(50, outcomes.BAD_COPY)]
    results, _ = cal.component_calibration(records, min_sample=1)
    assert results[C].auc == Decimal("0.5")


def test_auc_none_when_one_class_empty():
    records = [_rec(80, outcomes.GOOD_SKIP), _rec(70, outcomes.GOOD_COPY)]
    results, _ = cal.component_calibration(records, min_sample=1)
    comp = results[C]
    assert comp.n_bad == 0
    assert comp.auc is None
    assert comp.mean_bad is None
    assert comp.separation is None
    assert comp.mean_good == Decimal(75)


def test_empty_input():
    results, summary = cal.component_calibration([], min_sample=20)
    assert summary.total_records == 0
    assert summary.label_counts == {}
    comp = results[C]
    assert comp.n_good == 0 and comp.n_bad == 0
    assert comp.auc is None and comp.separation is None
    assert comp.sufficient is False
    assert all(b.n == 0 and b.bad_rate is None and b.avg_pnl is None for b in comp.bands)


# --- bands ------------------------------------------------------------------

def test_band_boundaries():
    # 0->band0, 25->band1, 50->band2, 75->band3, 100->band3 (top band closed).
    boundary_to_idx = {0: 0, 25: 1, 50: 2, 75: 3, 100: 3}
    for score, idx in boundary_to_idx.items():
        assert cal._band_index(Decimal(score)) == idx


def test_band_out_of_range_none():
    assert cal._band_index(Decimal("-1")) is None
    assert cal._band_index(Decimal("101")) is None


def test_band_stats_bad_rate_and_avg_pnl():
    records = [
        _rec(80, outcomes.GOOD_SKIP, pnl=Decimal("10")),   # top band, good
        _rec(90, outcomes.BAD_COPY, pnl=Decimal("-4")),    # top band, bad
        _rec(10, outcomes.BAD_COPY, pnl=Decimal("-2")),    # bottom band, bad
    ]
    results, _ = cal.component_calibration(records, min_sample=1)
    bands = results[C].bands
    bottom, top = bands[0], bands[3]
    assert bottom.n == 1 and bottom.bad_rate == Decimal(1)
    assert bottom.avg_pnl == Decimal("-2")
    assert top.n == 2 and top.bad_rate == Decimal("0.5")
    assert top.avg_pnl == Decimal(3)  # (10 + -4)/2
    # Empty bands carry Nones.
    assert bands[1].n == 0 and bands[1].bad_rate is None and bands[1].avg_pnl is None


def test_band_avg_pnl_none_when_no_pnl_values():
    records = [_rec(80, outcomes.GOOD_SKIP), _rec(90, outcomes.BAD_COPY)]
    results, _ = cal.component_calibration(records, min_sample=1)
    top = results[C].bands[3]
    assert top.n == 2 and top.avg_pnl is None


# --- record / label handling ------------------------------------------------

def test_missing_component_key_skipped_per_component():
    # Record has category_fit but not wallet_global_quality.
    records = [
        cal.CalibrationRecord(
            components={"category_fit": Decimal(90)},
            label=outcomes.GOOD_SKIP, decision="skip", pnl=None,
        ),
        _rec(30, outcomes.BAD_COPY),  # only wallet_global_quality
    ]
    results, summary = cal.component_calibration(records, min_sample=1)
    # Both records are judged (counted in summary), but each component only sees
    # the record that carries its key.
    assert summary.total_records == 2
    assert results[C].n_good == 0 and results[C].n_bad == 1
    assert results["category_fit"].n_good == 1 and results["category_fit"].n_bad == 0


def test_insufficient_sample_flag():
    records = [_rec(80, outcomes.GOOD_SKIP), _rec(20, outcomes.BAD_COPY)]
    results, _ = cal.component_calibration(records, min_sample=20)
    assert results[C].sufficient is False
    results2, _ = cal.component_calibration(records, min_sample=2)
    assert results2[C].sufficient is True


def test_non_good_bad_labels_skipped():
    records = [
        _rec(80, outcomes.GOOD_SKIP),
        _rec(50, outcomes.UNJUDGEABLE),   # excluded
        _rec(50, "some_unknown_label"),   # excluded
        _rec(20, outcomes.BAD_COPY),
    ]
    results, summary = cal.component_calibration(records, min_sample=1)
    assert summary.total_records == 2
    assert set(summary.label_counts) == {outcomes.GOOD_SKIP, outcomes.BAD_COPY}
    assert results[C].n_good == 1 and results[C].n_bad == 1


def test_float_and_none_score_inputs():
    records = [
        cal.CalibrationRecord(components={C: 80.0}, label=outcomes.GOOD_SKIP,
                              decision="skip", pnl=1.5),
        cal.CalibrationRecord(components={C: None}, label=outcomes.BAD_COPY,
                              decision="skip", pnl=None),  # None score skipped
        _rec(20, outcomes.BAD_COPY),
    ]
    results, _ = cal.component_calibration(records, min_sample=1)
    comp = results[C]
    assert comp.n_good == 1 and comp.n_bad == 1  # None-score bad row skipped
    assert comp.mean_good == Decimal("80")
    assert comp.bands[3].avg_pnl == Decimal("1.5")  # float pnl converted


# --- API / query ------------------------------------------------------------

async def _app(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    conn = await dbmod.init_db(config.db_path, config.migrations_dir)
    ids = await seed(conn)
    return conn, create_app(conn, config), ids


async def _add_decision(conn, *, decision, components_json, label, checkpoint,
                        eligible, is_demo, actual_pnl, hypo_pnl=None):
    """Insert a decision_journal row + one outcome_review for calibration tests."""
    now = dbmod.utcnow_iso()
    cur = await conn.execute("SELECT id FROM rule_sets WHERE status='active'")
    rule_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO observed_trades (source,wallet_address,condition_id,market_id,source_side,"
        "outcome,source_price,idempotency_key,detected_at) VALUES "
        "('dataapi','0xabc','c1','m1','BUY','Yes',450000,?,?)",
        (f"cal-{dbmod.utcnow_iso()}-{label}-{checkpoint}-{is_demo}", now),
    )
    obs_id = cur.lastrowid
    cur = await conn.execute(
        "INSERT INTO decision_journal (observed_trade_id,wallet_address,market_id,rule_set_id,"
        "decision,total_score,component_scores_json,created_at,is_demo) VALUES "
        "(?,'0xabc','m1',?,?,50,?,?,?)",
        (obs_id, rule_id, decision, components_json, now, is_demo),
    )
    dj_id = cur.lastrowid
    await conn.execute(
        "INSERT INTO outcome_reviews (decision_journal_id,review_checkpoint,"
        "hypothetical_pnl,actual_pnl,decision_quality_label,eligible_for_learning,"
        "reviewed_at,created_at,is_demo) VALUES (?,?,?,?,?,?,?,?,?)",
        (dj_id, checkpoint, hypo_pnl, actual_pnl, label, eligible, now, now, is_demo),
    )
    await conn.commit()
    return dj_id


async def test_calibration_endpoint_counts_auc_and_exclusions(tmp_path):
    conn, app, _ids = await _app(tmp_path)
    try:
        # Eligible, final, non-demo GOOD (high score) and BAD (low score).
        await _add_decision(
            conn, decision="skip",
            components_json='{"wallet_global_quality": 90, "spread": 70}',
            label=outcomes.GOOD_SKIP, checkpoint="final", eligible=1, is_demo=0,
            actual_pnl=None, hypo_pnl=5000000,
        )
        await _add_decision(
            conn, decision="skip",
            components_json='{"wallet_global_quality": 20, "spread": 40}',
            label=outcomes.MISSED_WINNER, checkpoint="final", eligible=1, is_demo=0,
            actual_pnl=None, hypo_pnl=-3000000,
        )
        # MUST be excluded: demo row, and a non-final checkpoint row.
        await _add_decision(
            conn, decision="skip",
            components_json='{"wallet_global_quality": 5}',
            label=outcomes.BAD_COPY, checkpoint="final", eligible=1, is_demo=1,
            actual_pnl=-9000000,
        )
        await _add_decision(
            conn, decision="skip",
            components_json='{"wallet_global_quality": 99}',
            label=outcomes.GOOD_SKIP, checkpoint="1h", eligible=0, is_demo=0,
            actual_pnl=None, hypo_pnl=1000000,
        )

        with TestClient(app) as client:
            body = client.get("/api/polymarket/performance/calibration").json()
            # Seed adds one good_copy final eligible non-demo row (score wgq=22),
            # plus our GOOD_SKIP + MISSED_WINNER -> 3 judged records total.
            assert body["total_records"] == 3
            comps = {c["component"]: c for c in body["components"]}
            wgq = comps["wallet_global_quality"]
            assert wgq["n_good"] == 2 and wgq["n_bad"] == 1
            # goods {90,22} vs bad {20}: both > 20 -> AUC = 1.0.
            assert wgq["auc"] == "1"
            assert body["window"] == "all"
            assert isinstance(body["min_sample"], int)

            # CSV: one row per component x band, text/csv.
            resp = client.get("/api/polymarket/performance/calibration?format=csv")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/csv")
            assert "calibration.csv" in resp.headers.get("content-disposition", "")
            assert "component,band,n,bad_rate,avg_pnl_usd" in resp.text.splitlines()[0]
    finally:
        await conn.close()


async def test_calibration_route_ordering(tmp_path):
    # /performance/calibration must not be swallowed by /performance.
    conn, app, _ids = await _app(tmp_path)
    try:
        with TestClient(app) as client:
            resp = client.get("/api/polymarket/performance/calibration")
            assert resp.status_code == 200
            assert "components" in resp.json()
    finally:
        await conn.close()
