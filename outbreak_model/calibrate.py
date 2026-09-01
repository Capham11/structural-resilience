"""
Phase 4 — SEIR Calibration
Fits beta and gamma to NYT COVID Wave 1 data for Washington State
Calibration window: 2020-03-01 to 2020-06-01
"""

from pathlib import Path
import json
import io

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
import scipy.sparse as sp
import geopandas as gpd
from scipy.optimize import minimize
from libpysal.weights import Queen

# ==================================================
# PATHS
# ==================================================

BASE_DIR   = Path(__file__).parent
DATA_PATH  = BASE_DIR / "outputs" / "washington_vulnerability_enriched.geojson"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

START_DATE = "2020-03-01"
END_DATE   = "2020-06-01"

# ==================================================
# PULL NYT DATA
# ==================================================

print("Pulling NYT county COVID data...")
url = "https://raw.githubusercontent.com/nytimes/covid-19-data/master/us-counties.csv"
res = requests.get(url, timeout=60)
df_raw = pd.read_csv(io.StringIO(res.text))

wa = df_raw[
    (df_raw["state"] == "Washington") &
    (df_raw["date"] >= START_DATE) &
    (df_raw["date"] <= END_DATE)
].copy()

wa["date"] = pd.to_datetime(wa["date"])
wa = wa.sort_values(["county", "date"])
wa["new_cases"] = wa.groupby("county")["cases"].diff().fillna(0).clip(lower=0)
wa["new_cases_smooth"] = wa.groupby("county")["new_cases"].transform(
    lambda x: x.rolling(7, min_periods=1).mean()
)

# Build a clean date index covering the full window
all_dates = pd.date_range(START_DATE, END_DATE, freq="D")
DAYS = len(all_dates)
print(f"Simulation days: {DAYS}  ({START_DATE} → {END_DATE})")

def get_county_series(county_name):
    """Return smoothed daily new cases aligned to all_dates."""
    sub = wa[wa["county"] == county_name].set_index("date")
    series = sub["new_cases_smooth"].reindex(all_dates, fill_value=0)
    return series.values

observed_king = get_county_series("King")
print(f"King County peak: {observed_king.max():.0f} on day {observed_king.argmax()}")

# ==================================================
# LOAD SPATIAL DATA
# ==================================================

print("\nLoading spatial data...")
tracts = gpd.read_file(DATA_PATH)
tracts["population"] = pd.to_numeric(tracts["population"], errors="coerce")
tracts = tracts.dropna(subset=["population"])
tracts = tracts[tracts["population"] > 0].copy()
tracts = tracts.reset_index(drop=True)

N   = len(tracts)
pop = tracts["population"].to_numpy(dtype=np.float64)

vuln = tracts["vuln_blended"].fillna(0).to_numpy(dtype=np.float64) \
       if "vuln_blended" in tracts.columns else np.zeros(N)

print("Building spatial graph...")
w = Queen.from_dataframe(tracts, use_index=False)
rows, cols_list, data_list = [], [], []
for i, neighbors in w.neighbors.items():
    for j in neighbors:
        rows.append(i); cols_list.append(j); data_list.append(0.05)
W_sparse = sp.csr_matrix((data_list, (rows, cols_list)), shape=(N, N))

COUNTY_FIPS = {
    "King": "033", "Pierce": "053", "Snohomish": "061",
    "Spokane": "063", "Yakima": "077",
}
county_masks = {
    name: (tracts["COUNTYFP"].astype(str) == fips).to_numpy()
    for name, fips in COUNTY_FIPS.items()
    if (tracts["COUNTYFP"].astype(str) == fips).sum() > 0
}
print(f"County masks: {list(county_masks.keys())}")

# ==================================================
# FAST SEIR
# ==================================================

def run_seir_fast(beta, gamma, sigma=1/6, dt=0.5, seed_pct=0.001):
    eff_beta = beta * (1.0 + vuln)

    S = pop.copy()
    E = np.zeros(N)
    I = np.zeros(N)
    R = np.zeros(N)

    king_mask = tracts["COUNTYFP"].astype(str) == "033"
    I[king_mask] = seed_pct * pop[king_mask]
    S[king_mask] -= I[king_mask]

    county_new_cases = {name: np.zeros(DAYS) for name in county_masks}

    for day in range(DAYS):
        force   = I + W_sparse @ I
        new_exp = dt * eff_beta * S * force / pop
        new_inf = dt * sigma * E
        new_rec = dt * gamma * I

        S += -new_exp; E += new_exp - new_inf
        I += new_inf  - new_rec; R += new_rec

        np.clip(S, 0, None, out=S); np.clip(E, 0, None, out=E)
        np.clip(I, 0, None, out=I); np.clip(R, 0, None, out=R)

        total = S + E + I + R
        scale = np.where(total > 0, pop / total, 1.0)
        S *= scale; E *= scale; I *= scale; R *= scale

        for name, mask in county_masks.items():
            county_new_cases[name][day] = new_exp[mask].sum()

    return county_new_cases

# ==================================================
# OBJECTIVE — fit curve SHAPE + timing
# ==================================================

def shape_score(obs, sim):
    """Compare normalized shapes — penalizes timing and shape mismatch."""
    obs_n = obs / (obs.max() + 1e-9)
    sim_n = sim / (sim.max() + 1e-9)
    shape_loss = np.sum((obs_n - sim_n) ** 2)

    # Also penalize peak day mismatch
    obs_peak = np.argmax(obs)
    sim_peak = np.argmax(sim)
    timing_loss = ((obs_peak - sim_peak) / DAYS) ** 2 * 10

    return shape_loss + timing_loss

def objective(params):
    beta, gamma = params
    if beta <= 0.05 or beta > 3.0 or gamma <= 0.01 or gamma > 1.0:
        return 1e10
    try:
        sim = run_seir_fast(beta, gamma)
        return shape_score(observed_king, sim["King"])
    except Exception:
        return 1e10

# ==================================================
# GRID SEARCH — wider range
# ==================================================

print("\nRunning grid search...")

best_loss = np.inf
best_beta, best_gamma = 0.3, 0.1

for beta in np.arange(0.1, 1.0, 0.1):
    for gamma in np.arange(0.05, 0.4, 0.05):
        loss = objective([beta, gamma])
        r0   = beta / gamma
        print(f"  beta={beta:.2f}  gamma={gamma:.2f}  R0={r0:.1f}  loss={loss:.4f}")
        if loss < best_loss:
            best_loss = loss
            best_beta, best_gamma = float(beta), float(gamma)

print(f"\nBest grid: beta={best_beta:.3f}  gamma={best_gamma:.3f}  R0={best_beta/best_gamma:.2f}  loss={best_loss:.4f}")

# ==================================================
# LOCAL OPTIMIZATION — from best grid point
# ==================================================

print("\nRunning local optimization...")

result = minimize(
    objective,
    x0=[best_beta, best_gamma],
    method="Nelder-Mead",
    bounds=[(0.05, 2.5), (0.01, 0.8)],
    options={"xatol": 1e-5, "fatol": 1e-7, "maxiter": 2000},
)

cal_beta, cal_gamma = float(result.x[0]), float(result.x[1])
cal_r0 = cal_beta / cal_gamma

print(f"\nCalibrated:")
print(f"  beta  = {cal_beta:.4f}")
print(f"  gamma = {cal_gamma:.4f}")
print(f"  R0    = {cal_r0:.2f}")
print(f"  loss  = {result.fun:.6f}")

# ==================================================
# BOOTSTRAP CI
# ==================================================

print("\nBootstrap uncertainty (30 samples)...")
bootstrap_params = []

for i in range(30):
    noise = np.random.normal(0, observed_king.std() * 0.08, size=DAYS)
    noisy = np.clip(observed_king + noise, 0, None)

    def obj_b(params):
        beta, gamma = params
        if beta <= 0.05 or beta > 3.0 or gamma <= 0.01 or gamma > 1.0:
            return 1e10
        try:
            sim = run_seir_fast(beta, gamma)
            return shape_score(noisy, sim["King"])
        except Exception:
            return 1e10

    r = minimize(obj_b, x0=[cal_beta, cal_gamma], method="Nelder-Mead",
                 options={"xatol": 1e-3, "fatol": 1e-4, "maxiter": 400})
    bootstrap_params.append(r.x.tolist())
    if (i + 1) % 10 == 0:
        print(f"  {i+1}/30")

bp = np.array(bootstrap_params)
beta_ci  = np.percentile(bp[:, 0], [5, 95])
gamma_ci = np.percentile(bp[:, 1], [5, 95])

print(f"\n  beta  95% CI: [{beta_ci[0]:.4f}, {beta_ci[1]:.4f}]")
print(f"  gamma 95% CI: [{gamma_ci[0]:.4f}, {gamma_ci[1]:.4f}]")
print(f"  R0    95% CI: [{cal_beta/gamma_ci[1]:.2f}, {cal_beta/gamma_ci[0]:.2f}]")

# ==================================================
# SAVE JSON
# ==================================================

calibration = {
    "beta":       round(cal_beta, 4),
    "gamma":      round(cal_gamma, 4),
    "sigma":      round(1/6, 4),
    "R0":         round(cal_r0, 2),
    "beta_ci":    [round(float(beta_ci[0]), 4),  round(float(beta_ci[1]), 4)],
    "gamma_ci":   [round(float(gamma_ci[0]), 4), round(float(gamma_ci[1]), 4)],
    "R0_ci":      [round(cal_beta/gamma_ci[1], 2), round(cal_beta/gamma_ci[0], 2)],
    "loss":       round(float(result.fun), 6),
    "window":     f"{START_DATE} to {END_DATE}",
    "target":     "King County daily new cases (7-day smoothed)",
    "note":       "Shape-calibrated to Wave 1. Scale factor reflects case ascertainment fraction."
}

with open(OUTPUT_DIR / "calibration_result.json", "w") as f:
    json.dump(calibration, f, indent=2)
print(f"\nSaved: calibration_result.json")

# ==================================================
# PLOT 1 — KING COUNTY FIT
# ==================================================

sim_best = run_seir_fast(cal_beta, cal_gamma)
sim_king = sim_best["King"]
scale_f  = observed_king.max() / (sim_king.max() + 1e-9)
sim_king_scaled = sim_king * scale_f

# Bootstrap envelope
boot_sims = []
for params in bootstrap_params:
    s  = run_seir_fast(params[0], params[1])
    sk = s["King"]
    sf = observed_king.max() / (sk.max() + 1e-9)
    boot_sims.append(sk * sf)
boot_arr = np.array(boot_sims)
ci_low   = np.percentile(boot_arr, 5,  axis=0)
ci_high  = np.percentile(boot_arr, 95, axis=0)

fig, axes = plt.subplots(2, 1, figsize=(12, 9))

ax = axes[0]
ax.fill_between(all_dates, ci_low, ci_high, alpha=0.2, color="steelblue", label="95% CI")
ax.plot(all_dates, observed_king,    color="crimson",   lw=2,   label="Observed (7-day avg)")
ax.plot(all_dates, sim_king_scaled,  color="steelblue", lw=2, ls="--",
        label=f"Simulated  β={cal_beta:.3f}  γ={cal_gamma:.3f}  R₀={cal_r0:.2f}")
ax.set_title("SEIR Calibration — King County Wave 1 (Mar–Jun 2020)", fontsize=13)
ax.set_ylabel("Daily New Cases")
ax.legend(fontsize=10); ax.grid(True, alpha=0.3)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
plt.setp(ax.xaxis.get_majorticklabels(), rotation=30)

ax2 = axes[1]
residuals = observed_king - sim_king_scaled
colors = ["crimson" if r > 0 else "steelblue" for r in residuals]
ax2.bar(all_dates, residuals, color=colors, alpha=0.6, width=1)
ax2.axhline(0, color="black", lw=0.8)
ax2.set_title("Residuals (Observed − Simulated)", fontsize=11)
ax2.set_ylabel("Cases"); ax2.grid(True, alpha=0.3)
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "calibration_fit.png", dpi=300)
print("Saved: calibration_fit.png")

# ==================================================
# PLOT 2 — MULTI-COUNTY CHECK
# ==================================================

fig2, axes2 = plt.subplots(2, 3, figsize=(16, 9))
axes2 = axes2.flatten()

for i, (county_name, fips) in enumerate(COUNTY_FIPS.items()):
    ax = axes2[i]
    obs = get_county_series(county_name)
    sim = sim_best.get(county_name, np.zeros(DAYS))
    sf  = obs.max() / (sim.max() + 1e-9)

    ax.plot(all_dates, obs,      color="crimson",   linewidth=1.5, label="Observed")
    ax.plot(all_dates, sim * sf, color="steelblue", linewidth=1.5, linestyle="--", label="Simulated")
    ax.set_title(f"{county_name} County", fontsize=11)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())

axes2[-1].axis("off")
plt.suptitle(f"Multi-County Check — β={cal_beta:.3f}  γ={cal_gamma:.3f}  R₀={cal_r0:.2f}", fontsize=13)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "calibration_counties.png", dpi=300)
print("Saved: calibration_counties.png")

# ==================================================
# SUMMARY
# ==================================================

print("\n" + "="*55)
print("CALIBRATION COMPLETE")
print("="*55)
print(f"  beta  = {cal_beta:.4f}   CI: [{beta_ci[0]:.4f}, {beta_ci[1]:.4f}]")
print(f"  gamma = {cal_gamma:.4f}   CI: [{gamma_ci[0]:.4f}, {gamma_ci[1]:.4f}]")
print(f"  R0    = {cal_r0:.2f}     CI: [{cal_beta/gamma_ci[1]:.2f}, {cal_beta/gamma_ci[0]:.2f}]")
print(f"\nUpdate DEFAULT_PARAMS in App.js:")
print(f"  beta:  {cal_beta:.4f}")
print(f"  gamma: {cal_gamma:.4f}")

plt.show()
