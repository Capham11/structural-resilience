import { useState, useEffect, useRef, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";
import axios from "axios";
import "mapbox-gl/dist/mapbox-gl.css";
import "./App.css";

mapboxgl.accessToken = "pk.eyJ1IjoiY2hyaXN0b3BoZXJwaGFtIiwiYSI6ImNtcXZlbTRqZzEyeXEydXExZzl0aWJiaHMifQ.o58ZrcJwSDHNwV98157itA";

const API = "http://localhost:8000";

const DEFAULT_PARAMS = {
  beta: 0.25,
  sigma: 0.1667,
  gamma: 0.1,
  days: 200,
  dt: 0.5,
  seed_pct: 0.01,
  seed_county: "033",
  spillover_rate: 0.05,
  intervention_day: null,
  intervention_reduction: 0.0,
  use_vulnerability: true,
  sample_every: 5,
};

const CURVE_COLORS = { S: "#4a9eff", E: "#f5a623", I: "#e74c3c", R: "#2ecc71" };

export default function App() {
  const mapContainer = useRef(null);
  const map          = useRef(null);
  const playRef      = useRef(null);

  const [mapReady, setMapReady]         = useState(false);
  const [tracts, setTracts]             = useState(null);
  const [result, setResult]             = useState(null);
  const [loading, setLoading]           = useState(false);
  const [params, setParams]             = useState(DEFAULT_PARAMS);
  const [playDay, setPlayDay]           = useState(0);
  const [playing, setPlaying]           = useState(false);
  const [mapMode, setMapMode]           = useState("vuln");
  const [hoveredTract, setHoveredTract] = useState(null);
  const [statusMsg, setStatusMsg]       = useState("Load tracts to begin");
  const [compareResult, setCompareResult] = useState(null);
  const [compareParams, setCompareParams] = useState({ intervention_day: 60, intervention_reduction: 0.4 });
  const [compareLoading, setCompareLoading] = useState(false);
  const [showCompare, setShowCompare]   = useState(false);

  // ── Init map ──
  useEffect(() => {
    if (map.current) return;
    map.current = new mapboxgl.Map({
      container: mapContainer.current,
      style: "mapbox://styles/mapbox/dark-v11",
      center: [-120.5, 47.4],
      zoom: 6.2,
    });
    map.current.on("load", () => setMapReady(true));
    map.current.addControl(new mapboxgl.NavigationControl(), "top-right");
  }, []);

  // ── Load tracts ──
  useEffect(() => {
    if (!mapReady) return;
    axios.get(`${API}/tracts`).then(res => {
      setTracts(res.data);
      map.current.addSource("tracts", { type: "geojson", data: res.data });
      map.current.addLayer({
        id: "tracts-fill", type: "fill", source: "tracts",
        paint: {
          "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "vuln_blended"], 0],
            0, "#0d1b2a", 0.25, "#1a4a7a", 0.5, "#e67e22", 1, "#c0392b"],
          "fill-opacity": 0.75,
        },
      });
      map.current.addLayer({
        id: "tracts-outline", type: "line", source: "tracts",
        paint: { "line-color": "#ffffff", "line-width": 0.2, "line-opacity": 0.3 },
      });
      map.current.on("mousemove", "tracts-fill", e => {
        if (e.features.length) { setHoveredTract(e.features[0].properties); map.current.getCanvas().style.cursor = "pointer"; }
      });
      map.current.on("mouseleave", "tracts-fill", () => { setHoveredTract(null); map.current.getCanvas().style.cursor = ""; });
      setStatusMsg("Tracts loaded — configure parameters and run simulation");
    }).catch(() => setStatusMsg("⚠ Could not reach API — is the backend running?"));
  }, [mapReady]);

  // ── Update map colors ──
  useEffect(() => {
    if (!mapReady || !map.current.getSource("tracts") || !tracts) return;

    const updated = {
      ...tracts,
      features: tracts.features.map(f => {
        const geoid = f.properties.GEOID;
        let val = 0;

        if (!result || mapMode === "vuln") {
          val = f.properties.vuln_blended || 0;
        } else if (mapMode === "peak_I") {
          val = result.tract_metrics[geoid] ? result.tract_metrics[geoid].peak_I / 5000 : 0;
        } else if (mapMode === "attack") {
          val = result.tract_metrics[geoid] ? result.tract_metrics[geoid].attack_rate : 0;
        } else if (mapMode === "delta" && compareResult) {
          val = compareResult.delta[geoid] ? Math.min(compareResult.delta[geoid] * 5, 1) : 0;
        } else if (mapMode === "playback" && result.snapshots) {
          const snapDays = Object.keys(result.snapshots).map(Number).sort((a, b) => a - b);
          const nearestDay = snapDays.reduce((prev, curr) =>
            Math.abs(curr - playDay) < Math.abs(prev - playDay) ? curr : prev, snapDays[0]);
          const snap = result.snapshots[nearestDay] || {};
          const pop = f.properties.population || 1;
          val = Math.min((snap[geoid] || 0) / pop, 1);
        }

        return { ...f, properties: { ...f.properties, _val: Math.min(Math.max(val, 0), 1) } };
      }),
    };

    map.current.getSource("tracts").setData(updated);
    map.current.setPaintProperty("tracts-fill", "fill-color", [
      "interpolate", ["linear"], ["coalesce", ["get", "_val"], 0],
      0, "#0d1b2a", 0.05, "#1a3a5c", 0.2, "#1a6b8a", 0.5, "#e67e22", 1, "#c0392b"
    ]);
  }, [result, playDay, mapMode, mapReady, tracts, compareResult]);

  // ── Playback ──
  useEffect(() => {
    if (playing && result) {
      const maxDay = params.days - 1;
      playRef.current = setInterval(() => {
        setPlayDay(d => { if (d >= maxDay) { setPlaying(false); return maxDay; } return d + 1; });
      }, 40);
    } else {
      clearInterval(playRef.current);
    }
    return () => clearInterval(playRef.current);
  }, [playing, result, params.days]);

  // ── Run simulation ──
  const runSim = useCallback(async () => {
    setLoading(true);
    setStatusMsg("Running simulation…");
    setPlaying(false);
    setPlayDay(0);
    try {
      const res = await axios.post(`${API}/simulate`, { ...params, intervention_day: params.intervention_day || null });
      setResult(res.data);
      setMapMode("peak_I");
      const m = res.data.meta;
      setStatusMsg(`Done — Peak ${m.peak_I.toLocaleString(undefined, { maximumFractionDigits: 0 })} infectious on day ${m.peak_day} · Attack rate ${m.attack_rate}%`);
    } catch (e) {
      setStatusMsg("Simulation error — check backend terminal");
    }
    setLoading(false);
  }, [params]);

  // ── Run comparison ──
  const runCompare = useCallback(async () => {
    setCompareLoading(true);
    setStatusMsg("Running comparison...");
    try {
      const res = await axios.post(`${API}/simulate/compare`, {
        baseline:     { ...params, intervention_day: null, intervention_reduction: 0 },
        intervention: { ...params, ...compareParams },
        label_a: "Baseline",
        label_b: `Intervene day ${compareParams.intervention_day} (${Math.round(compareParams.intervention_reduction * 100)}% reduction)`,
      });
      setCompareResult(res.data);
      setMapMode("delta");
      setStatusMsg(`Comparison done — intervention saves ${(res.data.scenario_a.meta.attack_rate - res.data.scenario_b.meta.attack_rate).toFixed(1)}% attack rate`);
    } catch (e) {
      setStatusMsg("Compare error — check backend terminal");
    }
    setCompareLoading(false);
  }, [params, compareParams]);

  // ── Chart data ──
  const chartData = result
    ? result.curves.day.map((d, i) => ({
        day: d,
        S: Math.round(result.curves.S[i]),
        E: Math.round(result.curves.E[i]),
        I: Math.round(result.curves.I[i]),
        R: Math.round(result.curves.R[i]),
      }))
    : [];

  const setParam = (key, val) => setParams(p => ({ ...p, [key]: val }));

  return (
    <div className="app">
      <header className="header">
        <div className="header-left">
          <span className="header-tag">STRUCTURAL RESILIENCE LAB</span>
          <h1 className="header-title">Washington State Epidemic Simulator</h1>
        </div>
        <div className="header-status">{statusMsg}</div>
      </header>

      <div className="layout">
        <aside className="sidebar">

          <section className="panel">
            <h2 className="panel-title">Transmission</h2>
            <label className="param-label">
              <span>β (beta) <em>R₀ = {(params.beta / params.gamma).toFixed(1)}</em></span>
              <input type="range" min="0.05" max="0.8" step="0.01" value={params.beta} onChange={e => setParam("beta", +e.target.value)} />
              <span className="param-val">{params.beta}</span>
            </label>
            <label className="param-label">
              <span>Latent period (1/σ days)</span>
              <input type="range" min="1" max="14" step="1" value={Math.round(1 / params.sigma)} onChange={e => setParam("sigma", 1 / +e.target.value)} />
              <span className="param-val">{Math.round(1 / params.sigma)}d</span>
            </label>
            <label className="param-label">
              <span>Infectious period (1/γ days)</span>
              <input type="range" min="2" max="21" step="1" value={Math.round(1 / params.gamma)} onChange={e => setParam("gamma", 1 / +e.target.value)} />
              <span className="param-val">{Math.round(1 / params.gamma)}d</span>
            </label>
          </section>

          <section className="panel">
            <h2 className="panel-title">Intervention</h2>
            <label className="param-label">
              <span>Start day <em>{params.intervention_day ? `day ${params.intervention_day}` : "none"}</em></span>
              <input type="range" min="0" max={params.days} step="5" value={params.intervention_day || 0} onChange={e => setParam("intervention_day", +e.target.value || null)} />
              <span className="param-val">{params.intervention_day || "—"}</span>
            </label>
            <label className="param-label">
              <span>β reduction</span>
              <input type="range" min="0" max="0.95" step="0.05" value={params.intervention_reduction} onChange={e => setParam("intervention_reduction", +e.target.value)} />
              <span className="param-val">{Math.round(params.intervention_reduction * 100)}%</span>
            </label>
          </section>

          <section className="panel">
            <h2 className="panel-title">Simulation</h2>
            <label className="param-label">
              <span>Days</span>
              <input type="range" min="30" max="500" step="10" value={params.days} onChange={e => setParam("days", +e.target.value)} />
              <span className="param-val">{params.days}</span>
            </label>
            <label className="param-label">
              <span>Seed %</span>
              <input type="range" min="0.001" max="0.05" step="0.001" value={params.seed_pct} onChange={e => setParam("seed_pct", +e.target.value)} />
              <span className="param-val">{(params.seed_pct * 100).toFixed(1)}%</span>
            </label>
            <label className="check-label">
              <input type="checkbox" checked={params.use_vulnerability} onChange={e => setParam("use_vulnerability", e.target.checked)} />
              Apply vulnerability index
            </label>
          </section>

          <button className="run-btn" onClick={runSim} disabled={loading}>
            {loading ? "Simulating…" : "▶  Run Simulation"}
          </button>

          <button className="compare-toggle" onClick={() => setShowCompare(p => !p)}>
            {showCompare ? "▲ Hide Compare" : "⇄ Compare Scenarios"}
          </button>

          {showCompare && (
            <section className="panel">
              <h2 className="panel-title">Scenario B — Intervention</h2>
              <label className="param-label">
                <span>Intervention day</span>
                <input type="range" min="5" max={params.days} step="5" value={compareParams.intervention_day}
                  onChange={e => setCompareParams(p => ({ ...p, intervention_day: +e.target.value }))} />
                <span className="param-val">{compareParams.intervention_day}</span>
              </label>
              <label className="param-label">
                <span>β reduction</span>
                <input type="range" min="0.1" max="0.95" step="0.05" value={compareParams.intervention_reduction}
                  onChange={e => setCompareParams(p => ({ ...p, intervention_reduction: +e.target.value }))} />
                <span className="param-val">{Math.round(compareParams.intervention_reduction * 100)}%</span>
              </label>
              <button className="run-btn" onClick={runCompare} disabled={compareLoading} style={{ background: "#8e44ad" }}>
                {compareLoading ? "Comparing…" : "⇄ Run Comparison"}
              </button>
              {compareResult && (
                <div style={{ marginTop: 10 }}>
                  <div className="metric"><span>Baseline attack</span><strong>{compareResult.scenario_a.meta.attack_rate}%</strong></div>
                  <div className="metric"><span>Intervention attack</span><strong>{compareResult.scenario_b.meta.attack_rate}%</strong></div>
                  <div className="metric"><span>Lives protected</span><strong>{(compareResult.scenario_a.meta.total_R - compareResult.scenario_b.meta.total_R).toLocaleString(undefined, { maximumFractionDigits: 0 })}</strong></div>
                  <div className="metric"><span>Peak day shift</span><strong>+{compareResult.scenario_b.meta.peak_day - compareResult.scenario_a.meta.peak_day}d</strong></div>
                </div>
              )}
            </section>
          )}

          {result && (
            <section className="panel metrics">
              <h2 className="panel-title">Results</h2>
              <div className="metric"><span>Peak infectious</span><strong>{result.meta.peak_I.toLocaleString(undefined, { maximumFractionDigits: 0 })}</strong></div>
              <div className="metric"><span>Peak day</span><strong>{result.meta.peak_day}</strong></div>
              <div className="metric"><span>Total recovered</span><strong>{result.meta.total_R.toLocaleString(undefined, { maximumFractionDigits: 0 })}</strong></div>
              <div className="metric"><span>Attack rate</span><strong>{result.meta.attack_rate}%</strong></div>
              <div className="metric"><span>R₀</span><strong>{result.meta.R0}</strong></div>
            </section>
          )}

        </aside>

        <main className="main">
          <div className="map-controls">
            <div className="map-mode-btns">
              {[
                ["vuln",    "Vulnerability"],
                ["peak_I",  "Peak Infectious"],
                ["attack",  "Attack Rate"],
                ["delta",   "Intervention Δ"],
              ].map(([mode, label]) => (
                <button key={mode} className={`mode-btn ${mapMode === mode ? "active" : ""}`} onClick={() => setMapMode(mode)}>{label}</button>
              ))}
            </div>
            {result && (
              <div className="playback">
                <button className="play-btn" onClick={() => { setMapMode("playback"); setPlaying(p => !p); }}>
                  {playing ? "⏸" : "▶"}
                </button>
                <input type="range" min="0" max={params.days - 1} step="1"
                  value={playDay} onChange={e => { setPlaying(false); setPlayDay(+e.target.value); }} />
                <span className="day-label">Day {playDay}</span>
              </div>
            )}
          </div>

          <div className="map-wrap">
            <div ref={mapContainer} className="map" />
            {hoveredTract && (
              <div className="tract-tooltip">
                <div className="tt-geoid">{hoveredTract.GEOID}</div>
                <div className="tt-row"><span>Population</span><span>{Number(hoveredTract.population).toLocaleString()}</span></div>
                <div className="tt-row"><span>Vulnerability</span><span>{Number(hoveredTract.vuln_blended || 0).toFixed(3)}</span></div>
                {result && result.tract_metrics[hoveredTract.GEOID] && <>
                  <div className="tt-row"><span>Attack rate</span><span>{(result.tract_metrics[hoveredTract.GEOID].attack_rate * 100).toFixed(1)}%</span></div>
                  <div className="tt-row"><span>Peak day</span><span>{result.tract_metrics[hoveredTract.GEOID].peak_day}</span></div>
                </>}
              </div>
            )}
          </div>

          {result && (
            <div className="chart-wrap">
              <h3 className="chart-title">SEIR Curves — Washington State</h3>
              <ResponsiveContainer width="100%" height={180}>
                <LineChart data={chartData} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a2a3a" />
                  <XAxis dataKey="day" stroke="#666" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#666" tick={{ fontSize: 11 }} tickFormatter={v => v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : v} />
                  <Tooltip formatter={(v, n) => [v.toLocaleString(), n]} contentStyle={{ background: "#1a1a2e", border: "1px solid #333", borderRadius: 6 }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  {["S", "E", "I", "R"].map(k => (
                    <Line key={k} type="monotone" dataKey={k} stroke={CURVE_COLORS[k]} dot={false} strokeWidth={k === "I" ? 2.5 : 1.5} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}