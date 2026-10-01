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
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import geo_utils  # noqa: E402
import ingest_hospital_capacity as hosp  # noqa: E402
import ingest_vaccination_coverage as vax  # noqa: E402
import ingest_air_quality as aq  # noqa: E402
import ingest_wastewater as ww  # noqa: E402


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


# ==================================================
# K-NEAREST IDW (geo_utils) — used by air quality
# ==================================================

def test_k_nearest_idw_uses_only_k_nearest():
    # 4 points; the 4th is a huge outlier that k=3 should exclude entirely.
    coords = np.array([[0.0, 0.0], [1000.0, 0.0], [2000.0, 0.0], [100_000.0, 0.0]])
    values = np.array([1.0, 2.0, 3.0, 1000.0])

    value, nearest_m, n_used = geo_utils.k_nearest_idw(coords, values, 0.0, 0.0, k=3)

    assert n_used == 3
    assert nearest_m == pytest.approx(0.0)
    assert value < 10.0  # nowhere near the excluded 1000.0 outlier


def test_k_nearest_idw_always_returns_a_value_no_radius_cutoff():
    # Unlike the hospital radius-cutoff pattern, a lone point 500km away
    # still produces a value — confidence is the caller's job, not a skip.
    coords = np.array([[500_000.0, 0.0]])
    values = np.array([42.0])

    value, nearest_m, n_used = geo_utils.k_nearest_idw(coords, values, 0.0, 0.0, k=5)

    assert n_used == 1
    assert value == pytest.approx(42.0)
    assert nearest_m == pytest.approx(500_000.0)


# ==================================================
# STREAM 3 — AIR QUALITY K-NEAREST IDW CROSSWALK
# ==================================================

def test_air_quality_confidence_full_within_good_distance():
    assert aq.compute_confidence(nearest_km=10.0, n_used=aq.MIN_K_FOR_FULL_CONFIDENCE) == pytest.approx(1.0)


def test_air_quality_confidence_decays_with_distance_but_never_silent():
    near = aq.compute_confidence(nearest_km=aq.MAX_GOOD_DISTANCE_KM, n_used=5)
    far = aq.compute_confidence(nearest_km=aq.MAX_GOOD_DISTANCE_KM * 5, n_used=5)
    assert far < near
    assert far > 0  # flagged low-confidence, never a silently-omitted value


def test_air_quality_crosswalk_produces_value_for_sparse_rural_tract():
    # A tract 200km from its nearest (only) monitor should still get a real
    # value — just at low confidence, per the "flag, don't silently skip" ask.
    centroids = _point_gdf([{"GEOID": "53001000100", "x": 0.0, "y": 0.0}])
    monitors = _point_gdf([{"x": 200_000.0, "y": 0.0, "mean": 12.5}])

    out = aq.crosswalk_week(centroids, monitors, "mean", aq.STREAM_MEAN, "2026-01-04")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["value"] == pytest.approx(12.5)
    assert row["stream"] == aq.STREAM_MEAN
    assert 0 < row["confidence"] < 1.0


# ==================================================
# DASYMETRIC CROSSWALK (geo_utils) — used by wastewater + school absenteeism
# ==================================================

def _block_gdf(rows):
    return gpd.GeoDataFrame(
        rows, geometry=[Point(r["x"], r["y"]) for r in rows], crs=geo_utils.DISTANCE_CRS
    )


def test_dasymetric_crosswalk_allocates_by_population_within_overlap():
    # Tract A: 2 blocks (pop 100 each); only one falls inside the catchment.
    # Tract B: 2 blocks (pop 100 + 300); only the 100-pop one falls inside.
    blocks = _block_gdf([
        {"GEOID": "53033000100001", "population": 100, "x": 1.0, "y": 1.0},   # inside catchment
        {"GEOID": "53033000100002", "population": 100, "x": 1.0, "y": 9.0},   # outside
        {"GEOID": "53033000200001", "population": 100, "x": 11.0, "y": 1.0},  # inside catchment
        {"GEOID": "53033000200002", "population": 300, "x": 11.0, "y": 9.0},  # outside
    ])
    catchments = gpd.GeoDataFrame(
        {"catchment_id": ["C1"], "value": [0.8]},
        geometry=[box(0, 0, 12, 5)], crs=geo_utils.DISTANCE_CRS,
    )

    result = geo_utils.dasymetric_crosswalk(catchments, blocks, value_col="value").set_index("tract_id")

    assert result.loc["53033000100", "value"] == pytest.approx(0.8)
    assert result.loc["53033000100", "confidence"] == pytest.approx(0.5)   # 100/200 covered
    assert result.loc["53033000200", "value"] == pytest.approx(0.8)
    assert result.loc["53033000200", "confidence"] == pytest.approx(0.25)  # 100/400 covered


def test_dasymetric_crosswalk_population_weights_multiple_catchments_in_one_tract():
    # One tract, fully covered by two different catchments with different values.
    blocks = _block_gdf([
        {"GEOID": "53033000300001", "population": 100, "x": 21.0, "y": 1.0},
        {"GEOID": "53033000300002", "population": 300, "x": 21.0, "y": 9.0},
    ])
    catchments = gpd.GeoDataFrame(
        {"catchment_id": ["C1", "C2"], "value": [0.8, 0.2]},
        geometry=[box(20, 0, 22, 2), box(20, 8, 22, 10)], crs=geo_utils.DISTANCE_CRS,
    )

    result = geo_utils.dasymetric_crosswalk(catchments, blocks, value_col="value").set_index("tract_id")

    # (0.8*100 + 0.2*300) / 400 = 0.35, fully covered -> confidence 1.0
    assert result.loc["53033000300", "value"] == pytest.approx(0.35)
    assert result.loc["53033000300", "confidence"] == pytest.approx(1.0)


def test_dasymetric_crosswalk_tract_with_no_overlap_is_absent_not_zero():
    blocks = _block_gdf([{"GEOID": "53033000400001", "population": 100, "x": 500.0, "y": 500.0}])
    catchments = gpd.GeoDataFrame(
        {"catchment_id": ["C1"], "value": [0.8]}, geometry=[box(0, 0, 1, 1)], crs=geo_utils.DISTANCE_CRS,
    )
    result = geo_utils.dasymetric_crosswalk(catchments, blocks, value_col="value")
    assert result.empty  # no row at all — caller/DB represents this as null, not 0


# ==================================================
# STREAM 4 — WASTEWATER: SCOPE FILTER + DASYMETRIC CROSSWALK
# ==================================================

def test_site_in_scope_matches_any_listed_county():
    assert ww.site_in_scope("53033, 53053") is True   # King + Pierce
    assert ww.site_in_scope("53061") is True           # Snohomish
    assert ww.site_in_scope("53063") is False          # Spokane — out of scope


def test_wastewater_crosswalk_week_end_to_end():
    sewersheds = gpd.GeoDataFrame(
        {"catchment_id": ["west_point"]}, geometry=[box(0, 0, 12, 5)], crs=geo_utils.DISTANCE_CRS,
    )
    crosswalk = pd.DataFrame([{"cdc_site_id": "2045", "catchment_id": "west_point"}])
    blocks = _block_gdf([
        {"GEOID": "53033000100001", "population": 100, "x": 1.0, "y": 1.0},
        {"GEOID": "53033000100002", "population": 100, "x": 1.0, "y": 9.0},
    ])
    weekly_values = pd.DataFrame([{"cdc_site_id": "2045", "week": "2026-09-13", "value": 9.5e8}])

    out = ww.crosswalk_week(sewersheds, crosswalk, blocks, weekly_values, "2026-09-13")

    assert len(out) == 1
    row = out.iloc[0]
    assert row["tract_id"] == "53033000100"
    assert row["value"] == pytest.approx(9.5e8)
    assert row["confidence"] == pytest.approx(0.5)  # only 1 of 2 blocks covered
    assert row["stream"] == ww.STREAM
    assert row["interpolation_method"] == "population_weighted_dasymetric"


def test_wastewater_crosswalk_week_no_match_returns_empty():
    sewersheds = gpd.GeoDataFrame(
        {"catchment_id": ["west_point"]}, geometry=[box(0, 0, 12, 5)], crs=geo_utils.DISTANCE_CRS,
    )
    crosswalk = pd.DataFrame([{"cdc_site_id": "2045", "catchment_id": "west_point"}])
    blocks = _block_gdf([{"GEOID": "53033000100001", "population": 100, "x": 1.0, "y": 1.0}])
    weekly_values = pd.DataFrame([{"cdc_site_id": "9999", "week": "2026-09-13", "value": 1.0}])

    out = ww.crosswalk_week(sewersheds, crosswalk, blocks, weekly_values, "2026-09-13")
    assert out.empty
