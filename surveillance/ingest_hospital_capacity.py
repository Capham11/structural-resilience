"""
Surveillance — Stream 1: Hospital Capacity
Washington State Census Tracts

Ingests facility-level hospital bed capacity and crosswalks it to tracts by
distance-decay weighting across nearby facilities, then writes it into the
unified tract_timeseries table (see db.py).

INPUT — local-file adapter, not a live API pull
------------------------------------------------
As of this writing (2026), there is no open, anonymously-scriptable
facility-level hospital capacity feed for WA:
  - HHS/healthdata.gov's facility-level file was discontinued May 3, 2024 and
    hasn't been updated since (federal facility-level reporting ended).
  - Its successor, CDC/NHSN's Weekly Hospital Respiratory Data, is public
    only at state/jurisdiction level — no facility identity or coordinates.
  - WA's own facility-granular system (WA HEALTH) is real-time but gated to
    health care / emergency-preparedness partners (request access via
    wahealth@doh.wa.gov) — not an anonymous pull.

So this script reads a local CSV instead of hitting a URL. Point
INPUT_CSV at whatever facility-level export you have (the frozen 2024 HHS
file, a WA HEALTH partner export, etc.) reshaped to these columns:

    facility_id    — any stable identifier for the facility
    name           — facility name (for QA/debugging only)
    latitude       — decimal degrees (WGS84)
    longitude      — decimal degrees (WGS84)
    week           — ISO date, the week this row's numbers represent
    beds_staffed   — staffed inpatient beds (7-day average or snapshot)
    beds_used      — occupied staffed inpatient beds (same window)

Multiple weeks may be stacked in one file (e.g. a historical backfill) — the
script processes every distinct week present and is safe to re-run.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

import db
import geo_utils

# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).parent
INPUT_CSV = BASE_DIR / "data" / "raw" / "hospital_capacity_facilities.csv"

STREAM = "hospital_capacity"

MAX_RADIUS_KM = 40.0          # facilities beyond this don't influence a tract
IDW_POWER = 2.0                # inverse-distance-squared decay
MIN_DISTANCE_M = 100.0         # floor to avoid divide-by-zero for on-top-of-tract facilities

# Confidence scales with how many facilities are actually in range —
# CONFIDENCE_REF_FACILITIES facilities in radius is treated as "full" coverage.
CONFIDENCE_REF_FACILITIES = 3


# ==================================================
# LOAD FACILITY DATA
# ==================================================

def load_facilities(path: Path) -> pd.DataFrame:
    print(f"Loading facility capacity data from {path}...")
    df = pd.read_csv(path, dtype={"facility_id": str})

    required = {"facility_id", "name", "latitude", "longitude", "week",
                "beds_staffed", "beds_used"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input CSV missing required columns: {missing}")

    df["week"] = pd.to_datetime(df["week"]).dt.strftime("%Y-%m-%d")
    df["occupancy_rate"] = (df["beds_used"] / df["beds_staffed"].replace(0, np.nan)).clip(0, 1)
    df = df.dropna(subset=["latitude", "longitude", "occupancy_rate"])

    print(f"  Loaded {len(df)} facility-week rows across {df['facility_id'].nunique()} facilities, "
          f"{df['week'].nunique()} week(s)")
    return df


def facilities_to_points(df: pd.DataFrame) -> gpd.GeoDataFrame:
    geometry = [Point(lon, lat) for lon, lat in zip(df["longitude"], df["latitude"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    return geo_utils.to_projected_points(gdf)


# ==================================================
# CROSSWALK — DISTANCE-WEIGHTED FACILITY -> TRACT
# ==================================================

def crosswalk_week(centroids: gpd.GeoDataFrame, facilities_week: gpd.GeoDataFrame, week: str) -> pd.DataFrame:
    """
    For each tract centroid, average nearby facilities' occupancy_rate,
    weighted by inverse distance. Returns rows only for tracts with at least
    one facility within MAX_RADIUS_KM (others are left for carry_forward).
    """
    max_radius_m = MAX_RADIUS_KM * 1000

    fac_coords = np.column_stack([facilities_week.geometry.x, facilities_week.geometry.y])
    fac_values = facilities_week["occupancy_rate"].to_numpy()

    rows = []
    for _, tract in centroids.iterrows():
        dx = fac_coords[:, 0] - tract.geometry.x
        dy = fac_coords[:, 1] - tract.geometry.y
        dist_m = np.sqrt(dx**2 + dy**2)

        in_range = dist_m <= max_radius_m
        n_in_range = int(in_range.sum())
        if n_in_range == 0:
            continue

        weights = geo_utils.idw_weights(dist_m[in_range], power=IDW_POWER, min_distance_m=MIN_DISTANCE_M)
        value = geo_utils.weighted_average(fac_values[in_range], weights)
        confidence = min(1.0, n_in_range / CONFIDENCE_REF_FACILITIES)

        rows.append({
            "tract_id": tract["GEOID"],
            "stream": STREAM,
            "week": week,
            "value": round(value, 4),
            "confidence": round(confidence, 4),
            "interpolation_method": "distance_weighted_idw",
        })

    return pd.DataFrame(rows)


# ==================================================
# MAIN
# ==================================================

def run(input_csv: Path = INPUT_CSV) -> None:
    facilities = load_facilities(input_csv)

    tracts = geo_utils.load_tracts()
    centroids = geo_utils.tract_centroids(tracts)
    all_tract_ids = centroids["GEOID"].tolist()

    conn = db.connect()

    for week, week_df in facilities.groupby("week"):
        print(f"\nWeek {week}: crosswalking {len(week_df)} facility rows to {len(centroids)} tracts...")
        facilities_week = facilities_to_points(week_df)
        out = crosswalk_week(centroids, facilities_week, week)

        if out.empty:
            print(f"  No tract matched any facility within {MAX_RADIUS_KM} km — skipping observed write")
        else:
            written = db.upsert_rows(conn, out)
            print(f"  Wrote {written} observed rows "
                  f"(mean confidence {out['confidence'].mean():.2f})")

        carried = db.carry_forward_gaps(conn, STREAM, all_tract_ids, week)
        if carried:
            print(f"  Carried forward {carried} tract(s) with no facility in range / no data this week")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.TABLE}, stream='{STREAM}')")


if __name__ == "__main__":
    run()
