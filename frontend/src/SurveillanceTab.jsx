import { useState, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer,
} from "recharts";
import axios from "axios";
import { Slider, fmt, pct, dark } from "./App.jsx";

// `kind: "fraction"` streams are already 0-1 (occupancy rate, coverage rate,
// absence rate) — displayed as % and safe to feed the map directly.
// `kind: "raw"` streams are native-unit magnitudes (µg/m³, a normalized
// viral concentration) — displayed as plain numbers with a unit, and
// min-max normalized to 0-1 *only* for the map layer (see normalizeForMap)
// since the map's color ramp assumes 0-1 input.
export const STREAMS = [
  { id: "hospital_capacity",     label: "Hospital Capacity",         kind: "fraction" },
  { id: "vaccination_coverage",  label: "Vaccination Coverage",      kind: "fraction" },
  { id: "air_quality_pm25_mean", label: "Air Quality — PM2.5 (avg)", kind: "raw", unit: "µg/m³" },
  { id: "air_quality_pm25_max",  label: "Air Quality — PM2.5 (max)", kind: "raw", unit: "µg/m³" },
  { id: "wastewater_sars_cov2",  label: "Wastewater (SARS-CoV-2)",   kind: "raw", unit: "" },
  { id: "school_absenteeism",    label: "School Absenteeism",        kind: "fraction" },
];

export const SURVEILLANCE_STREAM_IDS = STREAMS.map(s => s.id);

const INGEST_SCRIPT = {
  hospital_capacity:     "surveillance/ingest_hospital_capacity.py",
  vaccination_coverage:  "surveillance/ingest_vaccination_coverage.py",
  air_quality_pm25_mean: "surveillance/ingest_air_quality.py",
  air_quality_pm25_max:  "surveillance/ingest_air_quality.py",
  wastewater_sars_cov2:  "surveillance/ingest_wastewater.py",
  school_absenteeism:    "surveillance/ingest_school_absenteeism.py (scaffold — see its docstring for the access blockers)",
};

// Demo fallback — used only when the backend reports no real data yet
// (surveillance.db not deployed / not ingested). Purely client-side,
// deterministic per-GEOID so the map + charts look stable while scrubbing,
// and clearly labeled "demo" everywhere (interpolation_method, badges, a
// banner) so it's never mistaken for real surveillance data.
const DEMO_WEEKS = ["2026-08-16", "2026-08-23", "2026-08-30", "2026-09-06"];
const DEMO_RANGE = {
  hospital_capacity:     [0.40, 0.95],
  vaccination_coverage:  [0.35, 0.85],
  air_quality_pm25_mean: [3, 45],
  air_quality_pm25_max:  [8, 120],
  wastewater_sars_cov2:  [2e8, 3e9],
  school_absenteeism:    [0.05, 0.30],
};

function hashUnit(str, salt = 0) {
  let h = salt >>> 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(h, 31) + str.charCodeAt(i)) >>> 0;
  return (h % 10000) / 10000; // deterministic pseudo-random in [0,1)
}

function demoValueFor(geoid, stream, weekIdx) {
  const [lo, hi] = DEMO_RANGE[stream];
  const base  = hashUnit(`${geoid}|${stream}`, 1);
  const drift = Math.sin(weekIdx * 0.7 + base * 6.28) * 0.05 * (hi - lo);
  const value = Math.min(hi, Math.max(lo, lo + base * (hi - lo) + drift));
  const confidence = Math.round((0.6 + hashUnit(`${geoid}|${stream}`, weekIdx + 9) * 0.4) * 100) / 100;
  return { value: Math.round(value * 10000) / 10000, confidence, interpolation_method: "demo" };
}

function buildDemoTractValues(tracts, stream, weekIdx) {
  if (!tracts?.features) return {};
  const out = {};
  tracts.features.forEach(f => { out[f.properties.GEOID] = demoValueFor(f.properties.GEOID, stream, weekIdx); });
  return out;
}

function buildDemoSeries(geoid, stream) {
  return DEMO_WEEKS.map((week, i) => ({ week, ...demoValueFor(geoid, stream, i) }));
}

// The map's color ramp assumes 0-1 input. "fraction" streams already are;
// "raw" streams (native units, wildly different magnitudes per stream) get
// min-max normalized across the current week's values *only* for this
// purpose — the per-tract chart/table below always shows the real value.
function normalizeForMap(tractValues, kind) {
  if (kind !== "raw") return tractValues;
  const vals = Object.values(tractValues).map(v => v.value).filter(v => v != null && !isNaN(v));
  if (!vals.length) return tractValues;
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = max - min || 1;
  const out = {};
  for (const [geoid, v] of Object.entries(tractValues)) {
    out[geoid] = { ...v, value: v.value != null ? (v.value - min) / range : null };
  }
  return out;
}

function formatStreamValue(kind, unit, value) {
  if (value == null) return "—";
  return kind === "fraction" ? pct(value) : `${fmt(value)}${unit ? " " + unit : ""}`;
}

/**
 * Phase 6 — Surveillance tab. Self-contained: fetches its own data from the
 * three /surveillance/* endpoints and only talks to the rest of the app via
 * props (mapMode/setMapMode to put a layer on the map, onValuesChange to
 * hand the current week's per-tract values up for the map color effect).
 * Falls back to clearly-labeled client-side demo values when the backend
 * has no real data yet.
 */
export default function SurveillanceTab({ api, tracts, mapMode, setMapMode, selectedTract, onValuesChange }) {
  const [stream, setStream]         = useState("hospital_capacity");
  const [weeks, setWeeks]           = useState([]);
  const [weekIndex, setWeekIndex]   = useState(0);
  const [available, setAvailable]   = useState(true); // true = real backend data; false = demo fallback
  const [loadingWeeks, setLoadingWeeks] = useState(false);

  const [series, setSeries]         = useState([]);
  const [loadingSeries, setLoadingSeries] = useState(false);

  // Load available weeks whenever the stream changes; fall back to demo weeks.
  useEffect(() => {
    let cancelled = false;
    setLoadingWeeks(true);
    axios.get(`${api}/surveillance/weeks`, { params: { stream } })
      .then(res => {
        if (cancelled) return;
        if (res.data.available && res.data.weeks.length) {
          setWeeks(res.data.weeks);
          setAvailable(true);
          setWeekIndex(res.data.weeks.length - 1);
        } else {
          setWeeks(DEMO_WEEKS);
          setAvailable(false);
          setWeekIndex(DEMO_WEEKS.length - 1);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setWeeks(DEMO_WEEKS);
        setAvailable(false);
        setWeekIndex(DEMO_WEEKS.length - 1);
      })
      .finally(() => { if (!cancelled) setLoadingWeeks(false); });
    return () => { cancelled = true; };
  }, [api, stream]);

  const streamKind = STREAMS.find(s => s.id === stream)?.kind || "fraction";
  const streamUnit = STREAMS.find(s => s.id === stream)?.unit || "";

  // Load that week's per-tract values (real or demo), hand them up to the map.
  useEffect(() => {
    if (!weeks.length) { onValuesChange({}); return; }
    if (!available) {
      onValuesChange(normalizeForMap(buildDemoTractValues(tracts, stream, weekIndex), streamKind));
      return;
    }
    let cancelled = false;
    const week = weeks[weekIndex];
    axios.get(`${api}/surveillance/tracts`, { params: { stream, week } })
      .then(res => { if (!cancelled) onValuesChange(normalizeForMap(res.data.tracts || {}, streamKind)); })
      .catch(() => { if (!cancelled) onValuesChange({}); });
    return () => { cancelled = true; };
  }, [api, stream, weeks, weekIndex, available, tracts, onValuesChange, streamKind]);

  // Clear any surveillance layer off the map when this tab is torn down.
  useEffect(() => () => onValuesChange({}), [onValuesChange]);

  // Per-tract history, when a tract is hovered/selected (real or demo).
  useEffect(() => {
    if (!selectedTract) { setSeries([]); return; }
    if (!available) {
      setSeries(buildDemoSeries(selectedTract.GEOID, stream));
      return;
    }
    let cancelled = false;
    setLoadingSeries(true);
    axios.get(`${api}/surveillance/timeseries`, {
      params: { stream, tract_id: selectedTract.GEOID },
    })
      .then(res => { if (!cancelled) setSeries(res.data.series || []); })
      .catch(() => { if (!cancelled) setSeries([]); })
      .finally(() => { if (!cancelled) setLoadingSeries(false); });
    return () => { cancelled = true; };
  }, [api, stream, selectedTract, available]);

  const chartData = series.map(row => ({
    week: row.week?.slice(5),  // MM-DD, short label
    value: row.value == null ? null : streamKind === "fraction" ? Math.round(row.value * 1000) / 10 : row.value,
  }));

  const onMap = mapMode === stream;
  const streamLabel = STREAMS.find(s => s.id === stream)?.label;

  return (
    <>
      <div className="ds-title">Stream</div>
      <select className="drawer-select" value={stream} onChange={e=>setStream(e.target.value)}>
        {STREAMS.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
      </select>

      {loadingWeeks && <p className="drawer-note">Loading weeks…</p>}

      {!loadingWeeks && !available && (
        <div className="roi-caveat" style={{ color: "#fbbf24", marginBottom: 8 }}>
          ⚠ Demo data — {streamLabel} hasn't been ingested yet, showing synthetic
          placeholder values for preview only. Real ingestion: <code>{INGEST_SCRIPT[stream]}</code>.
        </div>
      )}

      {!loadingWeeks && weeks.length > 0 && (
        <>
          <Slider
            label="Week"
            note={weeks[weekIndex]}
            min={0} max={weeks.length-1} step={1}
            value={weekIndex}
            onChange={setWeekIndex}
          />
          <button
            className={`drawer-run-btn ${onMap ? "purple" : ""}`}
            onClick={()=>setMapMode(onMap ? "vuln" : stream)}
          >
            {onMap ? "✓ Shown on map" : "🗺  View on map"}
          </button>

          {selectedTract && (
            <>
              <div className="ds-title" style={{marginTop:12}}>
                {selectedTract.GEOID} — {streamLabel}
              </div>
              {loadingSeries && <p className="drawer-note">Loading history…</p>}
              {!loadingSeries && series.length > 0 && (
                <>
                  <div className="drawer-chart-label">
                    Weekly value {streamKind==="fraction" ? "(%)" : streamUnit ? `(${streamUnit})` : ""}
                  </div>
                  <ResponsiveContainer width="100%" height={120}>
                    <LineChart data={chartData} margin={{top:4,right:12,left:0,bottom:0}}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                      <XAxis dataKey="week" stroke="#334155" tick={{fontSize:9,fill:"#64748b"}} />
                      <YAxis stroke="#334155" tick={{fontSize:9,fill:"#64748b"}} unit={streamKind==="fraction"?"%":""} />
                      <Tooltip formatter={(v)=>[streamKind==="fraction"?`${v}%`:fmt(v),"Value"]} contentStyle={dark} />
                      <Line type="monotone" dataKey="value" stroke="#3b82f6" strokeWidth={2} dot={{r:2}} connectNulls />
                    </LineChart>
                  </ResponsiveContainer>
                  <div className="eq-table-wrap" style={{marginTop:8}}>
                    <table className="eq-table">
                      <thead><tr><th>Week</th><th>Value</th><th>Conf.</th><th>Method</th></tr></thead>
                      <tbody>
                        {series.slice(-6).reverse().map(row => (
                          <tr key={row.week}>
                            <td>{row.week}</td>
                            <td>{formatStreamValue(streamKind, streamUnit, row.value)}</td>
                            <td>{fmt(Math.round((row.confidence||0)*100))}%</td>
                            <td>
                              <span className={`surv-badge ${
                                row.interpolation_method==="demo" ? "demo"
                                : row.interpolation_method==="carry_forward" ? "carry-forward" : "observed"
                              }`}>
                                {row.interpolation_method==="demo" ? "demo"
                                  : row.interpolation_method==="carry_forward" ? "carried" : "observed"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )}
          {!selectedTract && (
            <p className="drawer-note" style={{marginTop:10}}>
              Hover or click a tract on the map to see its weekly history.
            </p>
          )}
        </>
      )}

      <p className="roi-caveat" style={{marginTop:14}}>
        Hospital capacity &amp; air quality: distance-weighted interpolation.
        Vaccination coverage, wastewater &amp; school absenteeism: population-
        weighted catchment allocation. Air quality/wastewater values are
        native-unit magnitudes, min-max scaled on the map only (the table
        above always shows the real number). "Carried" values are gap-filled
        from the prior week (see confidence).
      </p>
    </>
  );
}
