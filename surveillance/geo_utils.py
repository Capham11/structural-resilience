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
