import { useState, useEffect, useRef, useCallback } from "react";
import mapboxgl from "mapbox-gl";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, ReferenceLine,
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
} from "recharts";
import axios from "axios";
import "mapbox-gl/dist/mapbox-gl.css";
import "./App.css";

mapboxgl.accessToken = "pk.eyJ1IjoiY2hyaXN0b3BoZXJwaGFtIiwiYSI6ImNtcXZlbTRqZzEyeXEydXExZzl0aWJiaHMifQ.o58ZrcJwSDHNwV98157itA";

const API = "https://structural-resilience-production.up.railway.app";

const PRESETS = [
  { label: "COVID-19",  beta: 0.225, sigma: 0.1667, gamma: 0.1,   days: 200 },
  { label: "Influenza", beta: 0.15,  sigma: 0.2,    gamma: 0.143, days: 150 },
  { label: "Measles",   beta: 0.6,   sigma: 0.1,    gamma: 0.067, days: 300 },
];

const DEFAULT_PARAMS = {
  beta: 0.225, sigma: 0.1667, gamma: 0.1, days: 200, dt: 0.5,
  seed_pct: 0.01, seed_county: "033", spillover_rate: 0.05,
  intervention_day: null, intervention_reduction: 0.0,
  use_vulnerability: true, sample_every: 5,
};

const CURVE_COLORS = { S: "#3b82f6", E: "#f97316", I: "#ef4444", R: "#22c55e" };

const COUNTY_NAMES = {
  "001":"Adams","003":"Asotin","005":"Benton","007":"Chelan","009":"Clallam",
  "011":"Clark","013":"Columbia","015":"Cowlitz","017":"Douglas","019":"Ferry",
  "021":"Franklin","023":"Garfield","025":"Grant","027":"Grays Harbor","029":"Island",
  "031":"Jefferson","033":"King","035":"Kitsap","037":"Kittitas","039":"Klickitat",
  "041":"Lewis","043":"Lincoln","045":"Mason","047":"Okanogan","049":"Pacific",
  "051":"Pend Oreille","053":"Pierce","055":"San Juan","057":"Skagit","059":"Skamania",
  "061":"Snohomish","063":"Spokane","065":"Stevens","067":"Thurston","069":"Wahkiakum",
  "071":"Walla Walla","073":"Whatcom","075":"Whitman","077":"Yakima",
};

const LEGEND_CONFIG = {
  vuln:        { label: "Vulnerability",         low: "Low",  high: "High",  colors: ["#0d1b2a","#1e3a5f","#f97316","#dc2626"] },
  peak_I:      { label: "Peak Infectious Rate",  low: "0%",   high: "20%+",  colors: ["#0d1b2a","#1e3a5f","#1d6fa8","#f97316","#dc2626"] },
  attack:      { label: "Attack Rate",           low: "0%",   high: "100%",  colors: ["#0d1b2a","#1e3a5f","#1d6fa8","#f97316","#dc2626"] },
  delta:       { label: "Intervention Benefit",  low: "None", high: "High",  colors: ["#0d1b2a","#1e3a5f","#f97316","#dc2626"] },
  healthcare:  { label: "Hospital Distance",     low: "Near", high: "Far",   colors: ["#0d2a1a","#166534","#f97316","#dc2626"] },
  playback:    { label: "Active Infections",     low: "0%",   high: "High",  colors: ["#0d1b2a","#1e3a5f","#1d6fa8","#f97316","#dc2626"] },
  equity:      { label: "Equity Burden",         low: "Low",  high: "High",  colors: ["#0d1b2a","#1e3a5f","#7c3aed","#dc2626"] },
  resilience:  { label: "Structural Resilience", low: "Resilient", high: "Fragile", colors: ["#0d2a1a","#166534","#f97316","#dc2626"] },
};

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt  = n => n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
const pct  = n => n != null ? `${(n*100).toFixed(1)}%` : "—";
const dark = { background: "#111827", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 8, fontSize: 11 };

// ── Slider helper ─────────────────────────────────────────────────────────────
function Slider({ label, note, min, max, step, value, onChange, unit="" }) {
  return (
    <div className="d-slider">
      <div className="d-slider-head">
        <span className="d-slider-label">{label}{note && <em> {note}</em>}</span>
        <span className="d-slider-val">{value}{unit}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(+e.target.value)} />
    </div>
  );
}

// ── Map Legend ────────────────────────────────────────────────────────────────
function MapLegend({ mapMode }) {
  const cfg = LEGEND_CONFIG[mapMode] || LEGEND_CONFIG.vuln;
  return (
    <div className="map-legend">
      <div className="map-legend-label">{cfg.label}</div>
      <div className="map-legend-bar" style={{ background: `linear-gradient(to right,${cfg.colors.join(",")})` }} />
      <div className="map-legend-ticks"><span>{cfg.low}</span><span>{cfg.high}</span></div>
    </div>
  );
}

// ── Map Pills ─────────────────────────────────────────────────────────────────
function MapPills({ mapMode, setMapMode, result, playing, setPlaying, playDay, setPlayDay, days }) {
  const modes = [
    ["vuln","Vulnerability"],["peak_I","Peak I"],["attack","Attack Rate"],
    ["delta","Δ Intervention"],["healthcare","Hospital"],
    ["equity","Equity"],["resilience","Resilience"],
  ];
  return (
    <div className="map-pills-wrap">
      <div className="map-pills">
        {modes.map(([m, l]) => (
          <button key={m} className={`map-pill ${mapMode===m?"active":""}`}
            onClick={() => setMapMode(m)}>{l}</button>
        ))}
      </div>
      {result && (
        <div className="map-playback-pill">
          <button className="pb-btn" onClick={() => { setMapMode("playback"); setPlaying(p=>!p); }}>
            {playing ? "⏸" : "▶"}
          </button>
          <input type="range" min={0} max={days-1} step={1} value={playDay}
            onChange={e => { setPlaying(false); setMapMode("playback"); setPlayDay(+e.target.value); }} />
          <span className="pb-day">Day {playDay}</span>
        </div>
      )}
    </div>
  );
}

// ── Tract Card ────────────────────────────────────────────────────────────────
function TractCard({ tract, result, resilienceScores, onClose }) {
  if (!tract) return null;
  const metrics  = result?.tract_metrics?.[tract.GEOID];
  const res      = resilienceScores?.[tract.GEOID];
  const isSurge  = Number(tract.HubDist||0)>30000 && metrics?.peak_I/(Number(tract.population)||1)>0.1;
  return (
    <div className="tract-card">
      <div className="tc-head">
        <div>
          <div className="tc-geoid">{tract.GEOID}</div>
          <div className="tc-county">{COUNTY_NAMES[tract.COUNTYFP]||tract.COUNTYFP} County</div>
        </div>
        {onClose && <button className="tc-close" onClick={onClose}>✕</button>}
      </div>
      <div className="tc-rows">
        <div className="tc-row"><span>Population</span><span>{fmt(tract.population)}</span></div>
        <div className="tc-row"><span>Vulnerability</span><span>{Number(tract.vuln_blended||0).toFixed(3)}</span></div>
        <div className="tc-row"><span>Hospital dist.</span><span>{tract.HubDist?`${(Number(tract.HubDist)/1000).toFixed(1)} km`:"—"}</span></div>
        {metrics && <>
          <div className="tc-row"><span>Attack rate</span><span>{pct(metrics.attack_rate)}</span></div>
          <div className="tc-row"><span>Peak day</span><span>{metrics.peak_day}</span></div>
        </>}
        {res && <div className="tc-row"><span>Resilience score</span><span className={`tc-res ${res.tier}`}>{res.score.toFixed(3)} — {res.tier}</span></div>}
      </div>
      {isSurge && <div className="tc-surge">⚠ Surge Risk — {(Number(tract.HubDist)/1000).toFixed(0)} km from hospital</div>}
    </div>
  );
}

// ── Vulnerability Radar ───────────────────────────────────────────────────────
function VulnRadar({ tract, stateAvg }) {
  if (!tract) return null;
  const dims = [
    {key:"pct_65plus",label:"Age 65+"},
    {key:"pct_poverty",label:"Poverty"},
    {key:"pct_uninsured",label:"Uninsured"},
    {key:"pct_no_broadband",label:"No Broadband"},
    {key:"pct_limited_english",label:"Ltd. English"},
    {key:"pct_service_occ",label:"Service Occ."},
  ];
  const data = dims.map(d => ({
    dim: d.label,
    tract: Math.round(Number(tract[d.key]||0)*100),
    state: Math.round((stateAvg[d.key]||0)*100),
  }));
  return (
    <div className="radar-wrap">
      <div className="radar-title">Vulnerability Fingerprint</div>
      <ResponsiveContainer width="100%" height={175}>
        <RadarChart data={data}>
          <PolarGrid stroke="rgba(255,255,255,0.07)" />
          <PolarAngleAxis dataKey="dim" tick={{ fontSize: 8, fill: "#64748b" }} />
          <PolarRadiusAxis angle={90} domain={[0,50]} tick={{ fontSize: 7, fill: "#475569" }} />
          <Radar name="Tract" dataKey="tract" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.2} />
          <Radar name="State avg" dataKey="state" stroke="#f97316" fill="#f97316" fillOpacity={0.08} strokeDasharray="4 2" />
          <Legend wrapperStyle={{ fontSize: 9, color: "#64748b" }} />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── ROI Calculator ────────────────────────────────────────────────────────────
function ROICalculator({ result, compareResult }) {
  if (!result || !compareResult) return (
    <div className="roi-empty">Run a scenario comparison to see the Intervention ROI Calculator.</div>
  );

  const a = compareResult.scenario_a.meta;
  const b = compareResult.scenario_b.meta;
  const saved     = Math.round(a.total_R - b.total_R);
  const peakCut   = Math.round(a.peak_I  - b.peak_I);
  const peakDelay = b.peak_day - a.peak_day;
  const reduction = compareResult.intervention?.intervention_reduction ||
                    (1 - b.attack_rate / a.attack_rate);

  // Policy equivalents
  const vaccineEq   = Math.round(saved / 0.70);
  const maskEq      = Math.round(reduction * 100 * 0.8);
  const iculBeds    = Math.round(peakCut * 0.05);

  const roiRows = [
    { label: "Total infections prevented", value: fmt(saved), color: "#22c55e" },
    { label: "Peak infectious reduced by",  value: fmt(peakCut), color: "#22c55e" },
    { label: "Peak delayed by",             value: `${peakDelay} days`, color: "#3b82f6" },
    { label: "Attack rate reduction",       value: `${(a.attack_rate - b.attack_rate).toFixed(1)}%`, color: "#3b82f6" },
  ];

  const equivRows = [
    { label: "Equivalent to vaccinating", value: `${fmt(vaccineEq)} people at 70% efficacy`, icon: "💉" },
    { label: "Or achieving mask compliance of", value: `~${maskEq}% of population`, icon: "😷" },
    { label: "ICU bed-days freed", value: `~${fmt(iculBeds)} bed-days`, icon: "🏥" },
  ];

  return (
    <div className="roi-wrap">
      <div className="roi-section-label">Outcome Metrics</div>
      <div className="roi-grid">
        {roiRows.map(r => (
          <div key={r.label} className="roi-card">
            <span>{r.label}</span>
            <strong style={{ color: r.color }}>{r.value}</strong>
          </div>
        ))}
      </div>

      <div className="roi-section-label" style={{ marginTop: 16 }}>Policy Equivalents</div>
      <div className="roi-equiv">
        {equivRows.map(r => (
          <div key={r.label} className="roi-equiv-row">
            <span className="roi-icon">{r.icon}</span>
            <div>
              <div className="roi-equiv-label">{r.label}</div>
              <div className="roi-equiv-value">{r.value}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="roi-caveat">
        Policy equivalents are illustrative estimates. Vaccine equivalence assumes 70% efficacy and no waning.
        ICU estimate uses 5% hospitalization rate of prevented cases.
      </div>
    </div>
  );
}

// ── Counterfactual Timeline ───────────────────────────────────────────────────
function CounterfactualTimeline({ params, cfResult, setCfResult, setCfLoading, cfLoading }) {
  const [cfDay, setCfDay]         = useState(60);
  const [cfReduce, setCfReduce]   = useState(40);

  const run = async () => {
    setCfLoading(true);
    try {
      const res = await axios.post(`${API}/simulate/counterfactual`, {
        ...params,
        intervention_day: cfDay,
        intervention_reduction: cfReduce / 100,
        intervention_day_null: null,
      });
      setCfResult(res.data);
    } catch(e) { console.error(e); }
    setCfLoading(false);
  };

  const chartData = cfResult
    ? cfResult.baseline.curves.day.map((d,i) => ({
        day: d,
        Baseline:     Math.round(cfResult.baseline.curves.I[i]),
        Intervention: Math.round(cfResult.intervention.curves.I[i]),
        Saved:        Math.max(0, Math.round(cfResult.lives_saved[i])),
      }))
    : [];

  return (
    <div className="cf-wrap">
      <div className="cf-controls">
        <div className="cf-sliders">
          <Slider label="Intervene day" min={1} max={params.days-10} step={1}
            value={cfDay} onChange={setCfDay} />
          <Slider label="β reduction" min={5} max={95} step={5}
            value={cfReduce} onChange={setCfReduce} unit="%" />
        </div>
        <button className="cf-run-btn" onClick={run} disabled={cfLoading}>
          {cfLoading ? "Computing…" : "↺  Compute"}
        </button>
      </div>

      {cfResult && (
        <>
          <div className="cf-kpis">
            <div className="cf-kpi">
              <span>Lives protected</span>
              <strong style={{color:"#22c55e"}}>{fmt(cfResult.summary.total_lives_saved)}</strong>
            </div>
            <div className="cf-kpi">
              <span>Peak reduced</span>
              <strong style={{color:"#3b82f6"}}>{fmt(cfResult.summary.peak_reduction)}</strong>
            </div>
            <div className="cf-kpi">
              <span>Peak delayed</span>
              <strong style={{color:"#a855f7"}}>{cfResult.summary.peak_delay_days}d</strong>
            </div>
            <div className="cf-kpi">
              <span>Attack rate</span>
              <strong style={{color:"#ef4444"}}>{cfResult.summary.attack_rate_base}% → {cfResult.summary.attack_rate_intv}%</strong>
            </div>
          </div>

          <div className="cf-chart-label">Infectious Curves + Cumulative Lives Saved</div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={chartData} margin={{ top:4,right:20,left:0,bottom:0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="day" stroke="#334155" tick={{ fontSize:10,fill:"#64748b" }} />
              <YAxis yAxisId="left"  stroke="#334155" tick={{ fontSize:10,fill:"#64748b" }}
                tickFormatter={v => v>=1e6?`${(v/1e6).toFixed(1)}M`:v>=1e3?`${(v/1e3).toFixed(0)}k`:v} />
              <YAxis yAxisId="right" orientation="right" stroke="#334155" tick={{ fontSize:10,fill:"#64748b" }}
                tickFormatter={v => v>=1e6?`${(v/1e6).toFixed(1)}M`:v>=1e3?`${(v/1e3).toFixed(0)}k`:v} />
              <Tooltip formatter={(v,n)=>[fmt(v),n]} contentStyle={dark} />
              <Legend wrapperStyle={{ fontSize:11 }} />
              <ReferenceLine yAxisId="left" x={cfDay} stroke="#f97316" strokeDasharray="4 2"
                label={{ value:`Day ${cfDay}`, fill:"#f97316", fontSize:10 }} />
              <Line yAxisId="left"  type="monotone" dataKey="Baseline"     stroke="#ef4444" strokeWidth={2} dot={false} />
              <Line yAxisId="left"  type="monotone" dataKey="Intervention" stroke="#3b82f6" strokeWidth={2} dot={false} strokeDasharray="5 3" />
              <Line yAxisId="right" type="monotone" dataKey="Saved"        stroke="#22c55e" strokeWidth={1.5} dot={false} />
            </LineChart>
          </ResponsiveContainer>
          <div className="cf-note">Orange line = intervention start. Green = cumulative lives saved (right axis).</div>
        </>
      )}

      {!cfResult && !cfLoading && (
        <div className="cf-empty">Set parameters and click Compute to see the counterfactual timeline.</div>
      )}
    </div>
  );
}

// ── Structural Resilience Score ───────────────────────────────────────────────
function computeResilienceScores(tracts, result) {
  if (!tracts || !result) return null;
  const scores = {};
  const attackRates = Object.values(result.tract_metrics).map(m => m.attack_rate);
  const maxAttack   = Math.max(...attackRates) || 1;

  tracts.features.forEach(f => {
    const p     = f.properties;
    const geoid = p.GEOID;
    const m     = result.tract_metrics[geoid];
    if (!m) return;

    const attackNorm = m.attack_rate / maxAttack;
    const vulnScore  = Number(p.vuln_blended || 0);
    const hubNorm    = Number(p.hub_dist_norm || 0);
    const surgeNorm  = Number(p.surge_risk    || 0);

    // Structural Resilience Index — higher = more fragile
    const fragility = (
      0.35 * attackNorm  +
      0.25 * vulnScore   +
      0.25 * hubNorm     +
      0.15 * surgeNorm
    );

    const resilience = 1 - fragility;

    scores[geoid] = {
      score:    resilience,
      fragility,
      tier: resilience > 0.7 ? "resilient"
          : resilience > 0.4 ? "moderate"
          : "fragile",
    };
  });
  return scores;
}

function ResiliencePanel({ resilienceScores, tracts }) {
  if (!resilienceScores || !tracts) return (
    <div className="roi-empty">Run a simulation to compute Structural Resilience Scores.</div>
  );

  const rows = tracts.features
    .map(f => ({
      geoid:    f.properties.GEOID,
      county:   COUNTY_NAMES[f.properties.COUNTYFP] || f.properties.COUNTYFP,
      pop:      Number(f.properties.population || 0),
      ...resilienceScores[f.properties.GEOID],
    }))
    .filter(r => r.score != null)
    .sort((a,b) => a.score - b.score)
    .slice(0, 25);

  const tierCounts = Object.values(resilienceScores).reduce((acc, r) => {
    if (r) acc[r.tier] = (acc[r.tier]||0) + 1;
    return acc;
  }, {});

  return (
    <div className="res-wrap">
      <div className="res-summary">
        <div className="res-stat fragile"><span>Fragile</span><strong>{tierCounts.fragile||0}</strong></div>
        <div className="res-stat moderate"><span>Moderate</span><strong>{tierCounts.moderate||0}</strong></div>
        <div className="res-stat resilient"><span>Resilient</span><strong>{tierCounts.resilient||0}</strong></div>
      </div>
      <div className="roi-caveat" style={{marginBottom:10}}>
        Structural Resilience Index combines simulated attack rate, vulnerability, hospital distance, and surge risk.
        Score 0 = maximally fragile, 1 = maximally resilient.
      </div>
      <div className="eq-table-wrap">
        <table className="eq-table">
          <thead><tr>
            <th>#</th><th>County</th><th>Pop</th><th>Score</th><th>Tier</th>
          </tr></thead>
          <tbody>
            {rows.map((r,i) => (
              <tr key={r.geoid}>
                <td>{i+1}</td>
                <td>{r.county}</td>
                <td>{fmt(r.pop)}</td>
                <td><span className={`res-badge ${r.tier}`}>{r.score?.toFixed(3)}</span></td>
                <td><span className={`res-tier ${r.tier}`}>{r.tier}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Compare Modal ─────────────────────────────────────────────────────────────
function CompareModal({ compareResult, tracts, onClose }) {
  const mapARef = useRef(null); const mapBRef = useRef(null);
  const mapAObj = useRef(null); const mapBObj = useRef(null);

  useEffect(() => {
    if (!compareResult || !tracts) return;
    const init = (el, ref, mode) => {
      if (ref.current) return;
      ref.current = new mapboxgl.Map({ container: el, style:"mapbox://styles/mapbox/dark-v11", center:[-120.5,47.4], zoom:5.8 });
      ref.current.on("load", () => {
        const colored = { ...tracts, features: tracts.features.map(f => {
          const g = f.properties.GEOID;
          const v = mode==="base"
            ? (compareResult.scenario_a.tract_metrics[g]?.attack_rate||0)
            : Math.min((compareResult.delta[g]||0)*4,1);
          return {...f, properties:{...f.properties, _v: Math.min(Math.max(v,0),1)}};
        })};
        ref.current.addSource("t",{type:"geojson",data:colored});
        ref.current.addLayer({ id:"tf",type:"fill",source:"t", paint:{
          "fill-color": mode==="base"
            ? ["interpolate",["linear"],["coalesce",["get","_v"],0],0,"#0d1b2a",0.3,"#1d6fa8",0.7,"#f97316",1,"#dc2626"]
            : ["interpolate",["linear"],["coalesce",["get","_v"],0],0,"#0d1b2a",0.4,"#166534",0.8,"#22c55e",1,"#4ade80"],
          "fill-opacity":0.85 }});
        ref.current.addLayer({id:"tl",type:"line",source:"t",paint:{"line-color":"#fff","line-width":0.2,"line-opacity":0.12}});
      });
    };
    setTimeout(()=>{ if(mapARef.current) init(mapARef.current,mapAObj,"base"); if(mapBRef.current) init(mapBRef.current,mapBObj,"delta"); },80);
    return ()=>{ if(mapAObj.current){mapAObj.current.remove();mapAObj.current=null;} if(mapBObj.current){mapBObj.current.remove();mapBObj.current=null;} };
  },[compareResult,tracts]);

  if (!compareResult) return null;
  const a=compareResult.scenario_a.meta, b=compareResult.scenario_b.meta;
  const saved=Math.round(a.total_R-b.total_R);
  const cd = compareResult.scenario_a.curves.day.map((d,i)=>({
    day:d, Baseline:Math.round(compareResult.scenario_a.curves.I[i]),
    Intervention:Math.round(compareResult.scenario_b.curves.I[i]),
  }));

  return (
    <div className="modal-overlay" onClick={e=>e.target===e.currentTarget&&onClose()}>
      <div className="modal">
        <div className="modal-head">
          <div><div className="modal-eyebrow">Scenario Comparison</div>
          <h2 className="modal-title">{compareResult.label_a} vs {compareResult.label_b}</h2></div>
          <button className="modal-x" onClick={onClose}>✕</button>
        </div>
        <div className="modal-kpis">
          <div className="kpi-group">
            <div className="kpi-group-label">Baseline</div>
            {[["Attack rate",`${a.attack_rate}%`],["Peak infectious",fmt(a.peak_I)],["Peak day",a.peak_day],["Total infected",fmt(a.total_R)]].map(([l,v])=>(
              <div key={l} className="kpi"><span>{l}</span><strong>{v}</strong></div>
            ))}
          </div>
          <div className="kpi-delta">
            <div className="kpi-delta-row red">−{(a.attack_rate-b.attack_rate).toFixed(1)}%<span>attack rate</span></div>
            <div className="kpi-delta-row green">+{b.peak_day-a.peak_day}d<span>peak delayed</span></div>
            <div className="kpi-delta-row green">{fmt(saved)}<span>protected</span></div>
          </div>
          <div className="kpi-group right">
            <div className="kpi-group-label green">Intervention</div>
            {[["Attack rate",`${b.attack_rate}%`],["Peak infectious",fmt(b.peak_I)],["Peak day",b.peak_day],["Total infected",fmt(b.total_R)]].map(([l,v])=>(
              <div key={l} className="kpi"><span>{l}</span><strong>{v}</strong></div>
            ))}
          </div>
        </div>
        <div className="modal-section">
          <div className="modal-section-label">Infectious Curves</div>
          <ResponsiveContainer width="100%" height={150}>
            <LineChart data={cd} margin={{top:4,right:20,left:0,bottom:0}}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="day" stroke="#334155" tick={{fontSize:10,fill:"#64748b"}} />
              <YAxis stroke="#334155" tick={{fontSize:10,fill:"#64748b"}}
                tickFormatter={v=>v>=1e6?`${(v/1e6).toFixed(1)}M`:v>=1e3?`${(v/1e3).toFixed(0)}k`:v} />
              <Tooltip formatter={(v,n)=>[fmt(v),n]} contentStyle={dark} />
              <Legend wrapperStyle={{fontSize:11}} />
              <Line type="monotone" dataKey="Baseline"     stroke="#ef4444" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="Intervention" stroke="#22c55e" strokeWidth={2} dot={false} strokeDasharray="5 3" />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="modal-maps">
          <div className="modal-map-col"><div className="modal-section-label">Baseline Attack Rate</div><div ref={mapARef} className="modal-map"/></div>
          <div className="modal-map-col"><div className="modal-section-label">Intervention Benefit</div><div ref={mapBRef} className="modal-map"/></div>
        </div>
      </div>
    </div>
  );
}

// ── Equity Modal ──────────────────────────────────────────────────────────────
function EquityModal({ equityData, weaknessData, onClose }) {
  const [tab, setTab] = useState("burden");
  if (!equityData && !weaknessData) return null;
  return (
    <div className="modal-overlay" onClick={e=>e.target===e.currentTarget&&onClose()}>
      <div className="modal modal-equity">
        <div className="modal-head">
          <div><div className="modal-eyebrow">Equity Analysis</div>
          <h2 className="modal-title">Health Equity Impact Report</h2></div>
          <button className="modal-x" onClick={onClose}>✕</button>
        </div>
        <div className="eq-tabs">
          <button className={`eq-tab ${tab==="burden"?"active":""}`} onClick={()=>setTab("burden")}>Top Burdened Tracts</button>
          <button className={`eq-tab ${tab==="weakness"?"active":""}`} onClick={()=>setTab("weakness")}>Structural Weaknesses</button>
        </div>
        {tab==="burden" && equityData && (
          <div className="eq-body">
            <div className="eq-summary">
              <div className="eq-stat"><span>High-risk tracts</span><strong>{equityData.summary.high_risk_count}</strong></div>
              <div className="eq-stat"><span>Mean attack rate</span><strong>{(equityData.summary.mean_attack*100).toFixed(1)}%</strong></div>
              <div className="eq-stat"><span>Mean burden</span><strong>{equityData.summary.mean_burden.toFixed(4)}</strong></div>
            </div>
            <div className="eq-table-wrap"><table className="eq-table">
              <thead><tr><th>#</th><th>County</th><th>Pop</th><th>Attack</th><th>Vuln</th><th>Hosp.</th><th>Burden</th><th>Peak</th></tr></thead>
              <tbody>{equityData.top_burdened.map((r,i)=>(
                <tr key={r.GEOID} className={i<5?"high":""}>
                  <td>{i+1}</td><td>{COUNTY_NAMES[r.COUNTYFP]||r.COUNTYFP}</td>
                  <td>{fmt(r.population)}</td><td>{pct(r.attack_rate)}</td>
                  <td>{r.vuln_blended.toFixed(3)}</td><td>{r.hub_dist_km}km</td>
                  <td className="burden-cell">{r.equity_burden.toFixed(4)}</td><td>{r.peak_day}</td>
                </tr>
              ))}</tbody>
            </table></div>
          </div>
        )}
        {tab==="weakness" && weaknessData && (
          <div className="eq-body">
            <p className="eq-note">Tracts with high vulnerability, poor hospital access, and elevated surge risk.</p>
            <div className="eq-table-wrap"><table className="eq-table">
              <thead><tr><th>#</th><th>County</th><th>Pop</th><th>Vuln</th><th>Surge</th><th>Poverty</th><th>Uninsured</th><th>Score</th></tr></thead>
              <tbody>{weaknessData.map((r,i)=>(
                <tr key={r.GEOID} className={i<5?"high":""}>
                  <td>{i+1}</td><td>{COUNTY_NAMES[r.COUNTYFP]||r.COUNTYFP}</td>
                  <td>{fmt(r.population)}</td><td>{Number(r.vuln_blended).toFixed(3)}</td>
                  <td>{Number(r.surge_risk||0).toFixed(3)}</td><td>{pct(r.pct_poverty)}</td>
                  <td>{pct(r.pct_uninsured)}</td><td className="burden-cell">{Number(r.structural_score||0).toFixed(3)}</td>
                </tr>
              ))}</tbody>
            </table></div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Bottom Drawer ─────────────────────────────────────────────────────────────
function BottomDrawer({ result, compareResult, params, open, onToggle }) {
  const m = result?.meta;
  const preset = PRESETS.find(p => Math.abs(p.beta-params.beta)<0.01);
  const narrative = m
    ? `Under a ${preset?.label||"custom"} scenario (R₀ = ${m.R0}), the epidemic peaks at ${fmt(m.peak_I)} simultaneous infections on day ${m.peak_day}, with a statewide attack rate of ${m.attack_rate}%. ${fmt(m.total_R)} people are infected over ${params.days} days.${compareResult?` Early intervention protects ${fmt(Math.round(compareResult.scenario_a.meta.total_R-compareResult.scenario_b.meta.total_R))} people and delays peak by ${compareResult.scenario_b.meta.peak_day-compareResult.scenario_a.meta.peak_day} days.`:""}` : null;

  const cd = result ? result.curves.day.map((d,i)=>({
    day:d, S:Math.round(result.curves.S[i]), E:Math.round(result.curves.E[i]),
    I:Math.round(result.curves.I[i]), R:Math.round(result.curves.R[i]),
  })) : [];

  return (
    <div className={`bottom-drawer ${open?"open":""}`}>
      <button className="drawer-toggle" onClick={onToggle}>
        {open ? "▼ Hide Analysis" : "▲ Show Analysis"}
        {result && !open && <span className="drawer-peek"> · Peak {fmt(m?.peak_I)} day {m?.peak_day} · {m?.attack_rate}% attack</span>}
      </button>
      {open && result && (
        <div className="drawer-body">
          <div className="drawer-narrative">
            <div className="drawer-narrative-label">Scenario Summary</div>
            <p>{narrative}</p>
          </div>
          <div className="drawer-chart">
            <div className="drawer-chart-label">SEIR Curves — Washington State</div>
            <ResponsiveContainer width="100%" height={148}>
              <LineChart data={cd} margin={{top:4,right:20,left:0,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis dataKey="day" stroke="#334155" tick={{fontSize:10,fill:"#64748b"}} />
                <YAxis stroke="#334155" tick={{fontSize:10,fill:"#64748b"}}
                  tickFormatter={v=>v>=1e6?`${(v/1e6).toFixed(1)}M`:v>=1e3?`${(v/1e3).toFixed(0)}k`:v} />
                <Tooltip formatter={(v,n)=>[fmt(v),n]} contentStyle={dark} />
                <Legend wrapperStyle={{fontSize:11}} />
                {["S","E","I","R"].map(k=>(
                  <Line key={k} type="monotone" dataKey={k} stroke={CURVE_COLORS[k]} dot={false} strokeWidth={k==="I"?2.5:1.5} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = [
  { id:"model",    icon:"⚙",  label:"Model"      },
  { id:"scenario", icon:"↔",  label:"Compare"    },
  { id:"phase5",   icon:"◈",  label:"Insights"   },
  { id:"results",  icon:"◉",  label:"Results"    },
  { id:"equity",   icon:"⊕",  label:"Equity"     },
];

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const mapContainer = useRef(null);
  const map          = useRef(null);
  const playRef      = useRef(null);

  const [mapReady,         setMapReady]         = useState(false);
  const [tracts,           setTracts]           = useState(null);
  const [stateAvg,         setStateAvg]         = useState({});
  const [result,           setResult]           = useState(null);
  const [loading,          setLoading]          = useState(false);
  const [params,           setParams]           = useState(DEFAULT_PARAMS);
  const [playDay,          setPlayDay]          = useState(0);
  const [playing,          setPlaying]          = useState(false);
  const [mapMode,          setMapMode]          = useState("vuln");
  const [hoveredTract,     setHoveredTract]     = useState(null);
  const [selectedTract,    setSelectedTract]    = useState(null);
  const [statusMsg,        setStatusMsg]        = useState("Ready");
  const [compareResult,    setCompareResult]    = useState(null);
  const [compareParams,    setCompareParams]    = useState({ intervention_day:60, intervention_reduction:0.4 });
  const [compareLoading,   setCompareLoading]   = useState(false);
  const [showCompareModal, setShowCompareModal] = useState(false);
  const [showSurge,        setShowSurge]        = useState(false);
  const [equityData,       setEquityData]       = useState(null);
  const [weaknessData,     setWeaknessData]     = useState(null);
  const [equityLoading,    setEquityLoading]    = useState(false);
  const [showEquityModal,  setShowEquityModal]  = useState(false);
  const [activeTab,        setActiveTab]        = useState("model");
  const [drawerOpen,       setDrawerOpen]       = useState(false);
  const [cfResult,         setCfResult]         = useState(null);
  const [cfLoading,        setCfLoading]        = useState(false);
  const [resilienceScores, setResilienceScores] = useState(null);
  const [phase5Tab,        setPhase5Tab]        = useState("roi");

// Init map
useEffect(() => {
  if (map.current) return;
  setTimeout(() => {
    if (mapContainer.current) {
      map.current = new mapboxgl.Map({
        container: mapContainer.current,
        style: "mapbox://styles/mapbox/dark-v11",
        center: [-120.5, 47.4],
        zoom: 6.2,
      });
      map.current.on("load", () => {
        setMapReady(true);
        map.current.resize();
      });
      map.current.addControl(new mapboxgl.NavigationControl(), "top-right");
    }
  }, 500);
}, []);

  // Wait until container has non-zero width
  const observer = new ResizeObserver(entries => {
    for (const entry of entries) {
      if (entry.contentRect.width > 0) {
        observer.disconnect();
        initMap();
        break;
      }
    }
  });
  observer.observe(container);

  return () => observer.disconnect();
}, []);

  // Load tracts
  useEffect(() => {
    if (!mapReady) return;
    axios.get(`${API}/tracts`).then(res => {
      setTracts(res.data);
      const features = res.data.features;
      const dims = ["pct_65plus","pct_poverty","pct_uninsured","pct_no_broadband","pct_limited_english","pct_service_occ"];
      const avg = {};
      dims.forEach(d => { avg[d] = features.reduce((s,f)=>s+Number(f.properties[d]||0),0)/features.length; });
      setStateAvg(avg);
      map.current.addSource("tracts",{type:"geojson",data:res.data});
      map.current.addLayer({id:"tracts-fill",type:"fill",source:"tracts",paint:{
        "fill-color":["interpolate",["linear"],["coalesce",["get","vuln_blended"],0],0,"#0d1b2a",0.25,"#1e3a5f",0.5,"#f97316",1,"#dc2626"],
        "fill-opacity":0.75}});
      map.current.addLayer({id:"tracts-line",type:"line",source:"tracts",paint:{"line-color":"#fff","line-width":0.2,"line-opacity":0.2}});
      map.current.addLayer({id:"surge-outline",type:"line",source:"tracts",paint:{"line-color":"#ef4444","line-width":2.5,"line-opacity":0.9},filter:["==",["get","GEOID"],""]});
      map.current.addLayer({id:"sel-outline",type:"line",source:"tracts",paint:{"line-color":"#fff","line-width":2,"line-opacity":1},filter:["==",["get","GEOID"],""]});
      map.current.on("mousemove","tracts-fill",e=>{if(e.features.length){setHoveredTract(e.features[0].properties);map.current.getCanvas().style.cursor="pointer";}});
      map.current.on("mouseleave","tracts-fill",()=>{setHoveredTract(null);map.current.getCanvas().style.cursor="";});
      map.current.on("click","tracts-fill",e=>{if(e.features.length){const p=e.features[0].properties;setSelectedTract(p);map.current.setFilter("sel-outline",["==",["get","GEOID"],p.GEOID]);}});
      setStatusMsg("Tracts loaded");
    }).catch(()=>setStatusMsg("⚠ API unreachable"));
  }, [mapReady]);

  // Update map colors
  useEffect(() => {
    if (!mapReady||!map.current.getSource("tracts")||!tracts) return;
    const updated = { ...tracts, features: tracts.features.map(f => {
      const g=f.properties.GEOID; let val=0;
      if (!result||mapMode==="vuln")               val=f.properties.vuln_blended||0;
      else if (mapMode==="peak_I")                 val=result.tract_metrics[g]?result.tract_metrics[g].peak_I/5000:0;
      else if (mapMode==="attack")                 val=result.tract_metrics[g]?result.tract_metrics[g].attack_rate:0;
      else if (mapMode==="delta"&&compareResult)   val=Math.min((compareResult.delta[g]||0)*5,1);
      else if (mapMode==="healthcare")             val=f.properties.hub_dist_norm||0;
      else if (mapMode==="equity"&&equityData)     { const eq=equityData.all_tracts?.find(r=>r.GEOID===g); val=eq?Math.min(eq.equity_burden*20,1):0; }
      else if (mapMode==="resilience"&&resilienceScores) val=resilienceScores[g]?resilienceScores[g].fragility:0;
      else if (mapMode==="playback"&&result.snapshots) {
        const sds=Object.keys(result.snapshots).map(Number).sort((a,b)=>a-b);
        const nd=sds.reduce((p,c)=>Math.abs(c-playDay)<Math.abs(p-playDay)?c:p,sds[0]);
        val=Math.min(((result.snapshots[nd]||{})[g]||0)/(f.properties.population||1),1);
      }
      return {...f,properties:{...f.properties,_val:Math.min(Math.max(val,0),1)}};
    })};
    map.current.getSource("tracts").setData(updated);
    const ramp = mapMode==="healthcare"
      ? ["interpolate",["linear"],["coalesce",["get","_val"],0],0,"#0d2a1a",0.3,"#166534",0.6,"#f97316",1,"#dc2626"]
      : mapMode==="equity"
      ? ["interpolate",["linear"],["coalesce",["get","_val"],0],0,"#0d1b2a",0.2,"#1e3a5f",0.5,"#7c3aed",1,"#dc2626"]
      : ["interpolate",["linear"],["coalesce",["get","_val"],0],0,"#0d1b2a",0.05,"#1e3a5f",0.2,"#1d6fa8",0.5,"#f97316",1,"#dc2626"];
    map.current.setPaintProperty("tracts-fill","fill-color",ramp);
    if (showSurge&&result&&map.current.getLayer("surge-outline")) {
      const ids=tracts.features.filter(f=>Number(f.properties.HubDist||0)>30000&&result.tract_metrics[f.properties.GEOID]?.peak_I/(Number(f.properties.population)||1)>0.1).map(f=>f.properties.GEOID);
      map.current.setFilter("surge-outline",ids.length>0?["in",["get","GEOID"],["literal",ids]]:["==",["get","GEOID"],""]);
    } else if (map.current.getLayer("surge-outline")) map.current.setFilter("surge-outline",["==",["get","GEOID"],""]);
  }, [result,playDay,mapMode,mapReady,tracts,compareResult,showSurge,equityData,resilienceScores]);

  // Playback
  useEffect(() => {
    if (playing&&result) { playRef.current=setInterval(()=>{setPlayDay(d=>{if(d>=params.days-1){setPlaying(false);return params.days-1;}return d+1;});},40); }
    else clearInterval(playRef.current);
    return ()=>clearInterval(playRef.current);
  },[playing,result,params.days]);

  const runSim = useCallback(async () => {
    setLoading(true); setStatusMsg("Running simulation…"); setPlaying(false); setPlayDay(0);
    try {
      const res = await axios.post(`${API}/simulate`,{...params,intervention_day:params.intervention_day||null});
      setResult(res.data); setMapMode("peak_I"); setDrawerOpen(true); setActiveTab("results");
      const scores = computeResilienceScores(tracts, res.data);
      setResilienceScores(scores);
      const m=res.data.meta;
      setStatusMsg(`Peak ${fmt(m.peak_I)} · Day ${m.peak_day} · ${m.attack_rate}% attack rate`);
    } catch { setStatusMsg("Simulation error"); }
    setLoading(false);
  },[params,tracts]);

  const runCompare = useCallback(async () => {
    setCompareLoading(true); setStatusMsg("Comparing scenarios…");
    try {
      const res = await axios.post(`${API}/simulate/compare`,{
        baseline:{...params,intervention_day:null,intervention_reduction:0},
        intervention:{...params,...compareParams},
        label_a:"Baseline",
        label_b:`Day ${compareParams.intervention_day} / −${Math.round(compareParams.intervention_reduction*100)}%`,
      });
      setCompareResult(res.data); setMapMode("delta"); setShowCompareModal(true);
      setStatusMsg(`Saves ${(res.data.scenario_a.meta.attack_rate-res.data.scenario_b.meta.attack_rate).toFixed(1)}% attack rate`);
    } catch { setStatusMsg("Comparison error"); }
    setCompareLoading(false);
  },[params,compareParams]);

  const runEquity = useCallback(async () => {
    setEquityLoading(true); setStatusMsg("Running equity analysis…");
    try {
      const [ir,wr]=await Promise.all([
        axios.post(`${API}/equity/impact`,{...params,intervention_day:params.intervention_day||null}),
        axios.get(`${API}/equity/structural-weakness`),
      ]);
      setEquityData(ir.data); setWeaknessData(wr.data); setShowEquityModal(true); setMapMode("equity");
      setStatusMsg(`${ir.data.summary.high_risk_count} high-risk tracts identified`);
    } catch { setStatusMsg("Equity error"); }
    setEquityLoading(false);
  },[params]);

  const exportCSV = () => {
    if (!result) return;
    const rows=result.curves.day.map((d,i)=>`${d},${Math.round(result.curves.S[i])},${Math.round(result.curves.E[i])},${Math.round(result.curves.I[i])},${Math.round(result.curves.R[i])}`);
    const blob=new Blob([["day,S,E,I,R",...rows].join("\n")],{type:"text/csv"});
    const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="seir.csv"; a.click();
  };

  const setParam = (k,v) => setParams(p=>({...p,[k]:v}));
  const display  = selectedTract || hoveredTract;

  return (
    <div className="app">
      <header className="header">
        <div className="header-left">
          <span className="header-tag">Structural Resilience Lab</span>
          <h1 className="header-title">Washington State Epidemic Simulator</h1>
        </div>
        <div className="header-center"><span className="header-status">{statusMsg}</span></div>
        <div className="header-right">
          {compareResult && <button className="hdr-btn purple" onClick={()=>setShowCompareModal(true)}>↔ Comparison</button>}
          {result && <button className="hdr-btn" onClick={exportCSV}>↓ Export</button>}
        </div>
      </header>

      <div className="layout">
        {/* Tab Rail */}
        <div className="tab-rail">
          {TABS.map(t=>(
            <button key={t.id} className={`tab-rail-btn ${activeTab===t.id?"active":""}`}
              onClick={()=>setActiveTab(activeTab===t.id?null:t.id)} title={t.label}>
              <span className="tab-icon">{t.icon}</span>
              <span className="tab-label">{t.label}</span>
            </button>
          ))}
        </div>

        {/* Side Drawer */}
        {activeTab && (
          <div className="side-drawer">

            {/* MODEL */}
            {activeTab==="model" && (
              <div className="drawer-content">
                <div className="ds-title">Pathogen</div>
                <div className="preset-row">
                  {PRESETS.map(p=>(
                    <button key={p.label} className="preset-chip"
                      onClick={()=>setParams(prev=>({...prev,beta:p.beta,sigma:p.sigma,gamma:p.gamma,days:p.days}))}>
                      {p.label}
                    </button>
                  ))}
                </div>
                <div className="ds-title">Transmission</div>
                <Slider label="β (beta)" note={`R₀=${(params.beta/params.gamma).toFixed(1)}`} min={0.05} max={0.8} step={0.01} value={params.beta} onChange={v=>setParam("beta",v)} />
                <Slider label="Latent period" note="1/σ" min={1} max={14} step={1} value={Math.round(1/params.sigma)} onChange={v=>setParam("sigma",1/v)} unit="d" />
                <Slider label="Infectious period" note="1/γ" min={2} max={21} step={1} value={Math.round(1/params.gamma)} onChange={v=>setParam("gamma",1/v)} unit="d" />
                <div className="ds-title">Outbreak Origin</div>
                <select className="drawer-select" value={params.seed_county} onChange={e=>setParam("seed_county",e.target.value)}>
                  {Object.entries(COUNTY_NAMES).sort((a,b)=>a[1].localeCompare(b[1])).map(([fips,name])=>(
                    <option key={fips} value={fips}>{name} County</option>
                  ))}
                </select>
                <Slider label="Seed %" min={0.1} max={5} step={0.1} value={+(params.seed_pct*100).toFixed(1)} onChange={v=>setParam("seed_pct",v/100)} unit="%" />
                <div className="ds-title">Intervention</div>
                <Slider label="Start day" note={params.intervention_day?`day ${params.intervention_day}`:"none"} min={0} max={params.days} step={5} value={params.intervention_day||0} onChange={v=>setParam("intervention_day",v||null)} />
                <Slider label="β reduction" min={0} max={95} step={5} value={Math.round(params.intervention_reduction*100)} onChange={v=>setParam("intervention_reduction",v/100)} unit="%" />
                <div className="ds-title">Simulation</div>
                <Slider label="Days" min={30} max={500} step={10} value={params.days} onChange={v=>setParam("days",v)} />
                <label className="drawer-check"><input type="checkbox" checked={params.use_vulnerability} onChange={e=>setParam("use_vulnerability",e.target.checked)} />Apply vulnerability index</label>
                <div className="ds-title">Healthcare</div>
                <label className="drawer-check"><input type="checkbox" checked={showSurge} onChange={e=>setShowSurge(e.target.checked)} />Show surge risk zones</label>
                <p className="drawer-note">Peak I &gt;10% + hospital &gt;30 km</p>
                <button className="drawer-run-btn" onClick={runSim} disabled={loading}>{loading?"Simulating…":"▶  Run Simulation"}</button>
              </div>
            )}

            {/* COMPARE */}
            {activeTab==="scenario" && (
              <div className="drawer-content">
                <div className="ds-title">Scenario A — Baseline</div>
                <p className="drawer-note">Current model parameters with no intervention applied.</p>
                <div className="ds-title">Scenario B — Intervention</div>
                <Slider label="Intervene day" min={5} max={params.days} step={5} value={compareParams.intervention_day} onChange={v=>setCompareParams(p=>({...p,intervention_day:v}))} />
                <Slider label="β reduction" min={10} max={95} step={5} value={Math.round(compareParams.intervention_reduction*100)} onChange={v=>setCompareParams(p=>({...p,intervention_reduction:v/100}))} unit="%" />
                <button className="drawer-run-btn purple" onClick={runCompare} disabled={compareLoading}>{compareLoading?"Comparing…":"↔  Compare Scenarios"}</button>
                {compareResult && (
                  <div className="compare-summary">
                    <div className="cs-row"><span>Baseline attack</span><strong>{compareResult.scenario_a.meta.attack_rate}%</strong></div>
                    <div className="cs-row"><span>Intervention attack</span><strong>{compareResult.scenario_b.meta.attack_rate}%</strong></div>
                    <div className="cs-row green"><span>Lives protected</span><strong>{fmt(compareResult.scenario_a.meta.total_R-compareResult.scenario_b.meta.total_R)}</strong></div>
                    <button className="cs-view-btn" onClick={()=>setShowCompareModal(true)}>View full comparison →</button>
                  </div>
                )}
              </div>
            )}

            {/* INSIGHTS (Phase 5) */}
            {activeTab==="phase5" && (
              <div className="drawer-content">
                <div className="p5-tabs">
                  {[["roi","ROI"],["cf","Counterfactual"],["res","Resilience"]].map(([id,label])=>(
                    <button key={id} className={`p5-tab ${phase5Tab===id?"active":""}`} onClick={()=>setPhase5Tab(id)}>{label}</button>
                  ))}
                </div>
                {phase5Tab==="roi" && <ROICalculator result={result} compareResult={compareResult} />}
                {phase5Tab==="cf"  && (
                  <CounterfactualTimeline
                    params={params} cfResult={cfResult}
                    setCfResult={setCfResult} setCfLoading={setCfLoading} cfLoading={cfLoading}
                  />
                )}
                {phase5Tab==="res" && <ResiliencePanel resilienceScores={resilienceScores} tracts={tracts} />}
              </div>
            )}

            {/* RESULTS */}
            {activeTab==="results" && (
              <div className="drawer-content">
                {result ? <>
                  <div className="ds-title">Simulation Output</div>
                  <div className="results-grid">
                    {[["Peak infectious",fmt(result.meta.peak_I)],["Peak day",result.meta.peak_day],
                      ["Attack rate",`${result.meta.attack_rate}%`],["Total infected",fmt(result.meta.total_R)],
                      ["R₀",result.meta.R0],["Tracts",result.meta.N_tracts]].map(([l,v])=>(
                      <div key={l} className="result-card"><span>{l}</span><strong>{v}</strong></div>
                    ))}
                  </div>
                  {display && <>
                    <div className="ds-title" style={{marginTop:12}}>Selected Tract</div>
                    <VulnRadar tract={display} stateAvg={stateAvg} />
                  </>}
                </> : <div className="drawer-empty">Run a simulation to see results.</div>}
              </div>
            )}

            {/* EQUITY */}
            {activeTab==="equity" && (
              <div className="drawer-content">
                <div className="ds-title">Equity Analysis</div>
                <p className="drawer-note">Identifies communities facing the highest compounded epidemic burden — attack rate amplified by vulnerability and hospital distance.</p>
                <button className="drawer-run-btn purple" onClick={runEquity} disabled={equityLoading||!result}>{equityLoading?"Analyzing…":"⊕  Run Equity Analysis"}</button>
                {!result && <p className="drawer-note" style={{marginTop:8}}>Run a simulation first.</p>}
                {equityData && (
                  <div className="compare-summary" style={{marginTop:12}}>
                    <div className="cs-row"><span>High-risk tracts</span><strong>{equityData.summary.high_risk_count}</strong></div>
                    <div className="cs-row"><span>Mean attack rate</span><strong>{(equityData.summary.mean_attack*100).toFixed(1)}%</strong></div>
                    <button className="cs-view-btn" onClick={()=>setShowEquityModal(true)}>View full report →</button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Map */}
        <main className="map-main">
          <div ref={mapContainer} className="map" style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
          <MapLegend mapMode={mapMode} />
          <MapPills mapMode={mapMode} setMapMode={setMapMode} result={result}
            playing={playing} setPlaying={setPlaying} playDay={playDay}
            setPlayDay={setPlayDay} days={params.days} compareResult={compareResult} />
          {display && (
            <TractCard tract={display} result={result} resilienceScores={resilienceScores}
              onClose={selectedTract?()=>{setSelectedTract(null);map.current?.setFilter("sel-outline",["==",["get","GEOID"],""]);}:null} />
          )}
          <BottomDrawer result={result} compareResult={compareResult} params={params} open={drawerOpen} onToggle={()=>setDrawerOpen(o=>!o)} />
        </main>
      </div>

      {showCompareModal && <CompareModal compareResult={compareResult} tracts={tracts} onClose={()=>setShowCompareModal(false)} />}
      {showEquityModal  && <EquityModal  equityData={equityData} weaknessData={weaknessData} onClose={()=>setShowEquityModal(false)} />}
    </div>
  );
}
