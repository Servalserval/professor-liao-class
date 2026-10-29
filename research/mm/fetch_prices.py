"""下載掛單回測用的 1 分鐘價格（含時間戳，v2 REST 的 timestamp 單位是秒）。"""
import json
import pickle
import time
from pathlib import Path

import pandas as pd
import requests

s = requests.Session()
raw = pickle.load(open("reward_mm_raw.pkl", "rb"))
mm = pd.read_csv("reward_mm.csv")
keep = mm[mm["best_spread_c"] <= mm["max_spread_c"]]["q"].unique()
cid = pd.read_csv("rewards_all.csv")[["q", "condition_id"]]
cid["q70"] = cid["q"].str[:70]
targets = cid[cid["q70"].isin(keep) & cid["condition_id"].isin(raw)].drop_duplicates("condition_id")
out = Path("prices")
out.mkdir(exist_ok=True)
end = int(time.time())
for r in targets.itertuples(index=False):
    f = out / f"{r.condition_id}.csv"
    if f.exists():
        continue
    yes = json.loads(raw[r.condition_id]["m"]["clobTokenIds"])[0]
    h = s.get("https://data-api.polymarket.com/v2/prices-history",
              params={"token_id": yes, "start": end - 7 * 86400, "end": end, "bucket_seconds": 60}, timeout=30)
    pts = (h.json().get("data") or []) if h.status_code == 200 else []
    pd.DataFrame([(int(p["timestamp"]), float(p["price"])) for p in pts], columns=["ts", "price"]).to_csv(f, index=False)
    time.sleep(0.1)
print("完成", len(list(out.glob("*.csv"))))
