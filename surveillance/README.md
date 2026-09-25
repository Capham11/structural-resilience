# Surveillance ingestion (Phase 6)

Ingests weekly tract-level surveillance streams into a single SQLite
table (`data/surveillance.db`, table `tract_timeseries`). No anomaly
detection or scoring here — just crosswalk + storage.

```
tract_id | stream | week | value | confidence | interpolation_method
```

- `tract_id` — 11-digit zero-padded GEOID (same convention as `outbreak_model/phase2_vulnerability_pull.py`)
- `stream` — `hospital_capacity` | `vaccination_coverage` | `air_quality_pm25_mean` |
  `air_quality_pm25_max` | `wastewater_sars_cov2` | `school_absenteeism` (scaffold, not live)
- `week` — ISO date
- `value` — stream-specific rate, usually 0-1 — **except `wastewater_sars_cov2`**,
  which is a flow-population-normalized concentration in its native (large)
  units, not rescaled (see that stream's section)
- `confidence` — 0-1, how well-supported the value is
- `interpolation_method` — `distance_weighted_idw`, `population_weighted_zip_tract`,
  `population_weighted_dasymetric`, or `carry_forward`

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

## Stream 3 — `ingest_air_quality.py` (EPA AirNow) — the first genuine live pull

Unlike the two streams above, this one really is scriptable end to end —
verified live while building it (2026-09-25):

- Monitor locations: `https://files.airnowtech.org/airnow/today/monitoring_site_locations.dat`,
  public, no key, confirmed reachable (148+ active WA PM2.5 monitors).
- Readings: `https://www.airnowapi.org/aq/data/`, needs a free key from
  https://docs.airnowapi.org — set `AIRNOW_API_KEY` as an environment
  variable, never hardcode it.

Crosswalk: k-nearest (3-5) inverse-distance-weighted interpolation to tract
centroids — no radius cutoff like hospital capacity, since air quality
genuinely still carries signal at long range. Instead, `confidence` decays
smoothly once the nearest monitor is beyond `MAX_GOOD_DISTANCE_KM` (40km),
floored at `CONFIDENCE_FLOOR` so sparse eastern-WA tracts get a real value,
clearly flagged low-confidence rather than silently treated as equal
quality to a tract next to three monitors.

Writes **two streams** per week — `air_quality_pm25_mean` and
`air_quality_pm25_max` — since the schema has one `value` column and
mean/max both matter.

Run: `AIRNOW_API_KEY=... python3 ingest_air_quality.py` (defaults to the
most recently completed week-ending-Sunday).

## Stream 4 — `ingest_wastewater.py` (SCAN / WastewaterSCAN) — King, Snohomish, Pierce only

"SCAN" data (WastewaterSCAN: Stanford/Emory/Verily) is republished by CDC's
NWSS. **Verify `CDC_DATASET_URL` in the script is still current before
relying on it** — CDC has already renamed/restructured this data once
(NWSS funding lapsed Sept 30 2025 during a testing-contract transition):
`g653-rqe2`/`2ew6-ywp6` are stale (max date found: 2025-09-07, any state);
`j9g8-acpt` ("CDC Wastewater Data for SARS-CoV-2") is current as of writing
(WA samples as recent as 2026-09-21, confirmed live).

Two local files needed, since real gaps exist that no auto-join covers:

| file | what | why it's local |
|---|---|---|
| `data/raw/sewersheds.geojson` | catchment polygons, needs a `catchment_id` column | CDC's site records carry no geometry at all — real polygons come from EPA's National Sewershed Dataset (github.com/USEPA/Sewersheds, `Current_Release.zip`, ~127MB **nationwide** — extract just the WA sites in scope) |
| `data/raw/wastewater_site_crosswalk.csv` | `cdc_site_id, catchment_id` | no shared identifier exists between CDC's site IDs and EPA's polygon IDs — confirmed by inspecting both sources; for the handful of in-scope plants this is a one-time manual match (same pattern as `backend/hub_distance.csv`) |

Also needs cached 2020 Census block population — see
`geo_utils.fetch_and_cache_block_population()` (not run automatically; the
statewide TIGER block file alone is ~115MB).

Crosswalk: `geo_utils.dasymetric_crosswalk()` — population-weighted
allocation of each catchment's value to tracts, via block-centroid
containment (a block's whole population counts toward whichever catchment
contains its centroid — the standard simplification vs. full area-weighted
polygon overlay, since blocks are small relative to tracts/catchments).

**Coverage is restricted to King/Snohomish/Pierce on purpose** — tracts
elsewhere get no row for this stream at all (real SQL-row absence = null),
not a zero. `carry_forward_gaps` is called with the tract universe filtered
to those three counties, never the statewide list.

**`value` is not a 0-1 rate** — it's `pcr_target_flowpop_lin`, CDC's
flow-and-population-normalized concentration in its native (large) units.
Rescaling it against a baseline is a scoring decision, out of scope here.

Run: `python3 ingest_wastewater.py` (processes every week found in the last
`LOOKBACK_WEEKS`, default 12).

## Stream 5 — `ingest_school_absenteeism.py` — scaffold only, real access blockers

Not wired to any real input. Two verified, hard blockers, not engineering
gaps:

1. **Cadence**: WA OSPI's Report Card publishes attendance/absenteeism data
   in periodic releases tied to the school calendar (district CEDARS
   submissions) — no evidence anywhere of a weekly public download.
2. **Boundaries are stale, not slow**: NCES's School Attendance Boundary
   Survey (SABS) was a two-cycle *experimental* survey (2013-14, 2015-16)
   discontinued after 2015-16 — the only public national school-catchment
   source is a decade-plus out of date, not just infrequently refreshed.

Direct district outreach (per the original request) is the real path to a
live version of this stream. What's built anyway: real crosswalk logic
(reusing `geo_utils.dasymetric_crosswalk()`, same as wastewater — school
attendance boundaries instead of sewersheds) and a real schema writer,
local-file adapter input (`data/raw/school_absenteeism.csv`:
`school_id, week, absence_rate`) — so wiring in real data later is a data
change, not a rebuild.

## Gap handling

Every live/local-file script calls `db.carry_forward_gaps` after writing
observed rows for a week: any tract in its tract universe with no row for
`(stream, week)` gets the most recent prior value copied forward,
`interpolation_method='carry_forward'`, confidence multiplied by
`CARRY_FORWARD_DECAY_PER_WEEK` (0.85) per week of staleness — so gap-filled
values visibly lose confidence rather than looking as fresh as observed
ones. For `wastewater_sars_cov2` that tract universe is King/Snohomish/
Pierce only — tracts elsewhere never get carried-forward *or* observed
rows, i.e. real absence, not zero.

## Tests

`pytest surveillance/tests/` — offline unit tests on synthetic fixtures
covering every crosswalk (IDW, k-nearest IDW, population-weighted
zip-tract, population-weighted dasymetric) and carry-forward (including
that it never overwrites an observed row).

## Verifying end to end

```
python3 ingest_hospital_capacity.py
python3 ingest_vaccination_coverage.py
AIRNOW_API_KEY=... python3 ingest_air_quality.py       # live pull, needs a free key
python3 ingest_wastewater.py                            # live pull + local sewershed/population files
sqlite3 ../data/surveillance.db "select stream, interpolation_method, count(*) from tract_timeseries group by 1,2;"
```

`ingest_school_absenteeism.py` has no real input to verify against — see
its section above.
