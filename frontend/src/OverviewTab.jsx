import { useState, useEffect } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import axios from "axios";
import { fmt, pct, dark } from "./App.jsx";
import { STREAMS, formatStreamValue } from "./SurveillanceTab.jsx";

// Demo fallback — same precedent as SurveillanceTab.jsx: deterministic,
// clearly labeled, used only when the backend reports no real data yet
// (surveillance.db has zero rows today).
const DEMO_WEEKS = ["2026-08-02", "2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"];

function hashUnit(str, salt = 0) {
  let h = salt >>> 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(h, 31) + str.charCodeAt(i)) >>> 0;
  return (h % 10000) / 10000;
}

function buildDemoSummary() {
  const streams = STREAMS.filter(s => s.id !== "school_absenteeism" || true).map((s, idx) => {
    const range = s.kind === "fraction" ? [0.3, 0.7] : s.id.startsWith("air_quality") ? [5, 40] : [3e8, 2e9];
    const trend = DEMO_WEEKS.map((week, i) => {
      const base = hashUnit(`${s.id}|${i}`, idx + 1);
      const value = range[0] + base * (range[1] - range[0]);
      return { week, mean_value: Math.round(value * 10000) / 10000 };
    });
    return {
      stream: s.id,
      latest_week: DEMO_WEEKS[DEMO_WEEKS.length - 1],
      weeks_available: DEMO_WEEKS.length,
      tracts_reporting_latest: Math.round(1200 + hashUnit(s.id, 3) * 500),
      mean_value_latest: trend[trend.length - 1].mean_value,
      pct_carry_forward_latest: Math.round(hashUnit(s.id, 7) * 15) / 100,
      trend,
    };
  });

  const alertTrend = DEMO_WEEKS.map((week, i) => ({ week, count: Math.round(hashUnit(`alerts|${i}`, 5) * 4) }));
  const recent = [
    { tract_id: "53033030003", week: DEMO_WEEKS[DEMO_WEEKS.length - 1], direction: "elevated", composite_score: 3.1, corroborating_streams: ["hospital_capacity", "wastewater_sars_cov2"] },
    { tract_id: "53061041201", week: DEMO_WEEKS[DEMO_WEEKS.length - 2], direction: "elevated", composite_score: 2.6, corroborating_streams: ["air_quality_pm25_mean", "school_absenteeism"] },
  ];

  return {
    available: false,
    demo: true,
    latest_week: DEMO_WEEKS[DEMO_WEEKS.length - 1],
    streams,
    alerts: { active_count_latest: alertTrend[alertTrend.length - 1].count, trend: alertTrend, recent },
  };
}

function Sparkline({ data, dataKey }) {
  return (
    <ResponsiveContainer width="100%" height={36}>
      <LineChart data={data} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
        <Line type="monotone" dataKey={dataKey} stroke="#3b82f6" strokeWidth={1.5} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/**
 * Overview tab — statewide anomaly summary + recent per-stream activity,
 * fed by one call to /surveillance/summary. Self-contained, same
 * fetch-its-own-data / demo-fallback convention as SurveillanceTab.jsx.
 */
export default function OverviewTab({ api }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    axios.get(`${api}/surveillance/summary`)
      .then(res => { if (!cancelled) setData(res.data.available ? res.data : buildDemoSummary()); })
      .catch(() => { if (!cancelled) setData(buildDemoSummary()); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api]);

  if (loading) return <p className="drawer-note">Loading overview…</p>;
  if (!data) return <p className="drawer-note">No data.</p>;

  const streamLabel = id => STREAMS.find(s => s.id === id)?.label || id;
  const streamMeta = id => STREAMS.find(s => s.id === id) || { kind: "fraction", unit: "" };

  return (
    <>
      <div className="ds-title">Anomaly Overview</div>
      {data.demo && (
        <div className="roi-caveat" style={{ color: "#fbbf24", marginBottom: 8 }}>
          ⚠ Demo data — no real surveillance data ingested yet, showing
          synthetic placeholder values for preview only.
        </div>
      )}
      <p className="drawer-note" style={{ marginBottom: 8 }}>Latest week: {data.latest_week}</p>

      <div className="results-grid">
        <div className="result-card"><span>Active Alerts</span><strong>{fmt(data.alerts.active_count_latest)}</strong></div>
        <div className="result-card"><span>Streams Reporting</span><strong>{fmt(data.streams.length)}</strong></div>
      </div>

      <div className="ds-title" style={{ marginTop: 14 }}>Alert Trend</div>
      <ResponsiveContainer width="100%" height={110}>
        <BarChart data={data.alerts.trend} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
          <XAxis dataKey="week" tickFormatter={w => w?.slice(5)} stroke="#334155" tick={{ fontSize: 9, fill: "#64748b" }} />
          <YAxis allowDecimals={false} stroke="#334155" tick={{ fontSize: 9, fill: "#64748b" }} />
          <Tooltip formatter={v => [v, "Alerts"]} labelFormatter={w => `Week ${w}`} contentStyle={dark} />
          <Bar dataKey="count" fill="#fbbf24" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>

      <div className="ds-title" style={{ marginTop: 14 }}>Recent Alerts</div>
      {data.alerts.recent.length === 0 ? (
        <p className="drawer-note">No alerts in the recent history window.</p>
      ) : (
        <div className="eq-table-wrap">
          <table className="eq-table">
            <thead><tr><th>Week</th><th>Tract</th><th>Dir.</th><th>Score</th><th>Streams</th></tr></thead>
            <tbody>
              {data.alerts.recent.map((a, i) => (
                <tr key={`${a.tract_id}-${a.week}-${i}`}>
                  <td>{a.week}</td>
                  <td>{a.tract_id}</td>
                  <td><span className={`surv-badge ${a.direction === "elevated" ? "carry-forward" : "observed"}`}>{a.direction}</span></td>
                  <td>{fmt(Math.round(a.composite_score * 100) / 100)}</td>
                  <td>{a.corroborating_streams.map(streamLabel).join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="ds-title" style={{ marginTop: 14 }}>Stream Activity (recent)</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {data.streams.map(s => {
          const meta = streamMeta(s.stream);
          return (
            <div key={s.stream} className="compare-summary" style={{ marginTop: 0 }}>
              <div className="cs-row"><span>{streamLabel(s.stream)}</span><strong>{s.latest_week}</strong></div>
              <div className="cs-row"><span>Tracts reporting</span><strong>{fmt(s.tracts_reporting_latest)}</strong></div>
              <div className="cs-row"><span>Mean value</span><strong>{formatStreamValue(meta.kind, meta.unit, s.mean_value_latest)}</strong></div>
              <div className="cs-row"><span>Carry-forward</span><strong>{pct(s.pct_carry_forward_latest)}</strong></div>
              <Sparkline data={s.trend} dataKey="mean_value" />
            </div>
          );
        })}
      </div>

      <p className="roi-caveat" style={{ marginTop: 14 }}>
        Trend windows cover the last {data.streams[0]?.trend.length || 8} weeks of data.
        An alert requires 2+ streams anomalous in the same direction, with at
        least one tract-resolved stream among them (see surveillance/scoring_config.py).
      </p>
    </>
  );
}
