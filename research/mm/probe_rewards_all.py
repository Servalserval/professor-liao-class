"""全站有流動性獎勵的市場：獎勵有多大、集中在哪裡。"""
import json

import pandas as pd
import requests

s = requests.Session()
rows, cursor = [], ""
for _ in range(200):
    params = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
    if cursor:
        params["next_cursor"] = cursor
    r = s.get("https://clob.polymarket.com/rewards/markets/multi", params=params, timeout=30)
    if r.status_code != 200:
        print(r.status_code, r.text[:200])
        break
    d = r.json()
    rows += d.get("data", [])
    cursor = d.get("next_cursor")
    if not cursor or cursor == "LTE=":
        break
print("抓到", len(rows), "個市場")
out = []
for m in rows:
    rate = sum(float(c.get("rate_per_day") or 0) for c in m.get("rewards_config") or [])
    toks = m.get("tokens") or []
    price = float(toks[0].get("price")) if toks and toks[0].get("price") is not None else None
    out.append(dict(rate=rate, min_size=m.get("rewards_min_size"), max_spread=m.get("rewards_max_spread"),
                    spread=m.get("spread"), vol24=m.get("volume_24hr"), comp=m.get("market_competitiveness"),
                    price=price, end=str(m.get("end_date"))[:10], q=m.get("question"), slug=m.get("event_slug"),
                    condition_id=m.get("condition_id")))
df = pd.DataFrame(out).drop_duplicates("condition_id")
df = df[df["rate"] > 0].sort_values("rate", ascending=False)
df.to_csv("rewards_all.csv", index=False)
print(f"有獎勵的市場：{len(df)} 個，每天合計 ${df['rate'].sum():,.0f}")
print("每天獎勵的分布：", df["rate"].describe(percentiles=[.5, .9, .99]).round(1).to_dict())
for cut in (10, 50, 100, 500, 1000):
    sub = df[df["rate"] >= cut]
    print(f"  ≥ ${cut}/天：{len(sub)} 個市場，合計 ${sub['rate'].sum():,.0f}/天")
pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 70)
print(df.head(30)[["rate", "min_size", "max_spread", "spread", "vol24", "comp", "price", "end", "q"]].to_string(index=False))
