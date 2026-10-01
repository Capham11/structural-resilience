"""
Surveillance — Shared Geo Helpers
Washington State Census Tracts

Shared by ingest_hospital_capacity.py and ingest_vaccination_coverage.py:
- loading tract geometry the same way phase2_vulnerability_pull.py does
- projecting to a distance-accurate CRS before any radius/IDW math
- inverse-distance-decay weighting

Follows the conventions of outbreak_model/phase2_vulnerability_pull.py:
GEOID zero-padded to 11 digits as the tract join key, CONFIG constants up top.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import requests

# ==================================================
# CONFIG
# ==================================================

REPO_ROOT = Path(__file__).parent.parent
TRACTS_PATH = REPO_ROOT / "outbreak_model" / "data" / "washington_base_structural_resilience.geojson"

# NAD83 / Conus Albers (meters) — equal-area, accurate for statewide distance
# math across all of WA. A single UTM zone would distort tracts near the
# UTM 10N/11N seam that splits the state roughly through the Cascades.
DISTANCE_CRS = "EPSG:5070"


# ==================================================
# TRACT LOADING
# ==================================================

def load_tracts() -> gpd.GeoDataFrame:
    """
    Load WA tract polygons and normalize GEOID the same way
    phase2_vulnerability_pull.py / merge_census.py do (zero-padded 11-digit string).

    Returns a GeoDataFrame in the source CRS (EPSG:4269) with at least
    GEOID, COUNTYFP, geometry.
    """
    tracts = gpd.read_file(TRACTS_PATH)
    tracts["GEOID"] = tracts["GEOID"].astype(str).str.zfill(11)
    return tracts


def tract_centroids(tracts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Project tracts to DISTANCE_CRS and return one row per tract with a
    centroid point geometry, for use as the "tract location" in distance
    calculations against facility/zip points.
    """
    projected = tracts.to_crs(DISTANCE_CRS)
    out = projected[["GEOID"]].copy()
    out["geometry"] = projected.geometry.centroid
    return gpd.GeoDataFrame(out, geometry="geometry", crs=DISTANCE_CRS)


def to_projected_points(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject an arbitrary point GeoDataFrame to DISTANCE_CRS."""
    return gdf.to_crs(DISTANCE_CRS)


# ==================================================
# INVERSE-DISTANCE WEIGHTING
# ==================================================

def idw_weights(distances_m: np.ndarray, power: float = 2.0, min_distance_m: float = 100.0) -> np.ndarray:
    """
    Inverse-distance-decay weights. `min_distance_m` floors very small
    distances so a facility essentially on top of a tract centroid doesn't
    produce a divide-by-zero / infinite weight.
    """
    safe = np.maximum(distances_m, min_distance_m)
    return 1.0 / np.power(safe, power)


def weighted_average(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted mean, guarding against an all-zero weight vector."""
    total_weight = np.sum(weights)
    if total_weight <= 0:
        return float("nan")
    return float(np.sum(values * weights) / total_weight)


def k_nearest_idw(coords: np.ndarray, values: np.ndarray, target_x: float, target_y: float,
                   k: int = 5, power: float = 2.0, min_distance_m: float = 100.0):
    """
    IDW-interpolate `values` at (target_x, target_y) using the k nearest of
    `coords` — no radius cutoff. Unlike the radius-restricted pattern in
    ingest_hospital_capacity.py (where "too far" means "no signal, skip and
    carry forward"), this is for signals that genuinely still carry
    information at long range (e.g. regional air quality) — it always
    returns a value if any points exist, and leaves it to the caller to
    turn `nearest_distance_m` into a confidence penalty rather than silently
    treating a far interpolation as equal to a near one.

    coords: (N,2) array of projected x,y. values: (N,) array.
    Returns (value, nearest_distance_m, n_used).
    """
    if len(coords) == 0:
        return float("nan"), float("inf"), 0
    dx = coords[:, 0] - target_x
    dy = coords[:, 1] - target_y
    dist = np.sqrt(dx**2 + dy**2)
    k_eff = min(k, len(dist))
    idx = np.argpartition(dist, k_eff - 1)[:k_eff]
    nearest_dist = dist[idx]
    weights = idw_weights(nearest_dist, power=power, min_distance_m=min_distance_m)
    value = weighted_average(values[idx], weights)
    return value, float(nearest_dist.min()), k_eff


def dasymetric_crosswalk(catchments: gpd.GeoDataFrame, population_blocks: gpd.GeoDataFrame,
                          value_col: str = "value") -> pd.DataFrame:
    """
    Population-weighted dasymetric crosswalk: allocate each catchment
    polygon's value to census tracts proportional to the population living
    in the tract that also falls inside that catchment.

    Simplification (documented, not silent): a block is assigned entirely to
    whichever catchment contains its *centroid* (areal apportionment by
    centroid containment), not a full area-weighted polygon overlay. Blocks
    are small relative to tracts and catchments, so this is the standard,
    much-simpler dasymetric approximation — worth knowing if a block sits
    exactly on a catchment boundary.

    catchments: GeoDataFrame with a `catchment_id`, `value_col`, and geometry
      (already projected to geo_utils.DISTANCE_CRS).
    population_blocks: GeoDataFrame with `GEOID` (15-digit block id — its
      first 11 digits ARE the parent tract's GEOID, since blocks nest
      strictly inside tracts in Census geography, so no separate block-tract
      join is needed), `population`, and geometry (same CRS).

    Returns a DataFrame with columns: tract_id, value, confidence
    (confidence = fraction of the tract's total population that falls inside
    any catchment covered by this crosswalk — tracts with no overlap at all
    are simply absent from the result, left for the caller to treat as null).
    """
    blocks = population_blocks.copy()
    blocks["tract_id"] = blocks["GEOID"].str.slice(0, 11)
    blocks["centroid"] = blocks.geometry.centroid

    centroid_pts = gpd.GeoDataFrame(
        blocks[["tract_id", "population"]], geometry=blocks["centroid"], crs=population_blocks.crs
    )
    joined = gpd.sjoin(centroid_pts, catchments[["catchment_id", value_col, "geometry"]],
                        how="left", predicate="within")

    tract_total_pop = blocks.groupby("tract_id")["population"].sum()

    covered = joined.dropna(subset=["catchment_id"])
    if covered.empty:
        return pd.DataFrame(columns=["tract_id", "value", "confidence"])

    covered["weighted_value"] = covered[value_col] * covered["population"]
    grouped = covered.groupby("tract_id").agg(
        weighted_sum=("weighted_value", "sum"),
        covered_pop=("population", "sum"),
    ).reset_index()

    grouped["value"] = grouped["weighted_sum"] / grouped["covered_pop"].replace(0, np.nan)
    grouped["confidence"] = (
        grouped["covered_pop"] / grouped["tract_id"].map(tract_total_pop).replace(0, np.nan)
    ).clip(upper=1.0)

    return grouped[["tract_id", "value", "confidence"]].dropna(subset=["value"])


# ==================================================
# CENSUS BLOCK POPULATION (one-time local cache builder)
# ==================================================

CENSUS_POPULATION_URL = "https://api.census.gov/data/2020/dec/pl"
TIGER_BLOCKS_URL_TMPL = "https://www2.census.gov/geo/tiger/TIGER2022/TABBLOCK20/tl_2022_{state_fips}_tabblock20.zip"


def fetch_and_cache_block_population(state_fips: str, county_fips_list: list[str],
                                      cache_path: Path, census_api_key: str = None) -> gpd.GeoDataFrame:
    """
    One-time setup helper — NOT called automatically by any ingest script,
    since the statewide TIGER block shapefile alone is ~115MB (confirmed via
    a live HEAD request). Run this once (e.g. from a Python shell) to build
    a small local cache of 2020 Census block geometry + total population
    (P1_001N) for the counties a dasymetric_crosswalk() call needs, the same
    "download once, cache locally" pattern as the HUD zip-tract crosswalk
    file (see ingest_vaccination_coverage.py) — documented in
    surveillance/README.md.

    county_fips_list: 3-digit county codes (e.g. ["033","061","053"] for
    King/Snohomish/Pierce), matching TIGER's COUNTYFP20 field.
    census_api_key: optional but recommended — set via the CENSUS_API_KEY
    env var, never hardcoded (see the leaked key in
    outbreak_model/phase2_vulnerability_pull.py, a separate pre-existing
    issue worth rotating, not a pattern to repeat).

    Caches GEOID (15-digit block id), population, geometry to `cache_path`
    as GeoJSON; returns it as a GeoDataFrame. Safe to re-run — skips the
    download if the cache already exists.
    """
    if cache_path.exists():
        print(f"  Block population cache already exists at {cache_path}, skipping fetch")
        return gpd.read_file(cache_path)

    tiger_url = f"zip+{TIGER_BLOCKS_URL_TMPL.format(state_fips=state_fips)}"
    print(f"Downloading TIGER 2020 block geometry for state {state_fips} from {tiger_url}...")
    print("  (one-time, ~100MB+ for a full state — this will take a while)")
    blocks = gpd.read_file(tiger_url)
    blocks["GEOID20"] = blocks["GEOID20"].astype(str)
    blocks = blocks[blocks["COUNTYFP20"].isin(county_fips_list)].copy()

    print(f"  {len(blocks)} blocks in target counties — pulling population from the Census API...")
    county_str = ",".join(county_fips_list)
    url = f"{CENSUS_POPULATION_URL}?get=P1_001N&for=block:*&in=state:{state_fips}+county:{county_str}"
    if census_api_key:
        url += f"&key={census_api_key}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    raw = r.json()
    pop_df = pd.DataFrame(raw[1:], columns=raw[0])
    pop_df["GEOID20"] = pop_df["state"] + pop_df["county"] + pop_df["tract"] + pop_df["block"]
    pop_df["population"] = pd.to_numeric(pop_df["P1_001N"], errors="coerce")

    merged = blocks.merge(pop_df[["GEOID20", "population"]], on="GEOID20", how="left")
    merged = merged.rename(columns={"GEOID20": "GEOID"})[["GEOID", "population", "geometry"]]
    merged["population"] = merged["population"].fillna(0)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_crs(DISTANCE_CRS).to_file(cache_path, driver="GeoJSON")
    print(f"  Cached {len(merged)} blocks with population to {cache_path}")
    return merged.to_crs(DISTANCE_CRS)
