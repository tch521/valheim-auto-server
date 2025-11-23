from pathlib import Path

import pandas as pd
pd.set_option('display.max_columns', None)
pd.set_option('display.width', None)
pd.set_option('display.max_colwidth', None)

df = pd.read_csv(Path(__file__).parent / "instanceTypes.csv")

filtered_df = df[
    (df["vCPUs"] >= 2)
    & (df["vCPUs"] <= 4)
    & (df["Memory (GiB)"] == 8)
    # & (df["Memory (GiB)"] <= 16)
    & (df["Architecture"].str.contains("x86_64"))
    & (df["Current generation"] == True)
]

filtered_df = filtered_df.sort_values(by="On-Demand Linux pricing")
print(filtered_df.head(20).iloc[:, [0, 8, 14, 15, 22, 45]])

filtered_df.to_csv(Path(__file__).parent / "filtered_instances.csv", index=False)
