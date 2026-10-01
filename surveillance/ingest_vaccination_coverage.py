"""
Surveillance — Stream 2: Vaccination Coverage
Washington State Census Tracts

Ingests zip-level vaccination coverage and crosswalks it to tracts using
HUD's population-weighted ZIP-TRACT crosswalk (RES_RATIO), then writes it
into the unified tract_timeseries table (see db.py).

INPUT — local-file adapter, not a live API pull
------------------------------------------------
As of this writing (2026), WA DOH's public vaccination data stops at
county/state level — zip-level coverage isn't in their open catalog or on
data.wa.gov; DOH directs zip-level requests to WAIISDataRequests@doh.wa.gov
as an ad hoc data pull, not a scriptable feed.

So this script reads a local CSV instead of hitting a URL. Point
VACCINATION_CSV at a zip-level export reshaped to these columns:

    zip            — 5-digit ZIP code (string, zero-padded)
    week           — ISO date, the week this row's numbers represent
    coverage_rate  — fraction (0-1) of the zip's population vaccinated /
                     up to date, however WA DOH defines it in your export

Multiple weeks may be stacked in one file — every distinct week is processed.

ZIP -> TRACT CROSSWALK
----------------------
Uses HUD's quarterly USPS ZIP-TRACT crosswalk (RES_RATIO = share of a zip's
residential addresses that fall in a given tract). HUD's crosswalk API/file
downloads require a free huduser.gov account; rather than hardcode API
mechanics that may drift, this script reads a cached local crosswalk CSV by
default:

    zip            — 5-digit ZIP code (string, zero-padded)
    tract          — 11-digit GEOID
    res_ratio      — residential ratio, 0-1

Download it once from https://www.huduser.gov/portal/datasets/usps_crosswalk.html
(ZIP-TRACT, WA / all states, most recent quarter) and save it to
surveillance/data/raw/zip_tract_crosswalk.csv. It changes slowly (quarterly)
so it's fine to reuse for months.
"""

from pathlib import Path

import pandas as pd

import db
import geo_utils

# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).parent
VACCINATION_CSV = BASE_DIR / "data" / "raw" / "vaccination_by_zip.csv"
CROSSWALK_CSV = BASE_DIR / "data" / "raw" / "zip_tract_crosswalk.csv"

STREAM = "vaccination_coverage"


# ==================================================
# LOAD INPUTS
# ==================================================

def load_vaccination(path: Path) -> pd.DataFrame:
    print(f"Loading vaccination coverage data from {path}...")
    df = pd.read_csv(path, dtype={"zip": str})
    df["zip"] = df["zip"].str.zfill(5)

    required = {"zip", "week", "coverage_rate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input CSV missing required columns: {missing}")

    df["week"] = pd.to_datetime(df["week"]).dt.strftime("%Y-%m-%d")
    df["coverage_rate"] = df["coverage_rate"].clip(0, 1)
    df = df.dropna(subset=["coverage_rate"])

    print(f"  Loaded {len(df)} zip-week rows across {df['zip'].nunique()} zips, "
          f"{df['week'].nunique()} week(s)")
    return df


def load_crosswalk(path: Path) -> pd.DataFrame:
    print(f"Loading ZIP-TRACT crosswalk from {path}...")
    df = pd.read_csv(path, dtype={"zip": str, "tract": str})
    df["zip"] = df["zip"].str.zfill(5)
    df["tract"] = df["tract"].str.zfill(11)

    required = {"zip", "tract", "res_ratio"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Crosswalk CSV missing required columns: {missing}")

    print(f"  Loaded {len(df)} zip-tract crosswalk rows "
          f"({df['zip'].nunique()} zips, {df['tract'].nunique()} tracts)")
    return df


# ==================================================
# CROSSWALK — POPULATION-WEIGHTED ZIP -> TRACT
# ==================================================

def crosswalk_week(crosswalk: pd.DataFrame, vax_week: pd.DataFrame, week: str) -> pd.DataFrame:
    """
    For each tract, population-weight the coverage_rate of every zip that
    overlaps it by that zip's RES_RATIO into the tract, restricted to zips
    we actually have data for this week.
    """
    matched = crosswalk.merge(vax_week[["zip", "coverage_rate"]], on="zip", how="inner")
    if matched.empty:
        return pd.DataFrame(columns=["tract_id", "stream", "week", "value", "confidence", "interpolation_method"])

    matched["weighted_value"] = matched["coverage_rate"] * matched["res_ratio"]

    grouped = matched.groupby("tract").agg(
        weighted_sum=("weighted_value", "sum"),
        weight_sum=("res_ratio", "sum"),
    ).reset_index()

    grouped["value"] = grouped["weighted_sum"] / grouped["weight_sum"]
    grouped["confidence"] = grouped["weight_sum"].clip(upper=1.0)

    out = pd.DataFrame({
        "tract_id": grouped["tract"],
        "stream": STREAM,
        "week": week,
        "value": grouped["value"].round(4),
        "confidence": grouped["confidence"].round(4),
        "interpolation_method": "population_weighted_zip_tract",
    })
    return out


# ==================================================
# MAIN
# ==================================================

def run(vaccination_csv: Path = VACCINATION_CSV, crosswalk_csv: Path = CROSSWALK_CSV) -> None:
    vax = load_vaccination(vaccination_csv)
    crosswalk = load_crosswalk(crosswalk_csv)

    tracts = geo_utils.load_tracts()
    all_tract_ids = tracts["GEOID"].tolist()

    conn = db.connect()

    for week, vax_week in vax.groupby("week"):
        print(f"\nWeek {week}: crosswalking {len(vax_week)} zip rows to tracts...")
        out = crosswalk_week(crosswalk, vax_week, week)

        if out.empty:
            print("  No zip in this week's data matched the crosswalk — skipping observed write")
        else:
            written = db.upsert_rows(conn, out)
            print(f"  Wrote {written} observed rows "
                  f"(mean confidence {out['confidence'].mean():.2f})")

        carried = db.carry_forward_gaps(conn, STREAM, all_tract_ids, week)
        if carried:
            print(f"  Carried forward {carried} tract(s) with no matched zip data this week")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.TABLE}, stream='{STREAM}')")


if __name__ == "__main__":
    run()
