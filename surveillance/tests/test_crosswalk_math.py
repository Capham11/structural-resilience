"""
Offline unit tests for the surveillance crosswalk math and DB layer.

Live network pulls to HHS/WA DOH/HUD aren't reliably testable in review, so
these use small hand-built synthetic fixtures instead — a handful of
facilities/zips/tracts with numbers chosen so the expected result can be
computed by hand and asserted exactly.

Run with: pytest surveillance/tests/
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import pytest
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import geo_utils  # noqa: E402
import ingest_hospital_capacity as hosp  # noqa: E402
import ingest_vaccination_coverage as vax  # noqa: E402


# ==================================================
# IDW WEIGHTING (geo_utils)
# ==================================================

def test_idw_weights_favor_closer_points():
    weights = geo_utils.idw_weights(np.array([1000.0, 2000.0]), power=2.0)
    # inverse-square: weight ratio should be (2000/1000)^2 = 4
    assert weights[0] / weights[1] == pytest.approx(4.0)


def test_weighted_average_matches_hand_calc():
    values = np.array([0.5, 0.9])
    weights = np.array([3.0, 1.0])
    # (0.5*3 + 0.9*1) / 4 = 0.6
    assert geo_utils.weighted_average(values, weights) == pytest.approx(0.6)


def test_weighted_average_zero_weight_is_nan():
    assert np.isnan(geo_utils.weighted_average(np.array([0.5]), np.array([0.0])))


# ==================================================
# STREAM 1 — HOSPITAL DISTANCE-WEIGHTED CROSSWALK
# ==================================================

def _point_gdf(rows, crs=geo_utils.DISTANCE_CRS):
    return gpd.GeoDataFrame(
        rows, geometry=[Point(r["x"], r["y"]) for r in rows], crs=crs
    )


def test_hospital_crosswalk_weights_by_inverse_distance():
    # Tract at origin. One facility 1km away (occupancy 0.8), one 2km away (occupancy 0.4).
    centroids = _point_gdf([{"GEOID": "53033000100", "x": 0.0, "y": 0.0}])
    facilities = _point_gdf([
        {"x": 1000.0, "y": 0.0, "occupancy_rate": 0.8},
        {"x": 2000.0, "y": 0.0, "occupancy_rate": 0.4},
    ])

    out = hosp.crosswalk_week(centroids, facilities, "2026-01-04")

    assert len(out) == 1
    row = out.iloc[0]
    # IDW power=2: weights 1/1000^2 and 1/2000^2 -> ratio 4:1
    w1, w2 = 1 / 1000.0**2, 1 / 2000.0**2
    expected = (0.8 * w1 + 0.4 * w2) / (w1 + w2)
    assert row["value"] == pytest.approx(round(expected, 4))
    assert row["interpolation_method"] == "distance_weighted_idw"
    # 2 facilities in range, CONFIDENCE_REF_FACILITIES=3 -> confidence 2/3
    assert row["confidence"] == pytest.approx(round(2 / 3, 4))


def test_hospital_crosswalk_skips_tract_with_no_facility_in_range():
    centroids = _point_gdf([{"GEOID": "53033000100", "x": 0.0, "y": 0.0}])
    far_km = hosp.MAX_RADIUS_KM + 10
    facilities = _point_gdf([{"x": far_km * 1000, "y": 0.0, "occupancy_rate": 0.9}])

    out = hosp.crosswalk_week(centroids, facilities, "2026-01-04")
    assert out.empty


# ==================================================
# STREAM 2 — VACCINATION POPULATION-WEIGHTED CROSSWALK
# ==================================================

def test_vaccination_crosswalk_population_weighted():
    # Tract A is covered half by zip 1 (coverage 0.6) and half by zip 2 (coverage 1.0).
    crosswalk = pd.DataFrame([
        {"zip": "98001", "tract": "53033000100", "res_ratio": 0.5},
        {"zip": "98002", "tract": "53033000100", "res_ratio": 0.5},
    ])
    vax_week = pd.DataFrame([
        {"zip": "98001", "coverage_rate": 0.6},
        {"zip": "98002", "coverage_rate": 1.0},
    ])

    out = vax.crosswalk_week(crosswalk, vax_week, "2026-01-04")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["value"] == pytest.approx(0.8)  # (0.6*0.5 + 1.0*0.5) / 1.0
    assert row["confidence"] == pytest.approx(1.0)
    assert row["interpolation_method"] == "population_weighted_zip_tract"


def test_vaccination_crosswalk_partial_zip_coverage_lowers_confidence():
    # Tract B: only 30% of its residential ratio is covered by a zip we have data for.
    crosswalk = pd.DataFrame([
        {"zip": "98001", "tract": "53033000200", "res_ratio": 0.3},
        {"zip": "98099", "tract": "53033000200", "res_ratio": 0.7},  # no vax data this week
    ])
    vax_week = pd.DataFrame([{"zip": "98001", "coverage_rate": 0.5}])

    out = vax.crosswalk_week(crosswalk, vax_week, "2026-01-04")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["value"] == pytest.approx(0.5)
    assert row["confidence"] == pytest.approx(0.3)


# ==================================================
# DB — UPSERT + CARRY FORWARD
# ==================================================

def _memory_conn():
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    return conn


def test_carry_forward_fills_gap_with_decayed_confidence():
    conn = _memory_conn()

    week1 = pd.DataFrame([{
        "tract_id": "53033000100", "stream": "hospital_capacity", "week": "2026-01-04",
        "value": 0.7, "confidence": 1.0, "interpolation_method": "distance_weighted_idw",
    }])
    db.upsert_rows(conn, week1)

    carried = db.carry_forward_gaps(conn, "hospital_capacity", ["53033000100"], "2026-01-11")
    assert carried == 1

    row = pd.read_sql(f"SELECT * FROM {db.TABLE} WHERE week = '2026-01-11'", conn).iloc[0]
    assert row["value"] == pytest.approx(0.7)
    assert row["interpolation_method"] == "carry_forward"
    assert row["confidence"] == pytest.approx(db.CARRY_FORWARD_DECAY_PER_WEEK)


def test_carry_forward_never_overwrites_observed_row():
    conn = _memory_conn()

    week1 = pd.DataFrame([{
        "tract_id": "53033000100", "stream": "hospital_capacity", "week": "2026-01-04",
        "value": 0.7, "confidence": 1.0, "interpolation_method": "distance_weighted_idw",
    }])
    week2_observed = pd.DataFrame([{
        "tract_id": "53033000100", "stream": "hospital_capacity", "week": "2026-01-11",
        "value": 0.9, "confidence": 1.0, "interpolation_method": "distance_weighted_idw",
    }])
    db.upsert_rows(conn, week1)
    db.upsert_rows(conn, week2_observed)

    carried = db.carry_forward_gaps(conn, "hospital_capacity", ["53033000100"], "2026-01-11")
    assert carried == 0  # already has an observed row for that week

    row = pd.read_sql(f"SELECT * FROM {db.TABLE} WHERE week = '2026-01-11'", conn).iloc[0]
    assert row["value"] == pytest.approx(0.9)
    assert row["interpolation_method"] == "distance_weighted_idw"
