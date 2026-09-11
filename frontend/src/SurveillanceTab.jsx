import { useState, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer,
} from "recharts";
import axios from "axios";
import { Slider, fmt, pct, dark } from "./App.jsx";

const STREAMS = [
  { id: "hospital_capacity",    label: "Hospital Capacity" },
  { id: "vaccination_coverage", label: "Vaccination Coverage" },
];

const INGEST_SCRIPT = {
  hospital_capacity:    "surveillance/ingest_hospital_capacity.py",
  vaccination_coverage: "surveillance/ingest_vaccination_coverage.py",
};

// Demo fallback — used only when the backend reports no real data yet
// (surveillance.db not deployed / not ingested). Purely client-side,
// deterministic per-GEOID so the map + charts look stable while scrubbing,
// and clearly labeled "demo" everywhere (interpolation_method, badges, a
// banner) so it's never mistaken for real surveillance data.
const DEMO_WEEKS = ["2026-08-16", "2026-08-23", "2026-08-30", "2026-09-06"];
const DEMO_RANGE = {
  hospital_capacity:    [0.40, 0.95],
  vaccination_coverage: [0.35, 0.85],
};

function hashUnit(str, salt = 0) {
  let h = salt >>> 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(h, 31) + str.charCodeAt(i)) >>> 0;
  return (h % 10000) / 10000; // deterministic pseudo-random in [0,1)
}

function demoValueFor(geoid, stream, weekIdx) {
  const [lo, hi] = DEMO_RANGE[stream];
  const base  = hashUnit(geoid, stream === "hospital_capacity" ? 1 : 2);
  const drift = Math.sin(weekIdx * 0.7 + base * 6.28) * 0.05;
  const value = Math.min(0.99, Math.max(0.05, lo + base * (hi - lo) + drift));
  const confidence = Math.round((0.6 + hashUnit(geoid, weekIdx + 9) * 0.4) * 100) / 100;
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

  // Load that week's per-tract values (real or demo), hand them up to the map.
  useEffect(() => {
    if (!weeks.length) { onValuesChange({}); return; }
    if (!available) {
      onValuesChange(buildDemoTractValues(tracts, stream, weekIndex));
      return;
    }
    let cancelled = false;
    const week = weeks[weekIndex];
    axios.get(`${api}/surveillance/tracts`, { params: { stream, week } })
      .then(res => { if (!cancelled) onValuesChange(res.data.tracts || {}); })
      .catch(() => { if (!cancelled) onValuesChange({}); });
    return () => { cancelled = true; };
  }, [api, stream, weeks, weekIndex, available, tracts, onValuesChange]);

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
    value: row.value != null ? Math.round(row.value * 1000) / 10 : null,
  }));

  const onMap = mapMode === stream;
  const streamLabel = STREAMS.find(s => s.id === stream)?.label;

  return (
    <>
      <div className="ds-title">Stream</div>
      <div className="p5-tabs">
        {STREAMS.map(s => (
          <button key={s.id} className={`p5-tab ${stream===s.id?"active":""}`} onClick={()=>setStream(s.id)}>
            {s.label}
          </button>
        ))}
      </div>

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
                  <div className="drawer-chart-label">Weekly value (%)</div>
                  <ResponsiveContainer width="100%" height={120}>
                    <LineChart data={chartData} margin={{top:4,right:12,left:0,bottom:0}}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                      <XAxis dataKey="week" stroke="#334155" tick={{fontSize:9,fill:"#64748b"}} />
                      <YAxis stroke="#334155" tick={{fontSize:9,fill:"#64748b"}} unit="%" />
                      <Tooltip formatter={(v)=>[`${v}%`,"Value"]} contentStyle={dark} />
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
                            <td>{pct(row.value)}</td>
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
        Hospital capacity: distance-weighted average facility occupancy.
        Vaccination coverage: population-weighted zip→tract coverage.
        "Carried" values are gap-filled from the prior week (see confidence).
      </p>
    </>
  );
}
