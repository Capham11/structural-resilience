"""
Surveillance — Unified Tract-Level Time Series Store

Shared SQLite table for all surveillance streams (hospital capacity,
vaccination coverage, and any future stream). No anomaly detection or
scoring here — just schema + upsert + gap carry-forward.

Schema:
    tract_id              TEXT  — 11-digit zero-padded GEOID
    stream                TEXT  — e.g. 'hospital_capacity', 'vaccination_coverage'
    week                  TEXT  — ISO date, week-ending Sunday, 'YYYY-MM-DD'
    value                 REAL
    confidence            REAL  — 0-1
    interpolation_method  TEXT  — 'distance_weighted_idw' | 'population_weighted_zip_tract'
                                   | 'carry_forward'
"""

import sqlite3
from pathlib import Path

import pandas as pd

# ==================================================
# CONFIG
# ==================================================

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "data" / "surveillance.db"

TABLE = "tract_timeseries"
COLUMNS = ["tract_id", "stream", "week", "value", "confidence", "interpolation_method"]

# Confidence multiplier applied per week a value is carried forward, so
# staleness degrades visibly rather than silently repeating full-confidence
# numbers forever.
CARRY_FORWARD_DECAY_PER_WEEK = 0.85

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    tract_id              TEXT NOT NULL,
    stream                TEXT NOT NULL,
    week                  TEXT NOT NULL,
    value                 REAL,
    confidence            REAL,
    interpolation_method  TEXT,
    PRIMARY KEY (tract_id, stream, week)
);
"""


# ==================================================
# CONNECTION / SCHEMA
# ==================================================

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_SQL)
    conn.commit()


# ==================================================
# UPSERT
# ==================================================

def upsert_rows(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Insert or replace rows in tract_timeseries. `df` must contain exactly
    COLUMNS (extra columns are ignored). Returns the number of rows written.
    """
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"upsert_rows: missing required columns {missing}")

    rows = df[COLUMNS].itertuples(index=False, name=None)
    conn.executemany(
        f"""
        INSERT OR REPLACE INTO {TABLE} ({", ".join(COLUMNS)})
        VALUES ({", ".join(["?"] * len(COLUMNS))})
        """,
        rows,
    )
    conn.commit()
    return len(df)


# ==================================================
# GAP HANDLING
# ==================================================

def carry_forward_gaps(conn: sqlite3.Connection, stream: str, tract_ids, week: str) -> int:
    """
    For every tract_id in `tract_ids` with no row for (stream, week), find its
    most recent prior row for `stream` (any interpolation_method) and copy the
    value forward with interpolation_method='carry_forward', decaying
    confidence by CARRY_FORWARD_DECAY_PER_WEEK per week of staleness.

    Never overwrites a row that already exists for (stream, week) — carry
    forward only fills gaps, it doesn't clobber observed data.
    Returns the number of rows carried forward.
    """
    existing = pd.read_sql(
        f"SELECT tract_id FROM {TABLE} WHERE stream = ? AND week = ?",
        conn, params=(stream, week),
    )
    have = set(existing["tract_id"])
    missing = [t for t in tract_ids if t not in have]
    if not missing:
        return 0

    history = pd.read_sql(
        f"SELECT tract_id, week, value, confidence FROM {TABLE} "
        f"WHERE stream = ? AND tract_id IN ({','.join(['?'] * len(missing))}) AND week < ?",
        conn, params=(stream, *missing, week),
    )
    if history.empty:
        return 0

    history["week"] = pd.to_datetime(history["week"])
    target_week = pd.to_datetime(week)
    latest = history.sort_values("week").groupby("tract_id").tail(1).copy()

    weeks_stale = ((target_week - latest["week"]).dt.days / 7).clip(lower=1).round().astype(int)
    latest["confidence"] = (
        latest["confidence"].fillna(0) * (CARRY_FORWARD_DECAY_PER_WEEK ** weeks_stale)
    )

    out = pd.DataFrame({
        "tract_id": latest["tract_id"],
        "stream": stream,
        "week": week,
        "value": latest["value"],
        "confidence": latest["confidence"],
        "interpolation_method": "carry_forward",
    })
    return upsert_rows(conn, out)
