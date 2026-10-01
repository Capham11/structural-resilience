"""
Surveillance — Multi-Stream Corroboration

Turns per-(tract,stream) anomaly scores (tract_stream_scores, see
scoring.py) into tract-level alerts (tract_alerts, see db.py) using
scoring_config.CORROBORATION_RULE — a config dict, not hardcoded
if/else thresholds, since this will likely need tuning once real data
exists.

Rule (from scoring_config.CORROBORATION_RULE):
  - at least `min_streams` streams must be flagged anomalous (is_anomalous=1)
    for the same tract in the same week, in the SAME direction (elevated or
    depressed) — no single-stream alerts, and streams disagreeing on
    direction don't corroborate each other.
  - at least `min_tract_resolved` of those corroborating streams must be
    genuinely tract-resolved (scoring_config.STREAM_SCOPE[stream]=="tract"),
    not a regional/county broadcast re-applied across many tracts. Two
    broadcast-only streams agreeing is not independent evidence and never
    satisfies this alone.
"""

import json

import pandas as pd

import db
import scoring_config as cfg


def build_alerts_for_week(conn, week: str) -> pd.DataFrame:
    scores = pd.read_sql(
        f"SELECT * FROM {db.SCORES_TABLE} WHERE week = ? AND is_anomalous = 1",
        conn, params=[week],
    )
    if scores.empty:
        return pd.DataFrame(columns=db.ALERTS_COLUMNS)

    rule = cfg.CORROBORATION_RULE
    rows = []
    for tract_id, tract_group in scores.groupby("tract_id"):
        for direction, dgroup in tract_group.groupby("direction"):
            if direction == "flat" or len(dgroup) < rule["min_streams"]:
                continue

            # Default unregistered streams to "broadcast" (fail safe, not
            # fail open) — a stream only counts toward min_tract_resolved
            # once someone has explicitly classified it in STREAM_SCOPE.
            tract_resolved = [s for s in dgroup["stream"] if cfg.STREAM_SCOPE.get(s, "broadcast") == "tract"]
            if len(tract_resolved) < rule["min_tract_resolved"]:
                continue

            rows.append({
                "tract_id": tract_id,
                "week": week,
                "corroborating_streams": json.dumps(sorted(dgroup["stream"].tolist())),
                "composite_score": round(dgroup["z_score"].abs().mean(), 4),
                "confidence": round(float(dgroup["confidence"].fillna(0).mean()), 4),
                "fallback_baseline_used": int(dgroup["fallback_baseline_used"].any()),
                "direction": direction,
            })

    return pd.DataFrame(rows, columns=db.ALERTS_COLUMNS) if rows else pd.DataFrame(columns=db.ALERTS_COLUMNS)


def build_alerts_for_weeks(conn, weeks) -> int:
    """Build + upsert alerts for each week in `weeks`. Returns total rows written."""
    total = 0
    for week in weeks:
        out = build_alerts_for_week(conn, week)
        if not out.empty:
            total += db.upsert_rows(conn, out, table=db.ALERTS_TABLE, columns=db.ALERTS_COLUMNS)
    return total
