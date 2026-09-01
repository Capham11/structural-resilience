"""
Phase 3 — FastAPI Backend
Spatial SEIR Model — Washington State

Endpoints:
  GET  /                        Health check
  GET  /tracts                  GeoJSON of all tracts with vulnerability scores
  POST /simulate                Run SEIR simulation with given parameters
  GET  /scenarios               List of precomputed scenarios
  POST /simulate/compare        Run and compare two scenarios side by side
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

BASE_DIR   = Path(__file__).parent.parent / "outbreak_model"
DATA_PATH  = BASE_DIR / "outputs" / "washington_vulnerability_enriched.geojson"

# ==================================================
# APP INIT
# ==================================================

app = FastAPI(
    title="Structural Resilience SEIR API",
    description="Spatial SEIR epidemic model for Washington State census tracts",
    version="1.0.0"
)

# Allow frontend dev server to call the API
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

# Vulnerability
if "vuln_blended" in tracts.columns:
    vuln = tracts["vuln_blended"].fillna(0).to_numpy(dtype=np.float64)
else:
    vuln = np.zeros(N)

# Seed mask
seed_mask = tracts["COUNTYFP"].astype(str) == "033"

# Build sparse weight matrix once
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

# Cache GeoJSON for tract endpoint (geometry + attributes, no time series)
TRACT_GEOJSON = json.loads(
    tracts[[
        "geometry", "GEOID", "COUNTYFP", "population",
        "vuln_blended", "vuln_census", "vuln_percentile",
        "pct_65plus", "pct_poverty", "pct_uninsured",
        "pct_no_broadband", "pct_limited_english", "pct_service_occ"
    ]].to_json()
)

print(f"Ready — {N} tracts loaded")

# ==================================================
# PYDANTIC MODELS
# ==================================================

class SimulationRequest(BaseModel):
    beta:                 float = Field(0.25,  ge=0.01, le=2.0,  description="Transmission rate")
    sigma:                float = Field(1/6,   ge=0.01, le=1.0,  description="1 / latent period")
    gamma:                float = Field(0.1,   ge=0.01, le=1.0,  description="1 / infectious period")
    days:                 int   = Field(200,   ge=10,   le=730,  description="Simulation duration in days")
    dt:                   float = Field(0.5,   ge=0.1,  le=1.0,  description="Timestep")
    seed_pct:             float = Field(0.01,  ge=0.001,le=0.5,  description="Fraction of seed population initially infected")
    seed_county:          str   = Field("033",                    description="COUNTYFP of seed county")
    spillover_rate:       float = Field(0.05,  ge=0.0,  le=1.0,  description="Spatial spillover rate")
    intervention_day:     Optional[int]   = Field(None,           description="Day to apply intervention (None = no intervention)")
    intervention_reduction: float         = Field(0.0,  ge=0.0, le=1.0, description="Fractional beta reduction (0.0–1.0)")
    use_vulnerability:    bool  = Field(True,                     description="Apply vulnerability-adjusted beta")
    sample_every:         int   = Field(5,     ge=1,    le=30,   description="Days between tract-level snapshots")

class CompareRequest(BaseModel):
    baseline:     SimulationRequest
    intervention: SimulationRequest
    label_a:      str = "Baseline"
    label_b:      str = "Intervention"

# ==================================================
# CORE SIMULATION
# ==================================================

def run_seir(
    pop:        np.ndarray,
    vuln:       np.ndarray,
    W_sparse,
    seed_mask:  np.ndarray,
    params:     SimulationRequest,
) -> dict:
    """
    Run spatial SEIR and return summary curves + tract snapshots.
    """
    N = len(pop)

    # Effective beta per tract
    if params.use_vulnerability:
        eff_beta = params.beta * (1.0 + vuln)
    else:
        eff_beta = np.full(N, params.beta)

    # Override spillover rate if different from cached matrix
    # (rebuild only if needed)
    W = W_sparse  # use cached by default

    S = pop.copy()
    E = np.zeros(N)
    I = np.zeros(N)
    R = np.zeros(N)

    # Seed
    county_mask = tracts["COUNTYFP"].astype(str) == params.seed_county
    I[county_mask] = params.seed_pct * pop[county_mask]
    S[county_mask] -= I[county_mask]

    summary    = np.zeros((params.days, 4))
    snapshots  = {}   # day -> {GEOID: I_value}

    for day in range(params.days):

        # Intervention
        if params.intervention_day is not None and day >= params.intervention_day:
            beta_t = eff_beta * (1.0 - params.intervention_reduction)
        else:
            beta_t = eff_beta

        spillover = W @ I
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

        # Snapshot every N days
        if day % params.sample_every == 0:
            snapshots[day] = {
                tracts.iloc[i]["GEOID"]: round(float(I[i]), 2)
                for i in range(N)
            }

    # Tract-level final metrics
    R_final       = R.copy()
    peak_I_tract  = np.zeros(N)
    peak_day_tract = np.zeros(N, dtype=int)

    # Rerun to get per-tract peak (lightweight second pass using summary isn't enough)
    # Instead estimate from snapshots
    for day, snap in snapshots.items():
        for i, geoid in enumerate(tracts["GEOID"]):
            val = snap.get(geoid, 0)
            if val > peak_I_tract[i]:
                peak_I_tract[i]   = val
                peak_day_tract[i] = day

    df = pd.DataFrame(summary, columns=["S", "E", "I", "R"])
    peak_I   = float(df["I"].max())
    peak_day = int(df["I"].idxmax())
    total_R  = float(df["R"].iloc[-1])
    attack   = round(total_R / pop.sum() * 100, 2)
    r0       = round(params.beta / params.gamma, 2)

    return {
        "meta": {
            "peak_I":      peak_I,
            "peak_day":    peak_day,
            "total_R":     total_R,
            "attack_rate": attack,
            "R0":          r0,
            "days":        params.days,
            "N_tracts":    N,
        },
        "curves": {
            "day": list(range(params.days)),
            "S":   df["S"].round(1).tolist(),
            "E":   df["E"].round(1).tolist(),
            "I":   df["I"].round(1).tolist(),
            "R":   df["R"].round(1).tolist(),
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
    return {
        "status": "ok",
        "tracts": N,
        "model":  "Spatial SEIR — Washington State"
    }


@app.get("/tracts")
def get_tracts():
    """Return GeoJSON of all tracts with vulnerability attributes."""
    return TRACT_GEOJSON


@app.post("/simulate")
def simulate(req: SimulationRequest):
    """
    Run a single SEIR simulation and return curves + tract-level metrics.

    Example body:
    {
        "beta": 0.25,
        "days": 200,
        "intervention_day": 60,
        "intervention_reduction": 0.4
    }
    """
    try:
        result = run_seir(pop, vuln, W_sparse, seed_mask, req)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/simulate/compare")
def compare(req: CompareRequest):
    """
    Run two scenarios and return both for side-by-side comparison.

    Example body:
    {
        "baseline":     { "beta": 0.25, "days": 200 },
        "intervention": { "beta": 0.25, "days": 200, "intervention_day": 60, "intervention_reduction": 0.4 },
        "label_a": "No intervention",
        "label_b": "40% reduction day 60"
    }
    """
    try:
        result_a = run_seir(pop, vuln, W_sparse, seed_mask, req.baseline)
        result_b = run_seir(pop, vuln, W_sparse, seed_mask, req.intervention)

        # Compute tract-level delta (intervention benefit)
        delta = {}
        for geoid in result_a["tract_metrics"]:
            ar_a = result_a["tract_metrics"][geoid]["attack_rate"]
            ar_b = result_b["tract_metrics"].get(geoid, {}).get("attack_rate", ar_a)
            delta[geoid] = round(ar_a - ar_b, 4)

        return {
            "label_a":   req.label_a,
            "label_b":   req.label_b,
            "scenario_a": result_a,
            "scenario_b": result_b,
            "delta":      delta,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scenarios")
def list_scenarios():
    """Return preset scenario configurations."""
    return {
        "scenarios": [
            {
                "id":          "baseline",
                "label":       "Baseline — No intervention",
                "description": "Unmitigated epidemic spread from King County",
                "params":      {"beta": 0.25, "days": 200}
            },
            {
                "id":          "early_intervention",
                "label":       "Early intervention — Day 60, 40% reduction",
                "description": "Moderate NPIs applied early",
                "params":      {"beta": 0.25, "days": 200, "intervention_day": 60, "intervention_reduction": 0.4}
            },
            {
                "id":          "late_intervention",
                "label":       "Late intervention — Day 150, 40% reduction",
                "description": "NPIs applied after first wave peak",
                "params":      {"beta": 0.25, "days": 200, "intervention_day": 150, "intervention_reduction": 0.4}
            },
            {
                "id":          "strong_intervention",
                "label":       "Strong early intervention — Day 30, 70% reduction",
                "description": "Aggressive suppression strategy",
                "params":      {"beta": 0.25, "days": 200, "intervention_day": 30, "intervention_reduction": 0.7}
            },
        ]
    }