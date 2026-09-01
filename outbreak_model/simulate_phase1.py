"""
Spatial SEIR Model — Phase 1
Washington State Census Tracts

Features:
- Callable simulation function with intervention hooks
- Baseline vs intervention scenario comparison
- Tract-level I_history export for map animation
- Attack rate and peak metrics per tract
- GeoJSON time series export for Kepler.gl / future web frontend
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scipy.sparse as sp
from libpysal.weights import Queen

# ==================================================
# PATHS
# ==================================================

BASE_DIR = Path(__file__).parent
DATA_PATH = BASE_DIR / 'outputs' / "washington_vulnerability_enriched.geojson"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# ==================================================
# LOAD + CLEAN
# ==================================================

print("\nLoading data...")
tracts = gpd.read_file(DATA_PATH)
print(f"Loaded tracts: {len(tracts)}")

# Use pop_total from enriched GeoJSON (same data, different column name)
tracts["population"] = pd.to_numeric(tracts["population"], errors="coerce")
tracts = tracts.dropna(subset=["population"])
tracts = tracts[tracts["population"] > 0].copy()
tracts = tracts.rename(columns={'vuln_blended': 'vuln'})
tracts = tracts.reset_index(drop=True)

N = len(tracts)
pop = tracts["population"].to_numpy(dtype=np.float64)

# ==================================================
# SPATIAL WEIGHTS — sparse matrix (built once)
# ==================================================

print("\nBuilding spatial graph...")
w = Queen.from_dataframe(tracts, use_index=False)
print(f"Neighbor graph size: {len(w.neighbors)}")

SPILLOVER_RATE = 0.05

rows, cols, data = [], [], []
for i, neighbors in w.neighbors.items():
    for j in neighbors:
        rows.append(i)
        cols.append(j)
        data.append(SPILLOVER_RATE)

W_sparse = sp.csr_matrix((data, (rows, cols)), shape=(N, N))

# ==================================================
# VULNERABILITY-ADJUSTED BETA (computed once)
# ==================================================

BETA  = 0.25
SIGMA = 1 / 6
GAMMA = 1 / 10
DT    = 0.5
DAYS  = 200

if "vuln" in tracts.columns:
    vuln = tracts["vuln"].fillna(0).to_numpy(dtype=np.float64)
    eff_beta = BETA * (1.0 + vuln)
    print(f"Vuln range: {vuln.min():.3f} – {vuln.max():.3f}")
else:
    eff_beta = np.full(N, BETA)
    print("No vuln column found — using flat beta")

print(f"eff_beta range: {eff_beta.min():.3f} – {eff_beta.max():.3f}")

# Seed mask
seed_mask = tracts["COUNTYFP"].astype(str) == "033"
print(f"Seed tracts (King County): {seed_mask.sum()}")
print(f"Seeded population: {(0.01 * pop[seed_mask]).sum():,.0f}")

# ==================================================
# CORE SIMULATION FUNCTION
# ==================================================

def run_seir(
    pop,
    eff_beta,
    W_sparse,
    sigma,
    gamma,
    dt,
    days,
    seed_mask,
    intervention_day=None,
    intervention_reduction=0.0,
    label="run"
):
    """
    Run spatial SEIR simulation.

    Parameters
    ----------
    pop                  : (N,) array of tract populations
    eff_beta             : (N,) array of vulnerability-adjusted beta values
    W_sparse             : (N, N) sparse spillover weight matrix
    sigma                : 1 / latent period
    gamma                : 1 / infectious period
    dt                   : discrete timestep
    days                 : number of simulation days
    seed_mask            : boolean array marking seed tracts
    intervention_day     : day to apply beta reduction (None = no intervention)
    intervention_reduction: fractional reduction in beta (0.0–1.0)
    label                : string label for print output

    Returns
    -------
    summary   : (days, 4) array — daily [S, E, I, R] totals
    I_history : (days, N) array — daily infectious count per tract
    R_final   : (N,) array — final recovered count per tract
    """

    N = len(pop)

    S = pop.copy()
    E = np.zeros(N)
    I = np.zeros(N)
    R = np.zeros(N)

    # Seed 1% of King County population
    I[seed_mask] = 0.01 * pop[seed_mask]
    S[seed_mask] -= I[seed_mask]

    I_history = np.zeros((days, N))
    summary   = np.zeros((days, 4))

    for day in range(days):

        # Apply intervention at specified day
        if intervention_day is not None and day >= intervention_day:
            beta_t = eff_beta * (1.0 - intervention_reduction)
        else:
            beta_t = eff_beta

        # Vectorized spillover
        spillover = W_sparse @ I
        force     = I + spillover

        # SEIR transitions
        new_exposed   = dt * beta_t * S * force / pop
        new_infected  = dt * sigma  * E
        new_recovered = dt * gamma  * I

        S += -new_exposed
        E +=  new_exposed  - new_infected
        I +=  new_infected - new_recovered
        R +=  new_recovered

        # Clamp negatives
        np.clip(S, 0, None, out=S)
        np.clip(E, 0, None, out=E)
        np.clip(I, 0, None, out=I)
        np.clip(R, 0, None, out=R)

        # Conserve population
        total = S + E + I + R
        scale = np.where(total > 0, pop / total, 1.0)
        S *= scale; E *= scale; I *= scale; R *= scale

        I_history[day] = I.copy()
        summary[day]   = [S.sum(), E.sum(), I.sum(), R.sum()]

    df = pd.DataFrame(summary, columns=["S", "E", "I", "R"])
    peak_I   = df["I"].max()
    peak_day = df["I"].idxmax()
    total_R  = df["R"].iloc[-1]
    attack   = total_R / pop.sum() * 100

    print(f"\n[{label}]")
    print(f"  Peak infections : {peak_I:>12,.0f}  (day {peak_day})")
    print(f"  Total recovered : {total_R:>12,.0f}")
    print(f"  Attack rate     : {attack:>11.1f}%")

    return summary, I_history, R[-1].copy()


# ==================================================
# RUN SCENARIOS
# ==================================================

print("\nRunning baseline...")
summary_base, I_hist_base, R_final_base = run_seir(
    pop, eff_beta, W_sparse, SIGMA, GAMMA, DT, DAYS, seed_mask,
    label="Baseline"
)

print("\nRunning early intervention (day 60, 40% reduction)...")
summary_e40, I_hist_e40, R_final_e40 = run_seir(
    pop, eff_beta, W_sparse, SIGMA, GAMMA, DT, DAYS, seed_mask,
    intervention_day=60,
    intervention_reduction=0.4,
    label="Early intervention — 40% @ day 60"
)

print("\nRunning late intervention (day 150, 40% reduction)...")
summary_l40, I_hist_l40, R_final_l40 = run_seir(
    pop, eff_beta, W_sparse, SIGMA, GAMMA, DT, DAYS, seed_mask,
    intervention_day=150,
    intervention_reduction=0.4,
    label="Late intervention — 40% @ day 150"
)

df_base = pd.DataFrame(summary_base, columns=["S", "E", "I", "R"])
df_e40  = pd.DataFrame(summary_e40,  columns=["S", "E", "I", "R"])
df_l40  = pd.DataFrame(summary_l40,  columns=["S", "E", "I", "R"])

# ==================================================
# PLOT 1 — SEIR CURVES (BASELINE)
# ==================================================

fig, ax = plt.subplots(figsize=(12, 6))

colors = {"S": "steelblue", "E": "orange", "I": "crimson", "R": "forestgreen"}
for col, color in colors.items():
    ax.plot(df_base[col], label=col, color=color)

peak_day = df_base["I"].idxmax()
ax.axvline(peak_day, color="crimson", linestyle="--", alpha=0.4,
           label=f"Peak day {peak_day}")

ax.set_title("Spatial SEIR Model — Baseline (Washington State)")
ax.set_xlabel("Day")
ax.set_ylabel("Population")
ax.legend()
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "seir_baseline.png", dpi=300)
print("\nSaved: seir_baseline.png")

# ==================================================
# PLOT 2 — INTERVENTION COMPARISON
# ==================================================

fig2, axes = plt.subplots(1, 2, figsize=(16, 6))

# Left — Infectious curves
ax = axes[0]
ax.plot(df_base["I"], color="crimson",      linewidth=2, label="Baseline")
ax.plot(df_e40["I"],  color="steelblue",    linewidth=2, label="Intervene day 60 (−40%)")
ax.plot(df_l40["I"],  color="darkorange",   linewidth=2, label="Intervene day 150 (−40%)")
ax.axvline(60,  color="steelblue",  linestyle=":", alpha=0.6)
ax.axvline(150, color="darkorange", linestyle=":", alpha=0.6)
ax.set_title("Infectious (I) — Scenario Comparison")
ax.set_xlabel("Day")
ax.set_ylabel("Population")
ax.legend()
ax.grid(True, alpha=0.3)

# Right — Cumulative recovered (total burden)
ax = axes[1]
ax.plot(df_base["R"], color="crimson",    linewidth=2, label="Baseline")
ax.plot(df_e40["R"],  color="steelblue",  linewidth=2, label="Intervene day 60 (−40%)")
ax.plot(df_l40["R"],  color="darkorange", linewidth=2, label="Intervene day 150 (−40%)")
ax.set_title("Cumulative Recovered (R) — Total Burden")
ax.set_xlabel("Day")
ax.set_ylabel("Population")
ax.legend()
ax.grid(True, alpha=0.3)

plt.suptitle("Intervention Impact — Washington State SEIR", fontsize=14, y=1.02)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "seir_intervention_comparison.png", dpi=300, bbox_inches="tight")
print("Saved: seir_intervention_comparison.png")

# ==================================================
# COMPUTE TRACT-LEVEL METRICS (before any plotting)
# ==================================================

tracts["peak_I_rate"]  = np.clip(I_hist_base.max(axis=0) / pop, 0, 1)
tracts["attack_base"]  = np.clip(R_final_base / pop, 0, 1)
tracts["attack_e40"]   = np.clip(R_final_e40  / pop, 0, 1)
tracts["attack_l40"]   = np.clip(R_final_l40  / pop, 0, 1)
tracts["attack_delta"] = tracts["attack_base"] - tracts["attack_e40"]
tracts["peak_day_base"] = np.argmax(I_hist_base, axis=0)
tracts["peak_I_base"]   = I_hist_base.max(axis=0)

# ==================================================
# PLOT 3 — PEAK INFECTIOUS RATE CHOROPLETH
# ==================================================

fig3, ax3 = plt.subplots(figsize=(14, 10))
tracts.plot(
    column="peak_I_rate",
    cmap="YlOrRd",
    legend=True,
    vmin=0,
    vmax=0.2,               # cap at 20% so spatial variation is visible
    legend_kwds={"label": "Peak infectious rate (fraction of tract population)",
                 "shrink": 0.6},
    ax=ax3,
    edgecolor="none",
    missing_kwds={"color": "lightgrey"}
)
ax3.set_title("Peak Infectious Rate by Census Tract — Baseline", fontsize=14)
ax3.axis("off")
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "seir_attack_rate_map.png", dpi=300)
print("Saved: seir_attack_rate_map.png")

# ==================================================
# EXPORT — TRACT-LEVEL TIME SERIES (GEOJSON)
# ==================================================

print("\nExporting time series GeoJSON...")

sample_days = list(range(0, DAYS, 5))

for d in sample_days:
    tracts[f"I_base_d{d:03d}"] = I_hist_base[d]

for d in sample_days:
    tracts[f"I_e40_d{d:03d}"] = I_hist_e40[d]

export_cols = (
    ["geometry", "GEOID", "COUNTYFP", "population",
     "vuln", "peak_I_rate", "attack_base", "attack_e40",
     "attack_l40", "attack_delta", "peak_day_base", "peak_I_base"]
    + [f"I_base_d{d:03d}" for d in sample_days]
    + [f"I_e40_d{d:03d}"  for d in sample_days]
)

export_cols = [c for c in export_cols if c in tracts.columns]

tracts[export_cols].to_file(
    OUTPUT_DIR / "seir_timeseries.geojson", driver="GeoJSON"
)
print("Saved: seir_timeseries.geojson")
# ==================================================
# SUMMARY TABLE
# ==================================================

print("\n" + "="*60)
print("SCENARIO SUMMARY")
print("="*60)

scenarios = {
    "Baseline":             (df_base, R_final_base),
    "Intervene day 60":     (df_e40,  R_final_e40),
    "Intervene day 150":    (df_l40,  R_final_l40),
}

for name, (df, R_f) in scenarios.items():
    peak_I   = df["I"].max()
    peak_day = df["I"].idxmax()
    total_R  = df["R"].iloc[-1]
    attack   = total_R / pop.sum() * 100
    print(f"\n{name}")
    print(f"  Peak I   : {peak_I:>10,.0f}  (day {peak_day})")
    print(f"  Total R  : {total_R:>10,.0f}")
    print(f"  Attack % : {attack:>9.1f}%")

print("\nDone.")
plt.show()