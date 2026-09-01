import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).parent

# -----------------------------------------------------
# FUNCTION TO LOAD ACS FILES
# -----------------------------------------------------

def load_acs(filepath):
    """
    Load ACS data and use first row as column names.
    """
    df = pd.read_csv(filepath, dtype=str, header=None)

    # First row contains ACS variable codes
    df.columns = df.iloc[0]

    # Remove metadata rows
    df = df.iloc[2:].reset_index(drop=True)

    return df


# -----------------------------------------------------
# LOAD DATASETS
# -----------------------------------------------------

poverty = load_acs(
    BASE_DIR / "data/census/ACSST5Y2023.S1701-Data.csv"
)

disability = load_acs(
    BASE_DIR / "data/census/ACSST5Y2023.S1810-Data.csv"
)

income = load_acs(
    BASE_DIR / "data/census/ACSST5Y2023.S1901-Data.csv"
)

population = load_acs(
    BASE_DIR / "data/census/ACSDP5Y2023.DP05-Data.csv"
)

population = population[
    ["GEO_ID", "DP05_0001E"]
].rename(columns={
    "DP05_0001E": "population"
})


# -----------------------------------------------------
# SELECT VARIABLES
# -----------------------------------------------------

poverty = poverty[
    ["GEO_ID", "NAME", "S1701_C03_001E"]
].rename(columns={
    "S1701_C03_001E": "poverty_rate"
})

disability = disability[
    ["GEO_ID", "S1810_C03_001E"]
].rename(columns={
    "S1810_C03_001E": "disability_rate"
})

income = income[
    ["GEO_ID", "S1901_C03_001E"]
].rename(columns={
    "S1901_C03_001E": "median_income"
})


# -----------------------------------------------------
# MERGE TABLES
# -----------------------------------------------------

merged = poverty.merge(
    disability,
    on="GEO_ID",
    how="left"
)

merged = merged.merge(
    income,
    on="GEO_ID",
    how="left"
)

merged = merged.merge(
    population,
    on="GEO_ID",
    how="left"
)


# -----------------------------------------------------
# CREATE TRACT ID FOR QGIS JOIN
# -----------------------------------------------------

merged["TRACT_ID"] = (
    merged["GEO_ID"]
    .str.replace("1400000US", "", regex=False)
)


# -----------------------------------------------------
# CLEAN NUMERIC COLUMNS
# -----------------------------------------------------

numeric_cols = [
    "poverty_rate",
    "disability_rate",
    "median_income",
    "population"
]

for col in numeric_cols:

    merged[col] = (
        merged[col]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("-", "", regex=False)
        .str.strip()
    )

    merged[col] = pd.to_numeric(
        merged[col],
        errors="coerce"
    )


# -----------------------------------------------------
# CREATE INCOME RISK SCORE
# (lower income = higher vulnerability)
# -----------------------------------------------------

income_max = merged["median_income"].max()

merged["income_risk"] = (
    (income_max - merged["median_income"])
    / income_max
) * 100


# -----------------------------------------------------
# CREATE COMPOSITE VULNERABILITY SCORE
# -----------------------------------------------------

merged["vulnerability_score"] = (
      0.40 * merged["poverty_rate"]
    + 0.30 * merged["disability_rate"]
    + 0.30 * merged["income_risk"]
)


# -----------------------------------------------------
# FORCE NUMERIC TYPES
# -----------------------------------------------------

merged["poverty_rate"] = merged[
    "poverty_rate"
].astype(float)

merged["disability_rate"] = merged[
    "disability_rate"
].astype(float)

merged["median_income"] = merged[
    "median_income"
].astype(float)

merged["income_risk"] = merged[
    "income_risk"
].astype(float)

merged["vulnerability_score"] = merged[
    "vulnerability_score"
].astype(float)


# -----------------------------------------------------
# CHECK TYPES
# -----------------------------------------------------

print("\nCOLUMN TYPES\n")
print(merged.dtypes)


# -----------------------------------------------------
# EXPORT CSV
# -----------------------------------------------------

merged.to_csv(
    "data/census/census_vulnerability.csv",
    index=False
)

print("\nExport complete!")
print("\nFirst five rows:\n")
print(merged.head())