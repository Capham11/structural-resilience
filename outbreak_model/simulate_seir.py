from pathlib import Path

import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
from libpysal.weights import Queen

# ==================================================
# PATHS
# ==================================================

BASE_DIR = Path(__file__).parent
DATA_PATH = BASE_DIR / "data" / "washington_base_structural_resilience.geojson"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# ==================================================
# LOAD DATA
# ==================================================

print("\nLoading GIS data...")

tracts = gpd.read_file(DATA_PATH)

print(f"Loaded tracts: {len(tracts)}")

# ==================================================
# CLEAN / STANDARDIZE
# ==================================================

# Ensure numeric
tracts["census_vulnerability_population"] = pd.to_numeric(
    tracts["census_vulnerability_population"],
    errors="coerce"
)

tracts = tracts.dropna(subset=["census_vulnerability_population"]).copy()

# Rename for simplicity
tracts = tracts.rename(columns={
    "census_vulnerability_population": "population"
})

# ==================================================
# SEIR INITIALIZATION
# ==================================================

tracts["S"] = tracts["population"].astype(float)
tracts["E"] = 0.0
tracts["I"] = 0.0
tracts["R"] = 0.0

# Seed outbreak in King County
seed = tracts["COUNTYFP"].astype(str) == "033"

tracts.loc[seed, "I"] = 10
tracts.loc[seed, "S"] -= 10

print(f"\nSeeded outbreak in {seed.sum()} tracts")

# ==================================================
# SPATIAL NETWORK (CRITICAL STEP)
# ==================================================

print("\nBuilding spatial neighbors...")

w = Queen.from_dataframe(tracts)

print(f"Neighbor graph built for {len(w.neighbors)} tracts")

# ==================================================
# PARAMETERS
# ==================================================

beta = 0.30
sigma = 1 / 5
gamma = 1 / 10

days = 120

history = []

# ==================================================
# SIMULATION LOOP
# ==================================================

print("\nRunning simulation...")

for day in range(days):

    # ------------------------------------------
    # SPATIAL SPILLOVER
    # ------------------------------------------

    spillover = []

    for i in tracts.index:

        neighbors = w.neighbors[i]

        incoming = 0

        for n in neighbors:
            incoming += tracts.loc[n, "I"] * 0.001  # leakage factor

        spillover.append(incoming)

    tracts["spillover"] = spillover

    # ------------------------------------------
    # VULNERABILITY-ADJUSTED TRANSMISSION
    # ------------------------------------------

    if "final_index" in tracts.columns:
        effective_beta = beta * (1 + tracts["final_index"])
    else:
        effective_beta = beta

    # ------------------------------------------
    # SEIR EQUATIONS
    # ------------------------------------------
    dt = 0.1

    new_exposed = dt * (
        effective_beta
        * tracts["S"]
        * (tracts["I"] + tracts["spillover"])
        / tracts["population"].clip(lower=1e-10)
    )

    new_infected = dt * sigma * tracts["E"]
    new_recovered = dt * gamma * tracts["I"]

    tracts["S"] -= new_exposed
    tracts["E"] += new_exposed - new_infected
    tracts["I"] += new_infected - new_recovered
    tracts["R"] += new_recovered

    # ------------------------------------------
    # STABILITY FIX (NO NEGATIVE POPS)
    # ------------------------------------------

    tracts["S"] = tracts["S"].clip(lower=0)
    tracts["E"] = tracts["E"].clip(lower=0)
    tracts["I"] = tracts["I"].clip(lower=0)
    tracts["R"] = tracts["R"].clip(lower=0)

    # ------------------------------------------
    # RECORD STATE
    # ------------------------------------------

    history.append({
        "day": day,
        "S": tracts["S"].sum(),
        "E": tracts["E"].sum(),
        "I": tracts["I"].sum(),
        "R": tracts["R"].sum()
    })

# ==================================================
# RESULTS
# ==================================================

history = pd.DataFrame(history)

print("\nPeak infections:")
print(history["I"].max())

# ==================================================
# PLOT
# ==================================================

plt.figure(figsize=(10, 6))

plt.plot(history["day"], history["S"], label="Susceptible")
plt.plot(history["day"], history["E"], label="Exposed")
plt.plot(history["day"], history["I"], label="Infected")
plt.plot(history["day"], history["R"], label="Recovered")

plt.title("Spatial SEIR Model – Washington State")
plt.xlabel("Day")
plt.ylabel("Population")
plt.legend()
plt.grid(True)

output_file = OUTPUT_DIR / "seir_curve.png"
plt.savefig(output_file, dpi=300)

print(f"\nSaved plot to: {output_file}")

plt.show()