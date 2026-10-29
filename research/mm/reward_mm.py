"""估計「掛單嚕流動性獎勵」的收入與跳價成本。

對每個市場：
1. 用即時訂單簿（Yes 那一本，已包含鏡像的 No 單）算出競爭者在最大合格價差內的獎勵分數 Q。
   分數 = ((v − s)/v)² × 股數，v = 最大合格價差（分），s = 離中價的距離（分）。
   競爭者總分取保守值：中價在 10%–90% 之間用 max(min(Q買, Q賣), max(Q買, Q賣)/3)，否則用 min(Q買, Q賣)。
2. 假設你兩邊各掛 N 股、離中價 s_me 分，資金 C ≈ N × (1 − 2 s_me)。
   每天獎勵 ≈ R × S(s_me)·N / (S(s_me)·N + 競爭者總分)。
3. 跳價成本：過去 7 天的 1 分鐘價格，只要一分鐘內價格移動超過 s_me，就假設你來不及撤單、
   被吃掉 N 股、虧 (|Δ| − s_me) × N。這對「每分鐘調整一次」的程式是合理的近似；
   更快的程式會少虧一些，但比你快的人（看到新聞就吃單的人）一定存在。
限制：只有一個時間點的訂單簿；7 天內沒發生的大事件（例如開票）不會出現在跳價成本裡。
"""
import json
import pickle
import time

import numpy as np
import pandas as pd
import requests

s = requests.Session()
df = pd.read_csv("rewards_all.csv")
df = df[df["rate"] > 0]
top = df.sort_values("rate", ascending=False).head(60)
mid_tier = df[(df["rate"] >= 20) & (df["rate"] < 200) & ~df["condition_id"].isin(top["condition_id"])].sample(60, random_state=7)
sample = pd.concat([top.assign(group="top60"), mid_tier.assign(group="20-200 隨機")])
now = int(time.time())


def category(q: str, slug: str) -> str:
    t = f"{q} {slug}".lower()
    if any(k in t for k in (" vs", "o/u", "spread:", "win on 20", "nfl", "nba", "mlb", "nhl", "ufc", "premier", "laliga",
                            "serie", "bundesliga", "champions", "world series", "worlds 20", "lol", "cs2", "dota", "atp", "wta")):
        return "運動／電競"
    if any(k in t for k in ("election", "senate", "house", "governor", "president", "prime minister", "mandate", "balance of power",
                            "mayor", "parliament", "party", "nominee", "trump", "minister", "seats")):
        return "政治"
    if any(k in t for k in ("fed ", "interest rate", "cpi", "inflation", "gdp", "unemployment", "jobs", "recession", "bps")):
        return "總經"
    if any(k in t for k in ("bitcoin", "btc", "ethereum", "eth ", "solana", "crypto", "xrp")):
        return "加密貨幣"
    return "其他"


rows, raw = [], {}
for r in sample.itertuples(index=False):
    try:
        m = s.get("https://gamma-api.polymarket.com/markets", params={"condition_ids": r.condition_id}, timeout=20).json()
        if not m:
            continue
        m = m[0]
        if m.get("closed"):
            continue
        yes = json.loads(m["clobTokenIds"])[0]
        b = s.get("https://clob.polymarket.com/book", params={"token_id": yes}, timeout=20).json()
        bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
        asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
        big_b = [p for p, q in bids if q >= r.min_size]
        big_a = [p for p, q in asks if q >= r.min_size]
        if not big_b or not big_a:
            continue
        mid = (max(big_b) + min(big_a)) / 2
        v = float(r.max_spread)

        def S(dist_c):
            return ((v - dist_c) / v) ** 2 if dist_c < v else 0.0

        q_b = sum(S(abs(mid - p) * 100) * q for p, q in bids)
        q_a = sum(S(abs(mid - p) * 100) * q for p, q in asks)
        two_sided_only = not (0.10 <= mid <= 0.90)
        comp = min(q_b, q_a) if two_sided_only else max(min(q_b, q_a), max(q_b, q_a) / 3)

        h = s.get("https://data-api.polymarket.com/v2/prices-history",
                  params={"token_id": yes, "start": now - 7 * 86400, "end": now, "bucket_seconds": 60}, timeout=30)
        pts = (h.json().get("data") or []) if h.status_code == 200 else []
        px = np.array([float(p["price"]) for p in pts]) if pts else np.array([])
        raw[r.condition_id] = dict(px=px, bids=bids, asks=asks, m=m)
        # 薄的訂單簿裡，「每分鐘價格」會在買賣價之間閃動；用 5 分鐘中位數濾掉，只看持續的移動
        if len(px) >= 5:
            px = pd.Series(px).rolling(5, center=True, min_periods=1).median().to_numpy()
        days = max((len(px) - 1) / 1440, 1e-9)
        dpx = np.abs(np.diff(px)) if len(px) > 1 else np.array([0.0])
        best_spread_c = (min(a for a, _ in asks) - max(b for b, _ in bids)) * 100 if bids and asks else None

        end = pd.Timestamp(m.get("endDate")) if m.get("endDate") else None
        for dist_c in (1.0, v / 2):
            d = dist_c / 100
            for cap in (1_000, 10_000):
                n = cap / max(1 - 2 * d, 0.05)
                mine = S(dist_c) * n
                reward = r.rate * mine / (mine + comp) if mine + comp > 0 else 0.0
                loss = n * np.clip(dpx - d, 0, None).sum() / days
                rows.append(dict(
                    group=r.group, category=category(r.q, str(r.slug)), q=r.q[:70], rate=r.rate, min_size=r.min_size,
                    max_spread_c=v, mid=round(mid, 3), days_to_end=round((end - pd.Timestamp.now(tz="UTC")).days, 0) if end is not None else None,
                    comp_score=round(comp), comp_per_dollar=round(comp / r.rate, 1), dist_c=round(dist_c, 2), capital=cap,
                    reward_day=round(reward, 2), jump_loss_day=round(loss, 2), net_day=round(reward - loss, 2),
                    apr_net=round((reward - loss) * 365 / cap, 2), apr_reward=round(reward * 365 / cap, 2),
                    min_per_day_moves=round((dpx > d).sum() / days, 1), max_1m_move_c=round(dpx.max() * 100, 1),
                    hist_days=round(days, 1), best_spread_c=round(best_spread_c, 2) if best_spread_c is not None else None,
                ))
        time.sleep(0.1)
    except Exception as e:  # 單一市場失敗就跳過
        print("skip", r.q[:50], e)
pickle.dump(raw, open("reward_mm_raw.pkl", "wb"))
out = pd.DataFrame(rows)
out.to_csv("reward_mm.csv", index=False)
print(len(out) // 4, "個市場")
