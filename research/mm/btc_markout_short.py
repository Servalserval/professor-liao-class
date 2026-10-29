"""BTC 每日盤：掛單方被吃之後 Δ 分鐘的盈虧（markout），以及加上返佣後的淨值。

markout(Δ) = −dir × (P(t+Δ) − 成交價)，P 是 Polymarket 1 分鐘價格（data-api v2/prices-history）。
正值 = 掛單方賺（吃單的人事後看起來是錯的），負值 = 掛單方被逆選擇。
"""
import json
import time
from datetime import date

import numpy as np
import pandas as pd
import requests

s = requests.Session()
tr = pd.read_csv("btc_maker_trades.csv")
series = {}
for d in sorted(tr["day"].unique()):
    dd = date.fromisoformat(d)
    ev = s.get("https://gamma-api.polymarket.com/events",
               params={"slug": f"bitcoin-above-on-{dd:%B}-{dd.day}-{dd.year}".lower()}, timeout=20).json()[0]
    for m in ev["markets"]:
        yes = json.loads(m["clobTokenIds"])[0]
        end = int(pd.Timestamp(m["endDate"]).timestamp())
        h = s.get("https://data-api.polymarket.com/v2/prices-history",
                  params={"token_id": yes, "start": end - 4 * 86400, "end": end, "bucket_seconds": 60}, timeout=30).json()
        pts = h.get("data") or []
        # v2 REST 的 timestamp 是秒（SDK 文件範例寫的是毫秒）
        series[m["question"]] = pd.Series([float(p["price"]) for p in pts],
                                          index=[int(p["timestamp"]) for p in pts]).sort_index()
        time.sleep(0.05)

out = []
for q, g in tr.groupby("question"):
    ser = series.get(q)
    if ser is None or ser.empty:
        continue
    idx = ser.index.to_numpy()
    for lag in (1, 5, 30, 120):
        pos = np.searchsorted(idx, g["ts"].to_numpy() + lag * 60, side="right") - 1
        ok = pos >= 0
        later = np.where(ok, ser.to_numpy()[np.clip(pos, 0, None)], np.nan)
        g = g.assign(**{f"mo{lag}": -g["dir"] * (later - g["yes_price"])})
    out.append(g)
df = pd.concat(out)
df["rebate"] = 0.2 * 0.07 * df["yes_price"] * (1 - df["yes_price"])
df["hl"] = pd.cut(df["hours_left"], [-1, 1, 6, 24, 1e9], labels=["<1 小時", "1–6 小時", "6–24 小時", ">24 小時"])
df["pb"] = pd.cut(df["yes_price"], [0, 0.05, 0.2, 0.8, 0.95, 1], labels=["<5¢", "5–20¢", "20–80¢", "80–95¢", ">95¢"])
df.to_csv("btc_markouts.csv", index=False)


def summary(g):
    w = g["shares"]
    res = {"成交": len(g), "股數": int(w.sum())}
    for lag in (1, 5, 30, 120):
        col = f"mo{lag}"
        ok = g[col].notna()
        res[f"{lag}分後(¢)"] = round(np.average(g.loc[ok, col], weights=w[ok]) * 100, 2)
    res["返佣(¢)"] = round(np.average(g["rebate"], weights=w) * 100, 2)
    return pd.Series(res)


pd.set_option("display.width", 220)
print("全部（依股數加權，每股、單位：分）：")
print(summary(df).to_string())
print("\n依距離結算：")
print(df.groupby("hl", observed=True).apply(summary).to_string())
print("\n依價格：")
print(df.groupby("pb", observed=True).apply(summary).to_string())
print("\n依單筆大小（股）：")
df["sz"] = pd.cut(df["shares"], [0, 50, 500, 5000, 1e9], labels=["<50", "50–500", "500–5k", ">5k"])
print(df.groupby("sz", observed=True).apply(summary).to_string())
