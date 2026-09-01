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
DATA_PATH = BASE_DIR / "data" / "washington_base_structural_resilience.geojson"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# ==================================================
# LOAD + CLEAN DATA
# ==================================================

print("\nLoading data...")

tracts = gpd.read_file(DATA_PATH)
print(f"Loaded tracts: {len(tracts)}")

tracts["population"] = pd.to_numeric(
    tracts["census_vulnerability_population"], errors="coerce"
)
tracts = tracts.dropna(subset=["population"])
tracts = tracts[tracts["population"] > 0].copy()
tracts = tracts.rename(columns={"final_index": "vuln"})
tracts = tracts.reset_index(drop=True)

print("COUNTYFP sample:", tracts["COUNTYFP"].head(10).tolist())
print("Unique COUNTYFP count:", tracts["COUNTYFP"].nunique())
print("Seed tract count:", (tracts["COUNTYFP"].astype(str) == "033").sum())

N = len(tracts)

# ==================================================
# SPATIAL NETWORK — build ONCE as sparse matrix
# ==================================================

print("\nBuilding spatial graph...")

w = Queen.from_dataframe(tracts)
print(f"Neighbor graph size: {len(w.neighbors)}")

# Convert PySAL weights to a row-normalized sparse matrix (CSR for fast matvec)
# Shape: (N, N), entry [i,j] = spillover_rate if j is neighbor of i
spillover_rate = 0.001

rows, cols, data = [], [], []
for i, neighbors in w.neighbors.items():
    for j in neighbors:
        rows.append(i)
        cols.append(j)
        data.append(spillover_rate)

W_sparse = sp.csr_matrix((data, (rows, cols)), shape=(N, N))

# ==================================================
# PARAMETERS
# ==================================================

beta          = 0.4
sigma         = 1 / 6
gamma         = 1 / 10
dt            = 0.2
days          = 500

# ==================================================
# STATE ARRAYS (numpy — fast in-place ops)
# ==================================================

pop = tracts["population"].to_numpy(dtype=np.float64)

S = pop.copy()
E = np.zeros(N)
I = np.zeros(N)
R = np.zeros(N)

# Vulnerability-adjusted beta — computed ONCE
if "vuln" in tracts.columns:
    vuln = tracts["vuln"].fillna(0).to_numpy(dtype=np.float64)
    eff_beta = beta * (1.0 + vuln)          # shape (N,)
else:
    eff_beta = np.full(N, beta)

print("vuln sample:", vuln[:5])
print("vuln min/max:", vuln.min(), vuln.max())
print("vuln NaN count:", np.isnan(vuln).sum())

# Seed outbreak (King County FIPS 033)
seed_mask = tracts["COUNTYFP"].astype(str) == "033"
I[seed_mask] = 5.0
S[seed_mask] -= 5.0
print(f"\nSeeded {seed_mask.sum()} tracts")

# ==================================================
# SIMULATION LOOP
# ==================================================

print("\nRunning simulation...")

history = np.empty((days, 4))   # columns: S, E, I, R

for day in range(days):

    # Vectorized spillover: W_sparse @ I  gives sum of I in neighbors for each tract
    spillover = W_sparse @ I                        # shape (N,)

    force = I + spillover                           # effective infectious pressure

    # SEIR transitions
    new_exposed   = dt * eff_beta * S * force / pop
    new_infected  = dt * sigma * E
    new_recovered = dt * gamma * I

    S += -new_exposed
    E +=  new_exposed  - new_infected
    I +=  new_infected - new_recovered
    R +=  new_recovered

    # Clamp negatives (numerical safety)
    np.clip(S, 0, None, out=S)
    np.clip(E, 0, None, out=E)
    np.clip(I, 0, None, out=I)
    np.clip(R, 0, None, out=R)

    # Rescale to conserve population exactly
    total = S + E + I + R
    scale = np.where(total > 0, pop / total, 1.0)
    S *= scale
    E *= scale
    I *= scale
    R *= scale

    history[day] = [S.sum(), E.sum(), I.sum(), R.sum()]

# ==================================================
# RESULTS
# ==================================================

df_history = pd.DataFrame(history, columns=["S", "E", "I", "R"])
df_history.index.name = "day"

print(f"\nPeak infections: {df_history['I'].max():,.1f}")
peak_day = df_history['I'].idxmax()
print(f"Peak day: {peak_day}")

# Write final tract-level state back for export if needed
tracts["S_final"] = S
tracts["E_final"] = E
tracts["I_final"] = I
tracts["R_final"] = R
tracts["attack_rate"] = R / pop   # fraction ever infected

# ==================================================
# PLOT — SEIR CURVES
# ==================================================

fig, ax = plt.subplots(figsize=(10, 6))

for col, color in zip(["S", "E", "I", "R"], ["steelblue", "orange", "crimson", "forestgreen"]):
    ax.plot(df_history.index, df_history[col], label=col, color=color)

ax.axvline(peak_day, color="crimson", linestyle="--", alpha=0.4, label=f"Peak day {peak_day}")
ax.set_title("Spatial SEIR Model – Washington State (Census Tracts)")
ax.set_xlabel("Day")
ax.set_ylabel("Population")
ax.legend()
ax.grid(True, alpha=0.3)
plt.tight_layout()

out_file = OUTPUT_DIR / "seir_curve.png"
fig.savefig(out_file, dpi=300)
print(f"\nSaved: {out_file}")
plt.show()

# ==================================================
# OPTIONAL — CHOROPLETH OF ATTACK RATE
# ==================================================

fig2, ax2 = plt.subplots(figsize=(12, 10))
tracts.plot(
    column="attack_rate",
    cmap="YlOrRd",
    legend=True,
    legend_kwds={"label": "Attack rate (fraction infected)", "shrink": 0.6},
    ax=ax2,
    edgecolor="none",
)
ax2.set_title("Final Attack Rate by Census Tract – Washington State")
ax2.axis("off")
plt.tight_layout()

map_file = OUTPUT_DIR / "seir_attack_rate_map.png"
fig2.savefig(map_file, dpi=300)
print(f"Saved: {map_file}")
plt.show()