"""
Surveillance — Unified Tract-Level Time Series Store + Scoring Tables

Three SQLite tables:
  - tract_timeseries     raw/crosswalked observations (all ingest_*.py scripts)
  - tract_stream_scores  per-(tract,stream,week) baseline + z-score (scoring.py)
  - tract_alerts         corroborated multi-stream alerts (corroboration.py)

tract_timeseries schema:
    tract_id              TEXT  — 11-digit zero-padded GEOID
    stream                TEXT  — e.g. 'hospital_capacity', 'vaccination_coverage'
    week                  TEXT  — ISO date, week-ending Sunday, 'YYYY-MM-DD'
    value                 REAL
    confidence            REAL  — 0-1
    interpolation_method  TEXT  — 'distance_weighted_idw' | 'population_weighted_zip_tract'
                                   | 'population_weighted_dasymetric' | 'carry_forward'
                                   | 'regional_broadcast' | 'county_broadcast' (future
                                   search_trends/pharmacy_fills streams)

tract_stream_scores / tract_alerts schemas: see SCHEMA_SQL below and
surveillance/scoring.py / corroboration.py docstrings.
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

SCORES_TABLE = "tract_stream_scores"
SCORES_COLUMNS = [
    "tract_id", "stream", "week", "value", "confidence", "baseline_median", "baseline_mad",
    "z_score", "p_value", "direction", "is_anomalous", "baseline_source",
    "fallback_baseline_used", "n_history_weeks",
]

ALERTS_TABLE = "tract_alerts"
ALERTS_COLUMNS = [
    "tract_id", "week", "corroborating_streams", "composite_score",
    "confidence", "fallback_baseline_used", "direction",
]

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

CREATE TABLE IF NOT EXISTS {SCORES_TABLE} (
    tract_id               TEXT NOT NULL,
    stream                 TEXT NOT NULL,
    week                   TEXT NOT NULL,
    value                  REAL,
    confidence             REAL,
    baseline_median        REAL,
    baseline_mad           REAL,
    z_score                REAL,
    p_value                REAL,
    direction              TEXT,
    is_anomalous           INTEGER,
    baseline_source        TEXT,
    fallback_baseline_used INTEGER,
    n_history_weeks        INTEGER,
    PRIMARY KEY (tract_id, stream, week)
);

CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
    tract_id               TEXT NOT NULL,
    week                   TEXT NOT NULL,
    corroborating_streams  TEXT NOT NULL,
    composite_score        REAL,
    confidence             REAL,
    fallback_baseline_used INTEGER,
    direction              TEXT,
    PRIMARY KEY (tract_id, week)
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
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ==================================================
# UPSERT
# ==================================================

def upsert_rows(conn: sqlite3.Connection, df: pd.DataFrame, table: str = TABLE,
                 columns: list = None) -> int:
    """
    Insert or replace rows in `table` (default tract_timeseries). `df` must
    contain at least `columns` (default COLUMNS) — extra columns are
    ignored. Shared by every ingest_*.py script as well as scoring.py /
    corroboration.py, which pass table=SCORES_TABLE / ALERTS_TABLE.
    Returns the number of rows written.
    """
    columns = columns if columns is not None else COLUMNS
    missing = set(columns) - set(df.columns)
    if missing:
        raise ValueError(f"upsert_rows: missing required columns {missing}")

    rows = df[columns].itertuples(index=False, name=None)
    conn.executemany(
        f"""
        INSERT OR REPLACE INTO {table} ({", ".join(columns)})
        VALUES ({", ".join(["?"] * len(columns))})
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
