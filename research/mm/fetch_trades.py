"""下載掛單回測用的逐筆吃單成交（過去 7 天），每個市場一個 CSV。

只抓目前有真正雙邊報價（價差 ≤ 最大合格價差）的市場。成交轉成 Yes 的價格與吃單方方向。
"""
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
out = Path("trades")
out.mkdir(exist_ok=True)
start = int(time.time()) - 7 * 86400
print(len(targets), "個市場", flush=True)
for i, r in enumerate(targets.itertuples(index=False)):
    f = out / f"{r.condition_id}.csv"
    if f.exists():
        continue
    rows, cursor, pages, errors = [], None, 0, 0
    while pages < 400 and errors < 5:
        params = {"condition": r.condition_id, "limit": 500, "start": start}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = s.get("https://data-api.polymarket.com/v2/trades", params=params, timeout=30)
        except requests.RequestException as e:
            print("  網路錯誤", e, flush=True)
            errors += 1
            time.sleep(2)
            continue
        if resp.status_code == 429:
            errors += 1
            time.sleep(5)
            continue
        if resp.status_code != 200:
            print("  ", resp.status_code, resp.text[:120], flush=True)
            break
        j = resp.json()
        rows += j["data"]
        pages += 1
        cursor = (j.get("pagination") or {}).get("next_cursor")
        if not cursor or (j["data"] and int(j["data"][-1]["timestamp"]) < start):
            break
        time.sleep(0.05)
    m = raw[r.condition_id]["m"]
    yes_token = json.loads(m["clobTokenIds"])[0]
    recs = []
    for t in rows:
        ts = int(t["timestamp"])
        if ts < start:
            continue
        p, size = float(t["price"]), float(t["size"])
        if t["token_id"] == yes_token:
            yp, d = p, (1 if t["side"] == "BUY" else -1)
        else:
            yp, d = 1 - p, (-1 if t["side"] == "BUY" else 1)
        recs.append((ts, yp, d, size))
    pd.DataFrame(recs, columns=["ts", "yes_price", "dir", "shares"]).sort_values("ts").to_csv(f, index=False)
    print(f"[{i + 1}/{len(targets)}] {r.q[:50]:50s} 成交 {len(recs):6d}（{pages} 頁）", flush=True)
print("完成", flush=True)
