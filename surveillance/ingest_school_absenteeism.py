"""
Surveillance — Stream 5: School Absenteeism — SCAFFOLD ONLY, not live

This stream is deliberately NOT built to run against real data yet. Both
blockers below are data-access problems, not engineering ones — verified
directly, not assumed, the same way hospital capacity / vaccination
coverage's blockers were verified last session:

1. CADENCE: WA OSPI's public Report Card publishes attendance/chronic-
   absenteeism data in periodic releases tied to the school calendar
   (district CEDARS submissions), not a rolling weekly feed — I found no
   evidence anywhere in OSPI's data-reporting pages of a weekly-refreshed
   public download. State-level attendance reporting at this granularity is
   essentially never weekly anywhere; this isn't WA-specific pessimism, it
   matches how these systems are built nationally.

2. CATCHMENT BOUNDARIES ARE STALE, NOT JUST SLOW TO UPDATE: the only public
   national school-attendance-boundary source, NCES's School Attendance
   Boundary Survey (SABS), was an **experimental, two-cycle survey**
   (2013-14 and 2015-16 school years) that NCES discontinued after the
   2015-16 cycle — confirmed via NCES's own SABS documentation. Even if OSPI
   produced weekly counts tomorrow, the boundary geometry to crosswalk them
   with is now a decade-plus old. Schools open, close, and get redrawn over
   that span; this is a "the data source stopped being maintained" problem,
   not a "the feed lags a few weeks" one like hospital capacity.

Both of these point the same direction: **direct district outreach** (per
the original request) is the real path to a live version of this stream —
not something scriptable from public sources today.

WHAT'S BUILT ANYWAY
--------------------
The crosswalk logic and schema writer are real and tested, reusing
geo_utils.dasymetric_crosswalk() exactly like ingest_wastewater.py (same
underlying math: catchment polygon -> population-weighted tract
allocation, just school attendance boundaries instead of sewersheds) — so
that if a district-outreach effort produces weekly counts + current
boundaries, wiring them in is a data-loading change, not a rebuild. Input
is a local-file adapter (same convention as hospital capacity /
vaccination coverage), reshaped to:

    school_id      — any stable identifier for the school/catchment
    week           — ISO date, the week this row's numbers represent
    absence_rate   — fraction (0-1) of enrolled students absent that week

Catchment polygons: `data/raw/school_boundaries.geojson` with a
`catchment_id` column (matching `school_id` in the input CSV) — point this
at NCES SABS extract for WA if you want to see the crosswalk run at all
today, understanding its boundaries are 2015-16 vintage per the blocker
above; swap in real current boundaries from district outreach when
available, no code change needed.

Population weighting reuses the same census block population cache as
ingest_wastewater.py (see geo_utils.fetch_and_cache_block_population) —
build it once for whichever counties your school boundaries cover.
"""

from pathlib import Path

import pandas as pd
import geopandas as gpd

import db
import geo_utils

# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).parent
ABSENTEEISM_CSV = BASE_DIR / "data" / "raw" / "school_absenteeism.csv"
SCHOOL_BOUNDARIES_PATH = BASE_DIR / "data" / "raw" / "school_boundaries.geojson"
BLOCK_POPULATION_PATH = BASE_DIR / "data" / "raw" / "wa_block_population_king_snohomish_pierce.geojson"

STREAM = "school_absenteeism"


# ==================================================
# LOAD INPUTS (local-file adapter — see module docstring)
# ==================================================

def load_absenteeism(path: Path = ABSENTEEISM_CSV) -> pd.DataFrame:
    print(f"Loading school absenteeism data from {path}...")
    df = pd.read_csv(path, dtype={"school_id": str})

    required = {"school_id", "week", "absence_rate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input CSV missing required columns: {missing}")

    df["week"] = pd.to_datetime(df["week"]).dt.strftime("%Y-%m-%d")
    df["absence_rate"] = df["absence_rate"].clip(0, 1)
    df = df.dropna(subset=["absence_rate"])

    print(f"  Loaded {len(df)} school-week rows across {df['school_id'].nunique()} schools, "
          f"{df['week'].nunique()} week(s)")
    return df


def load_school_boundaries(path: Path = SCHOOL_BOUNDARIES_PATH) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. See module docstring — this stream has no wired-up "
            "input by design (real access blockers, not an engineering gap)."
        )
    print(f"Loading school attendance boundary polygons from {path}...")
    gdf = gpd.read_file(path)
    if "catchment_id" not in gdf.columns:
        raise ValueError(f"{path} missing required column: catchment_id")
    return gdf.to_crs(geo_utils.DISTANCE_CRS)


def load_block_population(path: Path = BLOCK_POPULATION_PATH) -> gpd.GeoDataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Build it once with geo_utils.fetch_and_cache_block_population() "
            "— see ingest_wastewater.py / geo_utils.py for the exact call."
        )
    print(f"Loading cached census block population from {path}...")
    return gpd.read_file(path).to_crs(geo_utils.DISTANCE_CRS)


# ==================================================
# CROSSWALK — POPULATION-WEIGHTED DASYMETRIC (same as wastewater)
# ==================================================

def crosswalk_week(boundaries: gpd.GeoDataFrame, blocks: gpd.GeoDataFrame,
                    absenteeism_week: pd.DataFrame, week: str) -> pd.DataFrame:
    matched = boundaries.merge(
        absenteeism_week.rename(columns={"school_id": "catchment_id", "absence_rate": "value"}),
        on="catchment_id", how="inner",
    )
    if matched.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    result = geo_utils.dasymetric_crosswalk(matched, blocks, value_col="value")
    if result.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    return pd.DataFrame({
        "tract_id": result["tract_id"],
        "stream": STREAM,
        "week": week,
        "value": result["value"].round(4),
        "confidence": result["confidence"].round(4),
        "interpolation_method": "population_weighted_dasymetric",
    })


# ==================================================
# MAIN
# ==================================================

def run(absenteeism_csv: Path = ABSENTEEISM_CSV, boundaries_path: Path = SCHOOL_BOUNDARIES_PATH) -> None:
    absenteeism = load_absenteeism(absenteeism_csv)
    boundaries = load_school_boundaries(boundaries_path)
    blocks = load_block_population()

    tracts = geo_utils.load_tracts()
    all_tract_ids = tracts["GEOID"].tolist()

    conn = db.connect()

    for week, week_df in absenteeism.groupby("week"):
        print(f"\nWeek {week}: crosswalking {len(week_df)} school rows to tracts...")
        out = crosswalk_week(boundaries, blocks, week_df, week)

        if out.empty:
            print("  No school matched a boundary polygon this week — skipping observed write")
        else:
            written = db.upsert_rows(conn, out)
            print(f"  Wrote {written} observed rows (mean confidence {out['confidence'].mean():.2f})")

        carried = db.carry_forward_gaps(conn, STREAM, all_tract_ids, week)
        if carried:
            print(f"  Carried forward {carried} tract(s) with no matched school this week")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.TABLE}, stream='{STREAM}')")


if __name__ == "__main__":
    print(__doc__)
    print("\nThis stream has no real input wired up (see the blockers above). "
          "Not running automatically — call run() with real files once you have them.")
