"""
Surveillance — Stream 3 (build order: 1st): EPA AirNow Air Quality
Washington State Census Tracts

Ingests PM2.5 monitor readings from EPA's AirNow network and crosswalks them
to tracts via k-nearest-neighbor inverse-distance weighting, then writes
weekly mean AND weekly max into the unified tract_timeseries table (see
db.py) as two separate streams (the schema has one `value` column per row,
so mean/max become two `stream` values rather than a schema change).

LIVE PULL — unlike hospital capacity / vaccination coverage, this one is a
genuine live API pull, not a local-file adapter:

  - The monitor location reference file is public with no key at all:
    https://files.airnowtech.org/airnow/today/monitoring_site_locations.dat
    (pipe-delimited; confirmed reachable — verified live 2026-09-25, 148 WA
    PM2.5-tagged rows).
  - Actual readings need a free API key from https://docs.airnowapi.org
    (sign up, no cost) set as the AIRNOW_API_KEY environment variable —
    never hardcode it (unlike the leaked CENSUS_API_KEY in
    outbreak_model/phase2_vulnerability_pull.py, which is a separate,
    pre-existing issue worth rotating).

The bbox observations endpoint (AIRNOW_DATA_URL below) and its exact
parameter/response shape are documented at docs.airnowapi.org behind a
login wall I don't have access to from here — the request below follows the
well-established, widely-referenced AirNow bbox query shape (startDate/
endDate/parameters/BBOX/dataType/format/verbose/monitorType/
includerawconcentrations/API_KEY), but verify the response column order
against your own account's docs on first real run; fetch_observations()
raises a clear error naming the mismatch if the response doesn't parse as
expected rather than silently misreading columns.

CROSSWALK — k-nearest IDW, not a radius cutoff
-----------------------------------------------
Unlike hospital capacity (facilities beyond MAX_RADIUS_KM are skipped
entirely and left to carry_forward), air quality genuinely still carries
signal at longer range — sparse eastern-WA tracts should get a real
interpolated value, just at low confidence, never a silent guess presented
as equal-quality to a tract next to three monitors. See
geo_utils.k_nearest_idw() and compute_confidence() below.
"""

import os
import io
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import requests
from shapely.geometry import Point

import db
import geo_utils

# ==================================================
# CONFIG
# ==================================================

MONITOR_LOCATIONS_URL = "https://files.airnowtech.org/airnow/today/monitoring_site_locations.dat"
AIRNOW_DATA_URL = "https://www.airnowapi.org/aq/data/"

PARAMETER = "PM2.5"

# WA bounding box + buffer so near-border ID/OR monitors that legitimately
# influence eastern/southern WA tracts are included in the k-nearest search.
WA_BBOX = (-124.85, 45.4, -116.9, 49.1)  # minLon, minLat, maxLon, maxLat
BBOX_BUFFER_DEG = 1.0

K_NEAREST = 5                  # up to 5 nearest monitors per tract
MIN_K_FOR_FULL_CONFIDENCE = 3  # fewer than this in range dents confidence
IDW_POWER = 2.0
MIN_DISTANCE_M = 500.0

MAX_GOOD_DISTANCE_KM = 40.0    # full distance-confidence within this range
CONFIDENCE_FLOOR = 0.15        # never fully zero out a real interpolation

STREAM_MEAN = "air_quality_pm25_mean"
STREAM_MAX = "air_quality_pm25_max"

MONITOR_LOCATIONS_COLUMNS = [
    "AQSID", "Parameter", "SiteCode", "SiteName", "Status", "Agency", "AgencyName",
    "EPARegion", "Latitude", "Longitude", "Elevation", "GMTOffset", "CountryCode",
    "_blank1", "_blank2", "CBSA_ID", "CBSA_Name", "StateAQSCode", "StateAbbrev",
    "CountyAQSCode", "CountyName", "_blank3", "_blank4",
]

# Best-known shape of the bbox observations response (verbose=1, dataType=C).
# Verify against your account's docs.airnowapi.org on first real run.
OBSERVATIONS_COLUMNS = [
    "Latitude", "Longitude", "UTC", "Parameter", "Concentration", "Unit",
    "RawConcentration", "AQI", "Category", "SiteName", "SiteAgency", "AQSID", "FullAQSID",
]


# ==================================================
# WEEK HELPERS
# ==================================================

def default_last_sunday() -> str:
    """Most recently completed week-ending Sunday (today if today is Sunday)."""
    today = date.today()
    offset = (today.weekday() - 6) % 7  # Monday=0 ... Sunday=6
    return (today - timedelta(days=offset)).isoformat()


def week_bounds(week_ending_sunday: str) -> tuple[str, str]:
    end = date.fromisoformat(week_ending_sunday)
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


# ==================================================
# LOAD MONITORS
# ==================================================

def load_monitors(bbox=WA_BBOX, buffer_deg=BBOX_BUFFER_DEG) -> pd.DataFrame:
    print(f"Loading AirNow monitor locations from {MONITOR_LOCATIONS_URL}...")
    r = requests.get(MONITOR_LOCATIONS_URL, timeout=60)
    r.raise_for_status()

    df = pd.read_csv(
        io.StringIO(r.text), sep="|", header=None, names=MONITOR_LOCATIONS_COLUMNS,
        dtype=str, engine="python",
    )
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")

    min_lon, min_lat, max_lon, max_lat = bbox
    in_bbox = (
        df["Latitude"].between(min_lat - buffer_deg, max_lat + buffer_deg) &
        df["Longitude"].between(min_lon - buffer_deg, max_lon + buffer_deg)
    )
    monitors = df[
        (df["Parameter"] == PARAMETER) & (df["Status"] == "Active") & in_bbox
    ].drop_duplicates(subset="AQSID").copy()

    print(f"  {len(monitors)} active {PARAMETER} monitors in WA + {buffer_deg}° buffer")
    return monitors


def monitors_to_points(monitors: pd.DataFrame) -> gpd.GeoDataFrame:
    geometry = [Point(lon, lat) for lon, lat in zip(monitors["Longitude"], monitors["Latitude"])]
    gdf = gpd.GeoDataFrame(monitors, geometry=geometry, crs="EPSG:4326")
    return geo_utils.to_projected_points(gdf)


# ==================================================
# LOAD OBSERVATIONS (live pull)
# ==================================================

def fetch_observations(bbox, week_start: str, week_end: str, api_key: str,
                        buffer_deg=BBOX_BUFFER_DEG) -> pd.DataFrame:
    min_lon, min_lat, max_lon, max_lat = bbox
    params = {
        "startDate": f"{week_start}T00",
        "endDate": f"{week_end}T23",
        "parameters": "PM25",
        "BBOX": f"{min_lon - buffer_deg},{min_lat - buffer_deg},{max_lon + buffer_deg},{max_lat + buffer_deg}",
        "dataType": "C",
        "format": "text/csv",
        "verbose": "1",
        "monitorType": "2",
        "includerawconcentrations": "1",
        "API_KEY": api_key,
    }
    print(f"Fetching AirNow PM2.5 observations {week_start}..{week_end}...")
    r = requests.get(AIRNOW_DATA_URL, params=params, timeout=180)
    r.raise_for_status()

    try:
        df = pd.read_csv(io.StringIO(r.text), header=None, names=OBSERVATIONS_COLUMNS)
        df["Concentration"] = pd.to_numeric(df["Concentration"], errors="raise")
    except Exception as e:
        raise RuntimeError(
            "AirNow /aq/data/ response didn't parse as expected — the column "
            "shape here (OBSERVATIONS_COLUMNS) is our best-documented guess, "
            "verify it against your account's docs.airnowapi.org and adjust. "
            f"First 300 chars of response: {r.text[:300]!r}"
        ) from e

    df = df.dropna(subset=["Concentration", "AQSID"])
    print(f"  {len(df)} hourly readings across {df['AQSID'].nunique()} monitors")
    return df


def aggregate_weekly(observations: pd.DataFrame) -> pd.DataFrame:
    """One row per monitor: weekly mean and weekly max concentration."""
    agg = observations.groupby("AQSID")["Concentration"].agg(mean="mean", max="max").reset_index()
    return agg


# ==================================================
# CROSSWALK — K-NEAREST IDW
# ==================================================

def compute_confidence(nearest_km: float, n_used: int) -> float:
    if nearest_km <= MAX_GOOD_DISTANCE_KM:
        dist_conf = 1.0
    else:
        dist_conf = max(CONFIDENCE_FLOOR, MAX_GOOD_DISTANCE_KM / nearest_km)
    count_conf = min(1.0, n_used / MIN_K_FOR_FULL_CONFIDENCE)
    return round(dist_conf * count_conf, 4)


def crosswalk_week(centroids: gpd.GeoDataFrame, monitor_points: gpd.GeoDataFrame,
                    metric_col: str, stream: str, week: str) -> pd.DataFrame:
    coords = np.column_stack([monitor_points.geometry.x, monitor_points.geometry.y])
    values = monitor_points[metric_col].to_numpy()

    rows = []
    for _, tract in centroids.iterrows():
        value, nearest_m, n_used = geo_utils.k_nearest_idw(
            coords, values, tract.geometry.x, tract.geometry.y,
            k=K_NEAREST, power=IDW_POWER, min_distance_m=MIN_DISTANCE_M,
        )
        if n_used == 0:
            continue
        confidence = compute_confidence(nearest_m / 1000.0, n_used)
        rows.append({
            "tract_id": tract["GEOID"],
            "stream": stream,
            "week": week,
            "value": round(value, 3),
            "confidence": confidence,
            "interpolation_method": "distance_weighted_idw",
        })
    return pd.DataFrame(rows)


# ==================================================
# MAIN
# ==================================================

def run(week: str = None, api_key: str = None) -> None:
    week = week or default_last_sunday()
    api_key = api_key or os.environ.get("AIRNOW_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AIRNOW_API_KEY not set. Sign up for a free key at "
            "https://docs.airnowapi.org and set it as an environment variable "
            "(never hardcode it) — export AIRNOW_API_KEY=... or pass api_key=."
        )

    week_start, week_end = week_bounds(week)

    monitors = load_monitors()
    monitor_points = monitors_to_points(monitors)

    observations = fetch_observations(WA_BBOX, week_start, week_end, api_key)
    weekly = aggregate_weekly(observations)

    merged = monitor_points.merge(weekly, on="AQSID", how="inner")
    print(f"  {len(merged)}/{len(monitor_points)} monitors have data for week {week}")

    tracts = geo_utils.load_tracts()
    centroids = geo_utils.tract_centroids(tracts)
    all_tract_ids = centroids["GEOID"].tolist()

    conn = db.connect()

    for metric_col, stream in [("mean", STREAM_MEAN), ("max", STREAM_MAX)]:
        print(f"\nWeek {week} ({stream}): crosswalking {len(merged)} monitors to {len(centroids)} tracts...")
        out = crosswalk_week(centroids, merged, metric_col, stream, week)

        if out.empty:
            print("  No monitor data available — skipping observed write")
        else:
            written = db.upsert_rows(conn, out)
            print(f"  Wrote {written} observed rows (mean confidence {out['confidence'].mean():.2f})")

        carried = db.carry_forward_gaps(conn, stream, all_tract_ids, week)
        if carried:
            print(f"  Carried forward {carried} tract(s) with no data this week")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.TABLE}, streams='{STREAM_MEAN}'/'{STREAM_MAX}')")


if __name__ == "__main__":
    run()
