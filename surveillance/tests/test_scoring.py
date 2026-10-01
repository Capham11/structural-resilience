"""
Offline unit tests for the anomaly scoring + corroboration layer.

Real data/surveillance.db has zero rows (confirmed before building this —
every stream is currently pre-launch), so these use small hand-built
synthetic histories instead, same convention as test_crosswalk_math.py.

Run with: pytest surveillance/tests/
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import scoring  # noqa: E402
import corroboration  # noqa: E402
import scoring_config as cfg  # noqa: E402


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    return conn


def _insert_history(conn, tract_id, stream, weeks, values, confidence=1.0, method="distance_weighted_idw"):
    df = pd.DataFrame({
        "tract_id": tract_id, "stream": stream, "week": weeks, "value": values,
        "confidence": confidence, "interpolation_method": method,
    })
    db.upsert_rows(conn, df)


# ==================================================
# PURE MATH
# ==================================================

def test_compute_baseline_matches_hand_calc():
    values = np.array([10.0, 10.0, 10.0, 10.0, 20.0])  # median 10, deviations [0,0,0,0,10], MAD=0
    median, mad = scoring.compute_baseline(values)
    assert median == pytest.approx(10.0)
    assert mad == pytest.approx(0.0)  # degenerate but shouldn't crash downstream (mad_floor handles it)


def test_normal_two_sided_p_decreases_with_larger_z():
    p_small = scoring.normal_two_sided_p(1.0)
    p_large = scoring.normal_two_sided_p(3.0)
    assert 0 < p_large < p_small < 1


# ==================================================
# PER-STREAM SCORING
# ==================================================

def _weeks(n, start="2026-01-04"):
    base = pd.Timestamp(start)
    return [(base + pd.Timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(n)]


def test_score_value_local_baseline_hand_computed():
    conn = _memory_conn()
    history_weeks = _weeks(8)  # 8 prior weeks, each value 0.5 -> median 0.5, MAD 0
    _insert_history(conn, "53033000100", "hospital_capacity", history_weeks, [0.5] * 8)

    target_week = _weeks(9)[-1]
    row = scoring.score_value(conn, "53033000100", "hospital_capacity", target_week, 0.9, confidence=1.0)

    assert row is not None
    assert row["baseline_source"] == "local"
    assert row["fallback_baseline_used"] == 0
    assert row["baseline_median"] == pytest.approx(0.5)
    # MAD degenerate (0) -> mad_floor = max(0.5*0.01, 1e-6) = 0.005
    expected_z = (0.9 - 0.5) / 0.005
    assert row["z_score"] == pytest.approx(expected_z)
    assert row["direction"] == "elevated"
    assert row["is_anomalous"] == 1  # z is huge here


def test_score_value_sits_out_below_min_local_history_with_no_fallback():
    conn = _memory_conn()
    history_weeks = _weeks(3)  # only 3 weeks — below MIN_LOCAL_HISTORY_WEEKS=8
    _insert_history(conn, "53033000100", "hospital_capacity", history_weeks, [0.5, 0.5, 0.5])

    target_week = _weeks(4)[-1]
    row = scoring.score_value(conn, "53033000100", "hospital_capacity", target_week, 0.9, confidence=1.0)

    assert row is None  # no county/statewide pool exists either in this fixture


def test_score_value_falls_back_to_county_pool_when_local_history_thin():
    conn = _memory_conn()
    weeks = _weeks(8)
    # Target tract has almost no history of its own...
    _insert_history(conn, "53033000100", "hospital_capacity", weeks[:2], [0.5, 0.5])
    # ...but enough OTHER tracts in the same county (53033 = King) do.
    for i, other_tract in enumerate(["53033000200", "53033000300", "53033000400"]):
        _insert_history(conn, other_tract, "hospital_capacity", weeks, [0.5] * 8)

    target_week = _weeks(9)[-1]
    row = scoring.score_value(conn, "53033000100", "hospital_capacity", target_week, 0.9, confidence=1.0)

    assert row is not None
    assert row["baseline_source"] == "county_fallback"
    assert row["fallback_baseline_used"] == 1
    assert row["baseline_median"] == pytest.approx(0.5)


def test_score_week_excludes_carry_forward_and_demo_from_history_and_current():
    conn = _memory_conn()
    weeks = _weeks(9)
    # Mix real history with carry_forward rows that should NOT count.
    _insert_history(conn, "53033000100", "hospital_capacity", weeks[:8], [0.5] * 8)
    _insert_history(conn, "53033000100", "hospital_capacity", [weeks[8]], [0.9], method="carry_forward")

    out = scoring.score_week(conn, "hospital_capacity", weeks[8])
    assert out.empty  # the only "observation" this week is carry_forward -> not scored at all


# ==================================================
# CORROBORATION
# ==================================================

def _insert_score(conn, tract_id, stream, week, z, direction, fallback=0, confidence=1.0):
    row = pd.DataFrame([{
        "tract_id": tract_id, "stream": stream, "week": week, "value": 0.0, "confidence": confidence,
        "baseline_median": 0.0, "baseline_mad": 0.0, "z_score": z, "p_value": 0.01,
        "direction": direction, "is_anomalous": 1, "baseline_source": "local",
        "fallback_baseline_used": fallback, "n_history_weeks": 10,
    }])
    db.upsert_rows(conn, row, table=db.SCORES_TABLE, columns=db.SCORES_COLUMNS)


def test_corroboration_fires_for_two_tract_resolved_streams_agreeing():
    conn = _memory_conn()
    _insert_score(conn, "53033000100", "hospital_capacity", "2026-03-01", 3.0, "elevated")
    _insert_score(conn, "53033000100", "wastewater_sars_cov2", "2026-03-01", 2.5, "elevated")

    alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")

    assert len(alerts) == 1
    row = alerts.iloc[0]
    assert row["tract_id"] == "53033000100"
    assert row["direction"] == "elevated"
    assert row["composite_score"] == pytest.approx((3.0 + 2.5) / 2)
    assert set(__import__("json").loads(row["corroborating_streams"])) == {"hospital_capacity", "wastewater_sars_cov2"}


def test_corroboration_does_not_fire_for_single_stream():
    conn = _memory_conn()
    _insert_score(conn, "53033000100", "hospital_capacity", "2026-03-01", 3.0, "elevated")

    alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")
    assert alerts.empty


def test_corroboration_does_not_fire_on_direction_disagreement():
    conn = _memory_conn()
    _insert_score(conn, "53033000100", "hospital_capacity", "2026-03-01", 3.0, "elevated")
    _insert_score(conn, "53033000100", "wastewater_sars_cov2", "2026-03-01", 2.5, "depressed")

    alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")
    assert alerts.empty


def test_corroboration_two_broadcast_streams_alone_do_not_trigger():
    conn = _memory_conn()
    cfg.STREAM_SCOPE["_test_broadcast_a"] = "broadcast"
    cfg.STREAM_SCOPE["_test_broadcast_b"] = "broadcast"
    try:
        _insert_score(conn, "53033000100", "_test_broadcast_a", "2026-03-01", 3.0, "elevated")
        _insert_score(conn, "53033000100", "_test_broadcast_b", "2026-03-01", 2.8, "elevated")

        alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")
        assert alerts.empty  # two broadcast streams is not enough on its own
    finally:
        del cfg.STREAM_SCOPE["_test_broadcast_a"]
        del cfg.STREAM_SCOPE["_test_broadcast_b"]


def test_corroboration_one_tract_resolved_plus_one_broadcast_triggers():
    conn = _memory_conn()
    cfg.STREAM_SCOPE["_test_broadcast_a"] = "broadcast"
    try:
        _insert_score(conn, "53033000100", "hospital_capacity", "2026-03-01", 3.0, "elevated")
        _insert_score(conn, "53033000100", "_test_broadcast_a", "2026-03-01", 2.8, "elevated")

        alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")
        assert len(alerts) == 1  # min_tract_resolved=1 is satisfied by hospital_capacity
    finally:
        del cfg.STREAM_SCOPE["_test_broadcast_a"]


def test_corroboration_unregistered_stream_defaults_to_broadcast_fail_safe():
    conn = _memory_conn()
    # "_test_unknown" is NOT in STREAM_SCOPE at all — should default to
    # broadcast (fail safe), so two unregistered streams alone don't alert.
    _insert_score(conn, "53033000100", "_test_unknown_a", "2026-03-01", 3.0, "elevated")
    _insert_score(conn, "53033000100", "_test_unknown_b", "2026-03-01", 2.8, "elevated")

    alerts = corroboration.build_alerts_for_week(conn, "2026-03-01")
    assert alerts.empty
