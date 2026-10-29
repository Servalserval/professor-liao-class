"""BTC 每日盤：掛單方（被吃的那一邊）抱到結算的實際盈虧，加上 20% 手續費返佣。

每一筆吃單成交，轉成「Yes 的價格 p、吃單方方向 dir（+1 = 買 Yes）」：
- 買 Yes @ p → dir +1、價格 p；賣 Yes @ p → dir −1、價格 p
- 買 No @ q  → 等同賣 Yes @ 1 − q；賣 No @ q → 等同買 Yes @ 1 − q
掛單方每股盈虧（抱到結算）= −dir × (結果 − p)，再加返佣 0.2 × 0.07 × p × (1 − p)。
"""
import json
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

s = requests.Session()
rows = []
for d in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)):
    slug = f"bitcoin-above-on-{d:%B}-{d.day}-{d.year}".lower()
    ev = s.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=20).json()
    if not ev:
        print("沒有", slug)
        continue
    for m in ev[0]["markets"]:
        prices = json.loads(m["outcomePrices"])
        outcome = 1 if float(prices[0]) > 0.99 else 0 if float(prices[1]) > 0.99 else None
        if outcome is None:
            continue
        end = pd.Timestamp(m["endDate"]).timestamp()
        cursor, n = None, 0
        while True:
            params = {"condition": m["conditionId"], "limit": 500}
            if cursor:
                params["cursor"] = cursor
            r = s.get("https://data-api.polymarket.com/v2/trades", params=params, timeout=30)
            if r.status_code != 200:
                print(r.status_code, r.text[:200])
                break
            j = r.json()
            for t in j["data"]:
                p, side, out = float(t["price"]), t["side"], t["outcome"]
                if out == "Yes":
                    yp, direction = p, (1 if side == "BUY" else -1)
                else:
                    yp, direction = 1 - p, (-1 if side == "BUY" else 1)
                rows.append(dict(day=d.isoformat(), question=m["question"], strike=m.get("groupItemTitle"), outcome=outcome,
                                 yes_price=yp, dir=direction, shares=float(t["size"]), ts=int(t["timestamp"]),
                                 hours_left=(end - int(t["timestamp"])) / 3600))
                n += 1
            cursor = (j.get("pagination") or {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.05)
        print(f"{m['question'][:55]:55s} 成交 {n}")
df = pd.DataFrame(rows)
df = df[(df["yes_price"] > 0) & (df["yes_price"] < 1)]
df["maker_pnl"] = -df["dir"] * (df["outcome"] - df["yes_price"])
df["rebate"] = 0.2 * 0.07 * df["yes_price"] * (1 - df["yes_price"])
df.to_csv("btc_maker_trades.csv", index=False)
print(len(df), "筆吃單成交，", df["shares"].sum().round(0), "股")
