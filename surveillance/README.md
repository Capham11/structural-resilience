# Surveillance ingestion (Phase 6)

Ingests two weekly tract-level surveillance streams into a single SQLite
table (`data/surveillance.db`, table `tract_timeseries`). No anomaly
detection or scoring here — just crosswalk + storage.

```
tract_id | stream | week | value | confidence | interpolation_method
```

- `tract_id` — 11-digit zero-padded GEOID (same convention as `outbreak_model/phase2_vulnerability_pull.py`)
- `stream` — `hospital_capacity` | `vaccination_coverage`
- `week` — ISO date
- `value` — stream-specific rate, 0-1
- `confidence` — 0-1, how well-supported the value is (facility count in range / matched population share)
- `interpolation_method` — `distance_weighted_idw`, `population_weighted_zip_tract`, or `carry_forward`

Requires `pandas`, `geopandas`, `numpy`, `shapely` (already used elsewhere in
this repo, e.g. `outbreak_model/phase2_vulnerability_pull.py`) plus `pytest`
for the tests. `sqlite3` is Python stdlib.

## Why these are local-file scripts, not live pulls

Neither source is actually open at the resolution this design needs, as of
2026:

- **Hospital capacity**: HHS's facility-level feed was discontinued
  May 3, 2024. Its federal successor (CDC/NHSN Weekly Hospital Respiratory
  Data) is public only at state/jurisdiction level — no facility identity or
  coordinates. WA's own facility-granular system (WA HEALTH) is real-time
  but gated to health care / emergency-preparedness partners
  (request access: wahealth@doh.wa.gov).
- **Vaccination coverage**: WA DOH's public data stops at county/state
  level. Zip-level coverage requires an ad hoc request to
  WAIISDataRequests@doh.wa.gov.

So both scripts read a local CSV (documented below) instead of a URL. Drop
in whatever export you get from either source, reshaped to these columns,
and the crosswalk math is unchanged — swapping in a real live feed later
only means changing how that CSV gets produced.

## Stream 1 — `ingest_hospital_capacity.py`

Input: `data/raw/hospital_capacity_facilities.csv`

| column | meaning |
|---|---|
| facility_id | any stable facility identifier |
| name | facility name (QA only) |
| latitude, longitude | WGS84 decimal degrees |
| week | ISO date |
| beds_staffed | staffed inpatient beds (7-day avg or snapshot) |
| beds_used | occupied staffed inpatient beds, same window |

Crosswalk: each tract's `value` is the inverse-distance-squared-weighted
average occupancy rate (`beds_used / beds_staffed`) of every facility within
`MAX_RADIUS_KM` (40km) of the tract centroid. `confidence` scales with how
many facilities are in range (3+ = full confidence). Tracts with no facility
in range are left for `carry_forward_gaps` to fill from the prior week.

Run: `python3 ingest_hospital_capacity.py`

## Stream 2 — `ingest_vaccination_coverage.py`

Input: `data/raw/vaccination_by_zip.csv`

| column | meaning |
|---|---|
| zip | 5-digit ZIP |
| week | ISO date |
| coverage_rate | fraction 0-1 |

Crosswalk input: `data/raw/zip_tract_crosswalk.csv` — HUD's quarterly
USPS ZIP-TRACT crosswalk (download from
https://www.huduser.gov/portal/datasets/usps_crosswalk.html, free account
required), reshaped to `zip, tract, res_ratio`. Changes slowly — fine to
reuse for months.

Crosswalk: each tract's `value` is the `RES_RATIO`-weighted average
coverage rate across every zip overlapping it that we have data for.
`confidence` is the sum of matched `RES_RATIO` (how much of the tract's
population the matched zips actually account for).

Run: `python3 ingest_vaccination_coverage.py`

## Gap handling

Both scripts call `db.carry_forward_gaps` after writing observed rows for a
week: any tract in the statewide tract universe with no row for
`(stream, week)` gets the most recent prior value copied forward,
`interpolation_method='carry_forward'`, confidence multiplied by
`CARRY_FORWARD_DECAY_PER_WEEK` (0.85) per week of staleness — so gap-filled
values visibly lose confidence rather than looking as fresh as observed
ones.

## Tests

`pytest surveillance/tests/` — offline unit tests on synthetic fixtures
covering the IDW crosswalk, the population-weighted crosswalk, and
carry-forward (including that it never overwrites an observed row).

## Verifying end to end

```
python3 ingest_hospital_capacity.py
python3 ingest_vaccination_coverage.py
sqlite3 ../data/surveillance.db "select stream, interpolation_method, count(*) from tract_timeseries group by 1,2;"
```
