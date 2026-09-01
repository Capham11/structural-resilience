"""
Phase 3 — FastAPI Backend
Spatial SEIR Model — Washington State
"""

from pathlib import Path
from typing import Optional
import json

import numpy as np
import pandas as pd
import geopandas as gpd
import scipy.sparse as sp

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from libpysal.weights import Queen

# ==================================================
# PATHS
# ==================================================

BASE_DIR  = Path(__file__).parent
DATA_PATH = BASE_DIR / "washington_vulnerability_enriched.geojson"

# ==================================================
# APP INIT
# ==================================================

app = FastAPI(
    title="Structural Resilience SEIR API",
    description="Spatial SEIR epidemic model for Washington State census tracts",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================================================
# LOAD + CACHE DATA AT STARTUP
# ==================================================

print("Loading spatial data...")

tracts = gpd.read_file(DATA_PATH)
tracts["population"] = pd.to_numeric(tracts["population"], errors="coerce")
tracts = tracts.dropna(subset=["population"])
tracts = tracts[tracts["population"] > 0].copy()
tracts = tracts.reset_index(drop=True)

N   = len(tracts)
pop = tracts["population"].to_numpy(dtype=np.float64)

if "vuln_blended" in tracts.columns:
    vuln = tracts["vuln_blended"].fillna(0).to_numpy(dtype=np.float64)
else:
    vuln = np.zeros(N)

seed_mask = tracts["COUNTYFP"].astype(str) == "033"

print("Building spatial graph...")
w = Queen.from_dataframe(tracts, use_index=False)

SPILLOVER_RATE = 0.05
rows, cols, data = [], [], []
for i, neighbors in w.neighbors.items():
    for j in neighbors:
        rows.append(i)
        cols.append(j)
        data.append(SPILLOVER_RATE)

W_sparse = sp.csr_matrix((data, (rows, cols)), shape=(N, N))

serve_cols = [
    "geometry", "GEOID", "COUNTYFP", "population",
    "vuln_blended", "vuln_census", "vuln_percentile",
    "pct_65plus", "pct_poverty", "pct_uninsured",
    "pct_no_broadband", "pct_limited_english", "pct_service_occ",
    "HubDist", "HubName", "health_norm", "hub_dist_norm", "surge_risk"
]
serve_cols = [c for c in serve_cols if c in tracts.columns]
TRACT_GEOJSON = json.loads(tracts[serve_cols].to_json())

print(f"Ready — {N} tracts loaded")

# ==================================================
# PYDANTIC MODELS
# ==================================================

class SimulationRequest(BaseModel):
    beta:                   float        = Field(0.225,  ge=0.01, le=2.0)
    sigma:                  float        = Field(1/6,   ge=0.01, le=1.0)
    gamma:                  float        = Field(0.1,   ge=0.01, le=1.0)
    days:                   int          = Field(200,   ge=10,   le=730)
    dt:                     float        = Field(0.5,   ge=0.1,  le=1.0)
    seed_pct:               float        = Field(0.01,  ge=0.001,le=0.5)
    seed_county:            str          = Field("033")
    spillover_rate:         float        = Field(0.05,  ge=0.0,  le=1.0)
    intervention_day:       Optional[int]= Field(None)
    intervention_reduction: float        = Field(0.0,   ge=0.0,  le=1.0)
    use_vulnerability:      bool         = Field(True)
    sample_every:           int          = Field(5,     ge=1,    le=30)

class CompareRequest(BaseModel):
    baseline:     SimulationRequest
    intervention: SimulationRequest
    label_a:      str = "Baseline"
    label_b:      str = "Intervention"

# ==================================================
# CORE SIMULATION
# ==================================================

def run_seir(pop, vuln, W_sparse, seed_mask, params):
    N = len(pop)

    eff_beta = params.beta * (1.0 + vuln) if params.use_vulnerability else np.full(N, params.beta)

    S = pop.copy()
    E = np.zeros(N)
    I = np.zeros(N)
    R = np.zeros(N)

    county_mask = tracts["COUNTYFP"].astype(str) == params.seed_county
    I[county_mask] = params.seed_pct * pop[county_mask]
    S[county_mask] -= I[county_mask]

    summary   = np.zeros((params.days, 4))
    snapshots = {}

    for day in range(params.days):
        if params.intervention_day is not None and day >= params.intervention_day:
            beta_t = eff_beta * (1.0 - params.intervention_reduction)
        else:
            beta_t = eff_beta

        spillover = W_sparse @ I
        force     = I + spillover

        new_exposed   = params.dt * beta_t * S * force / pop
        new_infected  = params.dt * params.sigma * E
        new_recovered = params.dt * params.gamma * I

        S += -new_exposed
        E +=  new_exposed  - new_infected
        I +=  new_infected - new_recovered
        R +=  new_recovered

        np.clip(S, 0, None, out=S)
        np.clip(E, 0, None, out=E)
        np.clip(I, 0, None, out=I)
        np.clip(R, 0, None, out=R)

        total = S + E + I + R
        scale = np.where(total > 0, pop / total, 1.0)
        S *= scale; E *= scale; I *= scale; R *= scale

        summary[day] = [S.sum(), E.sum(), I.sum(), R.sum()]

        if day % params.sample_every == 0:
            snapshots[day] = {
                tracts.iloc[i]["GEOID"]: round(float(I[i]), 2)
                for i in range(N)
            }

    R_final        = R.copy()
    peak_I_tract   = np.zeros(N)
    peak_day_tract = np.zeros(N, dtype=int)

    for day, snap in snapshots.items():
        for i, geoid in enumerate(tracts["GEOID"]):
            val = snap.get(geoid, 0)
            if val > peak_I_tract[i]:
                peak_I_tract[i]   = val
                peak_day_tract[i] = day

    df       = pd.DataFrame(summary, columns=["S", "E", "I", "R"])
    peak_I   = float(df["I"].max())
    peak_day = int(df["I"].idxmax())
    total_R  = float(df["R"].iloc[-1])
    attack   = round(total_R / pop.sum() * 100, 2)
    r0       = round(params.beta / params.gamma, 2)

    return {
        "meta": {
            "peak_I": peak_I, "peak_day": peak_day, "total_R": total_R,
            "attack_rate": attack, "R0": r0, "days": params.days, "N_tracts": N,
        },
        "curves": {
            "day": list(range(params.days)),
            "S": df["S"].round(1).tolist(),
            "E": df["E"].round(1).tolist(),
            "I": df["I"].round(1).tolist(),
            "R": df["R"].round(1).tolist(),
        },
        "tract_metrics": {
            tracts.iloc[i]["GEOID"]: {
                "attack_rate": round(float(R_final[i] / pop[i]), 4),
                "peak_I":      round(float(peak_I_tract[i]), 2),
                "peak_day":    int(peak_day_tract[i]),
            }
            for i in range(N)
        },
        "snapshots": snapshots,
    }

# ==================================================
# ENDPOINTS
# ==================================================

@app.get("/")
def health():
    return {"status": "ok", "tracts": N, "model": "Spatial SEIR — Washington State"}


@app.get("/tracts")
def get_tracts():
    return TRACT_GEOJSON


@app.post("/simulate")
def simulate(req: SimulationRequest):
    try:
        return run_seir(pop, vuln, W_sparse, seed_mask, req)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/simulate/compare")
def compare(req: CompareRequest):
    try:
        result_a = run_seir(pop, vuln, W_sparse, seed_mask, req.baseline)
        result_b = run_seir(pop, vuln, W_sparse, seed_mask, req.intervention)

        delta = {}
        for geoid in result_a["tract_metrics"]:
            ar_a = result_a["tract_metrics"][geoid]["attack_rate"]
            ar_b = result_b["tract_metrics"].get(geoid, {}).get("attack_rate", ar_a)
            delta[geoid] = round(ar_a - ar_b, 4)

        return {
            "label_a": req.label_a, "label_b": req.label_b,
            "scenario_a": result_a, "scenario_b": result_b,
            "delta": delta,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scenarios")
def list_scenarios():
    return {
        "scenarios": [
            {
                "id": "baseline", "label": "Baseline — No intervention",
                "description": "Unmitigated epidemic spread from King County",
                "params": {"beta": 0.25, "days": 200}
            },
            {
                "id": "early_intervention", "label": "Early intervention — Day 60, 40% reduction",
                "description": "Moderate NPIs applied early",
                "params": {"beta": 0.25, "days": 200, "intervention_day": 60, "intervention_reduction": 0.4}
            },
            {
                "id": "late_intervention", "label": "Late intervention — Day 150, 40% reduction",
                "description": "NPIs applied after first wave peak",
                "params": {"beta": 0.25, "days": 200, "intervention_day": 150, "intervention_reduction": 0.4}
            },
            {
                "id": "strong_intervention", "label": "Strong early intervention — Day 30, 70% reduction",
                "description": "Aggressive suppression strategy",
                "params": {"beta": 0.25, "days": 200, "intervention_day": 30, "intervention_reduction": 0.7}
            },
        ]
    }


@app.get("/equity/structural-weakness")
def structural_weakness():
    """
    Return tracts that are simultaneously high vulnerability,
    far from hospital, and high surge risk. Ranked by composite score.
    """
    try:
        cols_needed = [
            "GEOID", "COUNTYFP", "population", "vuln_blended",
            "surge_risk", "hub_dist_norm", "pct_poverty", "pct_uninsured",
            "pct_65plus", "pct_no_broadband", "pct_limited_english", "pct_service_occ"
        ]
        cols_needed = [c for c in cols_needed if c in tracts.columns]
        df = tracts[cols_needed].copy()

        vuln_col     = df["vuln_blended"] if "vuln_blended" in df.columns else pd.Series(0, index=df.index)
        hub_col      = df["hub_dist_norm"] if "hub_dist_norm" in df.columns else pd.Series(0, index=df.index)
        hubdist_col  = tracts["HubDist"] if "HubDist" in tracts.columns else pd.Series(0, index=tracts.index)

        mask = (vuln_col > 0.3) & (hubdist_col > 30000)
        weak = df[mask].copy()

        weak["structural_score"] = (
            0.4 * (weak["vuln_blended"] if "vuln_blended" in weak.columns else 0) +
            0.4 * (weak["hub_dist_norm"] if "hub_dist_norm" in weak.columns else 0) +
            0.2 * (weak["surge_risk"]    if "surge_risk"    in weak.columns else 0)
        )
        weak = weak.sort_values("structural_score", ascending=False).head(30)

        return weak.fillna(0).to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/equity/impact")
def equity_impact(req: SimulationRequest):
    """
    Run simulation and return equity-weighted impact metrics.
    Ranks tracts by attack_rate * vuln_blended * hospital distance penalty.
    """
    try:
        result = run_seir(pop, vuln, W_sparse, seed_mask, req)

        equity_rows = []
        for i in range(N):
            geoid    = tracts.iloc[i]["GEOID"]
            metrics  = result["tract_metrics"].get(geoid, {})
            attack   = metrics.get("attack_rate", 0)
            vuln_s   = float(tracts.iloc[i]["vuln_blended"]) if "vuln_blended" in tracts.columns else 0
            hub_dist = float(tracts.iloc[i]["HubDist"])      if "HubDist"      in tracts.columns else 0

            equity_burden = attack * vuln_s * (1 + hub_dist / 100000)

            equity_rows.append({
                "GEOID":               geoid,
                "COUNTYFP":            str(tracts.iloc[i]["COUNTYFP"]),
                "population":          int(pop[i]),
                "attack_rate":         round(attack, 4),
                "vuln_blended":        round(vuln_s, 4),
                "hub_dist_km":         round(hub_dist / 1000, 1),
                "equity_burden":       round(equity_burden, 6),
                "peak_day":            metrics.get("peak_day", 0),
                "pct_poverty":         round(float(tracts.iloc[i].get("pct_poverty",         0) or 0), 4),
                "pct_uninsured":       round(float(tracts.iloc[i].get("pct_uninsured",       0) or 0), 4),
                "pct_65plus":          round(float(tracts.iloc[i].get("pct_65plus",          0) or 0), 4),
                "pct_no_broadband":    round(float(tracts.iloc[i].get("pct_no_broadband",    0) or 0), 4),
                "pct_limited_english": round(float(tracts.iloc[i].get("pct_limited_english", 0) or 0), 4),
                "pct_service_occ":     round(float(tracts.iloc[i].get("pct_service_occ",     0) or 0), 4),
            })

        equity_rows.sort(key=lambda x: x["equity_burden"], reverse=True)

        return {
            "top_burdened": equity_rows[:20],
            "all_tracts":   equity_rows,
            "summary": {
                "mean_attack":     round(sum(r["attack_rate"]   for r in equity_rows) / len(equity_rows), 4),
                "mean_burden":     round(sum(r["equity_burden"] for r in equity_rows) / len(equity_rows), 6),
                "high_risk_count": sum(1 for r in equity_rows if r["equity_burden"] > 0.05),
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/simulate/counterfactual")
def counterfactual(req: SimulationRequest):
    """
    Run baseline + intervention in one call for counterfactual timeline.
    Returns both curves and per-day lives-saved delta.
    """
    try:
        from copy import deepcopy
        from pydantic import BaseModel

        # Baseline — same params, no intervention
        base_req = deepcopy(req)
        base_req.intervention_day = None
        base_req.intervention_reduction = 0.0

        result_base = run_seir(pop, vuln, W_sparse, seed_mask, base_req)
        result_intv = run_seir(pop, vuln, W_sparse, seed_mask, req)

        # Per-day cumulative lives saved
        lives_saved = [
            round(result_base["curves"]["R"][i] - result_intv["curves"]["R"][i], 1)
            for i in range(req.days)
        ]

        return {
            "baseline":    result_base,
            "intervention": result_intv,
            "lives_saved": lives_saved,
            "summary": {
                "total_lives_saved": round(result_base["meta"]["total_R"] - result_intv["meta"]["total_R"], 0),
                "peak_reduction":    round(result_base["meta"]["peak_I"]  - result_intv["meta"]["peak_I"],  0),
                "peak_delay_days":   result_intv["meta"]["peak_day"] - result_base["meta"]["peak_day"],
                "attack_rate_base":  result_base["meta"]["attack_rate"],
                "attack_rate_intv":  result_intv["meta"]["attack_rate"],
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))