import pandas as pd

df = pd.read_csv(
    "data/census/ACSST5Y2023.S1701-Data.csv"
)

print(df.columns.tolist())
print(df.head())