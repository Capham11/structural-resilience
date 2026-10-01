"""
Surveillance — Per-Stream Baseline + Anomaly Score

For each (tract_id, stream, week) observation in tract_timeseries, scores it
against a robust baseline built from that same (tract, stream)'s own prior
history, writing one row into tract_stream_scores (see db.py for the
schema). No cross-stream logic here — that's corroboration.py.

BASELINE METHOD — robust median/MAD, not seasonal-adjusted (yet)
------------------------------------------------------------------
The brief allows "seasonal-adjusted z-score or CUSUM, whichever is simpler
to get right first." With real history currently at zero weeks for every
stream (confirmed before building this), a seasonal term isn't estimable
today regardless of which method is picked — so this scores on median/MAD
of a trailing window, which is what's actually buildable and verifiable
right now. Median/MAD (not mean/stdev) because it's robust to the very
outliers being detected: with a short series, one bad week inflates a
mean/stdev estimate enough to mask the next real anomaly, which a
median-based estimate doesn't.

COLD START — local history -> county pool -> statewide pool -> sit out
--------------------------------------------------------------------------
A (tract, stream) needs MIN_LOCAL_HISTORY_WEEKS of its own real history
(within a trailing ROLLING_WINDOW_WEEKS) before it gets its own baseline.
Below that, falls back to pooling every tract sharing its 5-digit
state+county GEOID prefix; below MIN_COUNTY_OBSERVATIONS there too, pools
the full 2-digit state prefix. If none of the three have enough — no row is
written for that (tract, stream, week) at all. It is never scored as
"normal" for lack of data; `baseline_source`/`fallback_baseline_used` mark
exactly which tier actually produced a row.

County/state pooling needs no tract geometry lookup — a GEOID's digits
already encode state (first 2) and county (next 3), so pooling is a
substring match on tract_id, not a join against outbreak_model's tract
shapefile.

FDR-READINESS
--------------
`is_anomalous` is a convenience flag (|z| >= scoring_config.CORROBORATION_RULE
["z_threshold"]) computed at scoring time for corroboration.py to filter on
cheaply, but `z_score` and `p_value` are always stored regardless. A future
false-discovery-rate correction step reads z_score/p_value directly and can
recompute is_anomalous (e.g. via Benjamini-Hochberg across a week's p-values)
without rescoring anything.
"""

import math

import numpy as np
import pandas as pd

import db
import scoring_config as cfg

# ==================================================
# CONFIG
# ==================================================

MIN_LOCAL_HISTORY_WEEKS = 8     # a (tract,stream) needs this many real prior weeks for its own baseline
ROLLING_WINDOW_WEEKS = 52       # history older than this doesn't count toward any baseline
MIN_COUNTY_OBSERVATIONS = 20    # pooled (tract,week) observations needed for a county/statewide fallback

# History rows with these interpolation_methods are copies/placeholders, not
# real observations — they don't count as history, and aren't scored themselves.
NON_HISTORY_METHODS = {"carry_forward", "demo"}

MAD_SCALE = 1.4826  # scales median absolute deviation to be std-dev-equivalent under normality


# ==================================================
# STATISTICS
# ==================================================

def compute_baseline(values: np.ndarray) -> tuple:
    """Robust (median, mad) baseline — mad already scaled by MAD_SCALE."""
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * MAD_SCALE
    return median, mad


def normal_two_sided_p(z: float) -> float:
    """Two-sided p-value for |z| under a standard normal approximation."""
    return float(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))))


# ==================================================
# HISTORY QUERIES
# ==================================================

def _window_history(conn, stream: str, week: str, tract_ids=None, prefix: str = None,
                     window_weeks: int = ROLLING_WINDOW_WEEKS) -> pd.DataFrame:
    """
    Real (non-carry_forward/demo) tract_timeseries values for `stream`,
    strictly before `week`, within a trailing window. Either `tract_ids`
    (exact match — local history) or `prefix` (GEOID prefix match — county/
    statewide pooling) narrows which tracts count; giving neither returns
    every tract's history for the stream.
    """
    params = [stream, week, *NON_HISTORY_METHODS]
    query = (
        f"SELECT tract_id, week, value FROM {db.TABLE} "
        f"WHERE stream = ? AND week < ? "
        f"AND (interpolation_method IS NULL OR interpolation_method NOT IN "
        f"({','.join(['?'] * len(NON_HISTORY_METHODS))}))"
    )
    if tract_ids is not None:
        query += f" AND tract_id IN ({','.join(['?'] * len(tract_ids))})"
        params += list(tract_ids)
    elif prefix is not None:
        query += f" AND substr(tract_id, 1, {len(prefix)}) = ?"
        params.append(prefix)

    df = pd.read_sql(query, conn, params=params)
    if df.empty:
        return df
    df["week"] = pd.to_datetime(df["week"])
    cutoff = pd.to_datetime(week) - pd.Timedelta(weeks=window_weeks)
    return df[(df["week"] >= cutoff) & df["value"].notna()]


# ==================================================
# SCORING
# ==================================================

def score_value(conn, tract_id: str, stream: str, week: str, value: float, confidence) -> dict:
    """
    Score one observation against its baseline. Returns a score row dict,
    or None if there isn't enough history anywhere (local, county, or
    statewide) to score against yet — the cold-start "sit out" case.
    """
    local_hist = _window_history(conn, stream, week, tract_ids=[tract_id])
    if len(local_hist) >= MIN_LOCAL_HISTORY_WEEKS:
        median, mad = compute_baseline(local_hist["value"].to_numpy())
        source, fallback, n = "local", False, len(local_hist)
    else:
        county_hist = _window_history(conn, stream, week, prefix=tract_id[:5])
        if len(county_hist) >= MIN_COUNTY_OBSERVATIONS:
            median, mad = compute_baseline(county_hist["value"].to_numpy())
            source, fallback, n = "county_fallback", True, len(county_hist)
        else:
            statewide_hist = _window_history(conn, stream, week, prefix=tract_id[:2])
            if len(statewide_hist) >= MIN_COUNTY_OBSERVATIONS:
                median, mad = compute_baseline(statewide_hist["value"].to_numpy())
                source, fallback, n = "statewide_fallback", True, len(statewide_hist)
            else:
                return None  # sits out — not enough history anywhere yet

    mad_floor = max(abs(median) * 0.01, 1e-6)
    z = (value - median) / max(mad, mad_floor)
    direction = "elevated" if z > 0 else "depressed" if z < 0 else "flat"

    return {
        "tract_id": tract_id, "stream": stream, "week": week,
        "value": value, "confidence": confidence,
        "baseline_median": median, "baseline_mad": mad,
        "z_score": round(z, 4), "p_value": round(normal_two_sided_p(z), 6),
        "direction": direction,
        "is_anomalous": int(abs(z) >= cfg.CORROBORATION_RULE["z_threshold"]),
        "baseline_source": source, "fallback_baseline_used": int(fallback),
        "n_history_weeks": n,
    }


def score_week(conn, stream: str, week: str) -> pd.DataFrame:
    """Score every tract with a real observed value for `stream` this week."""
    observed = pd.read_sql(
        f"SELECT tract_id, value, confidence FROM {db.TABLE} "
        f"WHERE stream = ? AND week = ? AND value IS NOT NULL "
        f"AND (interpolation_method IS NULL OR interpolation_method NOT IN "
        f"({','.join(['?'] * len(NON_HISTORY_METHODS))}))",
        conn, params=[stream, week, *NON_HISTORY_METHODS],
    )
    rows = []
    for r in observed.itertuples(index=False):
        row = score_value(conn, r.tract_id, stream, week, r.value, r.confidence)
        if row:
            rows.append(row)
    return pd.DataFrame(rows, columns=db.SCORES_COLUMNS) if rows else pd.DataFrame(columns=db.SCORES_COLUMNS)


def score_week_all_streams(conn, week: str, streams=None) -> int:
    """Score every stream present in tract_timeseries for `week` (or a given
    list). Returns total rows written."""
    if streams is None:
        streams = pd.read_sql(
            f"SELECT DISTINCT stream FROM {db.TABLE} WHERE week = ?", conn, params=[week]
        )["stream"].tolist()

    total = 0
    for stream in streams:
        out = score_week(conn, stream, week)
        if not out.empty:
            total += db.upsert_rows(conn, out, table=db.SCORES_TABLE, columns=db.SCORES_COLUMNS)
    return total
