import pandas as pd

# -------------------
# Load datasets
# -------------------

poverty = pd.read_csv(
    "data/census/ACSST5Y2023.S1701-Data.csv",
    header=1,
    dtype=str
)

disability = pd.read_csv(
    "data/census/ACSST5Y2023.S1810-Data.csv",
    header=1,
    dtype=str
)

income = pd.read_csv(
    "data/census/ACSST5Y2023.S1901-Data.csv",
    header=1,
    dtype=str
)

# -------------------
# Keep only needed columns
# -------------------

poverty = poverty[
    ["GEO_ID", "NAME", "S1701_C03_001E"]
]

disability = disability[
    ["GEO_ID", "S1810_C03_001E"]
]

income = income[
    ["GEO_ID", "S1901_C03_001E"]
]

# -------------------
# Rename columns
# -------------------

poverty.columns = [
    "GEO_ID",
    "NAME",
    "poverty_rate"
]

disability.columns = [
    "GEO_ID",
    "disability_rate"
]

income.columns = [
    "GEO_ID",
    "median_income"
]

# -------------------
# Merge datasets
# -------------------

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

# -------------------
# Convert to numbers
# -------------------

merged["poverty_rate"] = pd.to_numeric(
    merged["poverty_rate"],
    errors="coerce"
)

merged["disability_rate"] = pd.to_numeric(
    merged["disability_rate"],
    errors="coerce"
)

merged["median_income"] = pd.to_numeric(
    merged["median_income"],
    errors="coerce"
)

# -------------------
# Create vulnerability score
# -------------------

# Higher poverty = more vulnerable
# Higher disability = more vulnerable
# Lower income = more vulnerable

merged["income_inverse"] = (
    merged["median_income"].max()
    - merged["median_income"]
)

merged["vulnerability_score"] = (
      merged["poverty_rate"]
    + merged["disability_rate"]
    + (merged["income_inverse"] /
       merged["income_inverse"].max() * 100)
)

# -------------------
# Save for QGIS
# -------------------

merged.to_csv(
    "data/census/vulnerability_index.csv",
    index=False
)

print("Finished!")
print(merged.head())