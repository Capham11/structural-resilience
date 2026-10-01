"""
Surveillance — Scoring + Corroboration Config

Kept as plain dicts (not hardcoded into scoring.py/corroboration.py) so the
corroboration threshold and stream classification can be tuned — or this
whole module swapped for a DB-backed rule table — without touching the
scoring/corroboration logic itself.
"""

# Which streams are genuine tract-resolved estimates vs. regional/county
# broadcasts re-applied to every tract in their coverage area. Two broadcast
# streams agreeing is not independent corroboration — add new streams here,
# nothing else needs to change.
STREAM_SCOPE = {
    "hospital_capacity":     "tract",
    "vaccination_coverage":  "tract",
    "air_quality_pm25_mean": "tract",
    "air_quality_pm25_max":  "tract",
    "wastewater_sars_cov2":  "tract",
    "school_absenteeism":    "tract",
    # Not built yet (see surveillance/README.md) — once ingest_search_trends.py
    # and ingest_pharmacy_fills.py exist, adding these two lines is the only
    # change needed for corroboration to treat them as broadcast-level:
    # "search_trends":   "broadcast",
    # "pharmacy_fills":  "broadcast",
}

CORROBORATION_RULE = {
    "min_streams": 2,          # no single-stream alerts
    "min_tract_resolved": 1,   # at least this many corroborating streams must be scope="tract"
    "z_threshold": 2.0,        # |z| at/above this counts as "anomalous" for corroboration purposes
}
