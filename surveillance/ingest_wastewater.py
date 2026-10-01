"""
Surveillance — Stream 4 (build order: 2nd): SCAN / WastewaterSCAN Wastewater
Washington State Census Tracts — King, Snohomish, Pierce counties only

Ingests SARS-CoV-2 wastewater concentrations for King/Snohomish/Pierce
treatment-plant catchments and crosswalks them to tracts via population-
weighted dasymetric mapping, then writes into the unified tract_timeseries
table (see db.py). Coverage is intentionally restricted to these three
counties — tracts elsewhere get no row at all (null by absence), not zero.

SOURCE — live pull, but verify the dataset ID before relying on this
------------------------------------------------------------------------
"SCAN" (WastewaterSCAN: Stanford/Emory/Verily) data is republished by CDC's
National Wastewater Surveillance System. I verified this LIVE while building
this script (2026-09-25), and it's worth restating because CDC's wastewater
datasets have already been renamed/restructured once (the funding for the
whole NWSS program lapsed Sept 30 2025, per public reporting, during a
transition to a new testing contract):

  - `g653-rqe2` ("NWSS Public SARS-CoV-2 Concentration in Wastewater Data")
    and `2ew6-ywp6` ("...Metric Data") are STALE — max date_end across every
    state, not just WA, was 2025-09-07 when checked live. Don't use these.
  - `j9g8-acpt` ("CDC Wastewater Data for SARS-CoV-2") is the CURRENT one —
    confirmed live with WA (King+Pierce, King+Snohomish, and Pierce-only
    sites) samples dated as recently as 2026-09-21 when checked. This is
    what CDC_DATASET_URL below points at. If this script starts returning
    only old dates, check https://data.cdc.gov for a renamed successor
    before assuming the crosswalk broke.

CROSSWALK — two real gaps, both handled as local files, not silently guessed
-----------------------------------------------------------------------------
1. CDC's site records have NO catchment polygon geometry — only an opaque
   `site` id, county(s) served, and self-reported population served. Real
   catchment polygons come from a separate source: EPA's National Sewershed
   Dataset (https://github.com/USEPA/Sewersheds, `Current_Release.zip` —
   confirmed downloadable, GeoJSON among the formats offered, but it's a
   ~127MB *nationwide* file, so this script does NOT fetch it automatically;
   extract the WA King/Snohomish/Pierce polygons once and save them to
   `data/raw/sewersheds.geojson` — a `catchment_id` column is required).
2. There's no clean automatic join between CDC's `site` id and EPA's
   sewershed polygon id — confirmed by inspecting both sources' actual
   fields (no shared identifier). For the small number of plants actually
   in scope (a handful across three counties), this needs a one-time manual
   match — `data/raw/wastewater_site_crosswalk.csv` (`cdc_site_id,
   catchment_id`), built by matching plant name/county/population_served
   between the two sources once. This is the same "small checked-in local
   extract instead of a fragile auto-join" pattern as last session's
   `backend/hub_distance.csv`.

Population weighting needs 2020 Census block population + geometry, cached
locally via geo_utils.fetch_and_cache_block_population() (also not run
automatically — see that function's docstring; another large one-time
download). See surveillance/README.md for the full one-time setup sequence.

VALUE IS NOT A 0-1 RATE, UNLIKE THE OTHER STREAMS
--------------------------------------------------
`value` here is CDC's flow-and-population-normalized concentration
(`pcr_target_flowpop_lin`, arbitrary large-magnitude units) — a real,
directly-comparable-across-sites signal, but not rescaled to 0-1 the way
hospital occupancy / vaccination coverage are. Rescaling it meaningfully
(e.g. against a historical baseline) is a scoring decision, out of scope
for "ingestion + crosswalk only."
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import requests

import db
import geo_utils

# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).parent
SEWERSHED_POLYGONS_PATH = BASE_DIR / "data" / "raw" / "sewersheds.geojson"
SITE_CROSSWALK_PATH = BASE_DIR / "data" / "raw" / "wastewater_site_crosswalk.csv"
BLOCK_POPULATION_PATH = BASE_DIR / "data" / "raw" / "wa_block_population_king_snohomish_pierce.geojson"

CDC_DATASET_URL = "https://data.cdc.gov/resource/j9g8-acpt.json"  # see module docstring before changing

STREAM = "wastewater_sars_cov2"

# King, Snohomish, Pierce — coverage is intentionally restricted to these.
IN_SCOPE_COUNTIES = {"53033": "King", "53061": "Snohomish", "53053": "Pierce"}
IN_SCOPE_COUNTY_FIPS_3DIGIT = ["033", "061", "053"]  # for TIGER's COUNTYFP20

LOOKBACK_WEEKS = 12  # process every distinct week found in this window


# ==================================================
# LOAD — LIVE CDC PULL
# ==================================================

def site_in_scope(county_fips_field: str) -> bool:
    codes = {c.strip() for c in str(county_fips_field).split(",")}
    return bool(codes & set(IN_SCOPE_COUNTIES))


def fetch_cdc_wastewater(lookback_weeks: int = LOOKBACK_WEEKS) -> pd.DataFrame:
    cutoff = (pd.Timestamp.today() - pd.Timedelta(weeks=lookback_weeks)).strftime("%Y-%m-%d")
    print(f"Fetching CDC wastewater data from {CDC_DATASET_URL} (since {cutoff})...")
    params = {
        "$where": f"state_territory='wa' AND pcr_target='sars-cov-2' AND sample_collect_date > '{cutoff}'",
        "$limit": 50000,
    }
    r = requests.get(CDC_DATASET_URL, params=params, timeout=120)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("  No rows returned — check CDC_DATASET_URL is still current (see module docstring)")
        return pd.DataFrame(columns=["site", "week", "value"])

    df = pd.DataFrame(rows)
    df = df[df["county_fips"].apply(site_in_scope)].copy()
    df["sample_collect_date"] = pd.to_datetime(df["sample_collect_date"])
    df["pcr_target_flowpop_lin"] = pd.to_numeric(df["pcr_target_flowpop_lin"], errors="coerce")
    df = df.dropna(subset=["pcr_target_flowpop_lin"])

    days_to_sunday = (6 - df["sample_collect_date"].dt.weekday) % 7
    df["week"] = (df["sample_collect_date"] + pd.to_timedelta(days_to_sunday, unit="D")).dt.strftime("%Y-%m-%d")

    print(f"  {len(df)} in-scope samples across {df['site'].nunique()} sites, "
          f"{df['week'].nunique()} week(s)")
    return df


def aggregate_weekly(samples: pd.DataFrame) -> pd.DataFrame:
    """One row per (site, week): mean flow-population-normalized concentration."""
    return (
        samples.groupby(["site", "week"])["pcr_target_flowpop_lin"]
        .mean().reset_index().rename(columns={"site": "cdc_site_id", "pcr_target_flowpop_lin": "value"})
    )


# ==================================================
# LOAD — LOCAL FILES (sewershed polygons, manual crosswalk, block population)
# ==================================================

def load_sewersheds(path: Path = SEWERSHED_POLYGONS_PATH) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Extract WA King/Snohomish/Pierce polygons once from "
            "EPA's National Sewershed Dataset (github.com/USEPA/Sewersheds, Current_Release.zip) "
            "and save them here with a `catchment_id` column — see module docstring."
        )
    print(f"Loading sewershed catchment polygons from {path}...")
    gdf = gpd.read_file(path)
    if "catchment_id" not in gdf.columns:
        raise ValueError(f"{path} missing required column: catchment_id")
    return gdf.to_crs(geo_utils.DISTANCE_CRS)


def load_site_crosswalk(path: Path = SITE_CROSSWALK_PATH) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Build the one-time manual cdc_site_id -> catchment_id "
            "mapping described in the module docstring."
        )
    print(f"Loading CDC site -> sewershed crosswalk from {path}...")
    df = pd.read_csv(path, dtype={"cdc_site_id": str, "catchment_id": str})
    required = {"cdc_site_id", "catchment_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Crosswalk CSV missing required columns: {missing}")
    return df


def load_block_population(path: Path = BLOCK_POPULATION_PATH) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Build it once with:\n"
            f"  geo_utils.fetch_and_cache_block_population('53', {IN_SCOPE_COUNTY_FIPS_3DIGIT!r}, "
            f"Path('{path}'))\n"
            "(downloads ~100MB+ of TIGER block geometry — see that function's docstring)."
        )
    print(f"Loading cached census block population from {path}...")
    return gpd.read_file(path).to_crs(geo_utils.DISTANCE_CRS)


# ==================================================
# CROSSWALK — POPULATION-WEIGHTED DASYMETRIC
# ==================================================

def crosswalk_week(sewersheds: gpd.GeoDataFrame, crosswalk: pd.DataFrame, blocks: gpd.GeoDataFrame,
                    weekly_values: pd.DataFrame, week: str) -> pd.DataFrame:
    week_values = weekly_values[weekly_values["week"] == week]
    matched = crosswalk.merge(week_values[["cdc_site_id", "value"]], on="cdc_site_id", how="inner")
    if matched.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    catchments = sewersheds.merge(matched[["catchment_id", "value"]], on="catchment_id", how="inner")
    if catchments.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    result = geo_utils.dasymetric_crosswalk(catchments, blocks, value_col="value")
    if result.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    out = pd.DataFrame({
        "tract_id": result["tract_id"],
        "stream": STREAM,
        "week": week,
        "value": result["value"].round(4),
        "confidence": result["confidence"].round(4),
        "interpolation_method": "population_weighted_dasymetric",
    })
    return out


# ==================================================
# MAIN
# ==================================================

def in_scope_tract_ids(tracts: gpd.GeoDataFrame) -> list:
    return tracts[tracts["COUNTYFP"].astype(str).isin(IN_SCOPE_COUNTY_FIPS_3DIGIT)]["GEOID"].tolist()


def run() -> None:
    sewersheds = load_sewersheds()
    crosswalk = load_site_crosswalk()
    blocks = load_block_population()

    samples = fetch_cdc_wastewater()
    if samples.empty:
        print("No in-scope samples fetched — nothing to do.")
        return
    weekly_values = aggregate_weekly(samples)

    tracts = geo_utils.load_tracts()
    scoped_tract_ids = in_scope_tract_ids(tracts)
    print(f"\n{len(scoped_tract_ids)} tracts in scope (King/Snohomish/Pierce)")

    conn = db.connect()

    for week in sorted(weekly_values["week"].unique()):
        print(f"\nWeek {week}: crosswalking wastewater sites to tracts...")
        out = crosswalk_week(sewersheds, crosswalk, blocks, weekly_values, week)

        if out.empty:
            print("  No matched sewershed data this week — skipping observed write")
        else:
            written = db.upsert_rows(conn, out)
            print(f"  Wrote {written} observed rows (mean confidence {out['confidence'].mean():.2f})")

        # Carry-forward is scoped to King/Snohomish/Pierce tracts ONLY — tracts
        # outside this coverage area never get a row for this stream at all,
        # which is genuine SQL-row absence (null), not a zero.
        carried = db.carry_forward_gaps(conn, STREAM, scoped_tract_ids, week)
        if carried:
            print(f"  Carried forward {carried} in-scope tract(s) with no matched sewershed this week")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.TABLE}, stream='{STREAM}')")


if __name__ == "__main__":
    run()
