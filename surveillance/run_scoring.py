"""
Surveillance — Scoring + Corroboration Runner

Scores every stream present in tract_timeseries for a given week (or every
week present in the table, if none given), then builds corroborated alerts
for the same weeks. Run after the ingest_*.py scripts for whichever week(s)
you just ingested.

Usage: python3 run_scoring.py [week]
       week — ISO date, e.g. 2026-09-28; omit to score every week present.
"""

import sys

import pandas as pd

import db
import scoring
import corroboration


def run(week: str = None) -> None:
    conn = db.connect()

    if week:
        weeks = [week]
    else:
        weeks = pd.read_sql(f"SELECT DISTINCT week FROM {db.TABLE} ORDER BY week", conn)["week"].tolist()

    if not weeks:
        print("No weeks found in tract_timeseries — nothing to score.")
        conn.close()
        return

    for w in weeks:
        print(f"\nWeek {w}: scoring streams...")
        n_scored = scoring.score_week_all_streams(conn, w)
        print(f"  Wrote {n_scored} score rows")

    print("\nBuilding corroborated alerts...")
    n_alerts = corroboration.build_alerts_for_weeks(conn, weeks)
    print(f"  Wrote {n_alerts} alert rows")

    conn.close()
    print(f"\nDone. Table: {db.DB_PATH} ({db.SCORES_TABLE}, {db.ALERTS_TABLE})")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
