"""全站盤點：所有有流動性獎勵的市場，現在的訂單簿、競爭分數、能不能套用掛單規則。

競爭分數的算法同 reward_mm.py（保守值）；另外算出「我掛在 0.7v」時每一股的分數。
"""
import json
import time

import pandas as pd
import requests

s = requests.Session()
rows, cursor = [], ""
for _ in range(400):
    params = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
    if cursor:
        params["next_cursor"] = cursor
    r = s.get("https://clob.polymarket.com/rewards/markets/multi", params=params, timeout=30)
    r.raise_for_status()
    d = r.json()
    page = d.get("data", [])
    rates = [sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or [])) for m in page]
    rows += [m for m, rt in zip(page, rates) if rt > 0]
    cursor = d.get("next_cursor")
    if not cursor or cursor == "LTE=" or (rates and max(rates) == 0):
        break
print("有獎勵的市場：", len(rows), flush=True)

mk = []
for m in rows:
    toks = m.get("tokens") or []
    yes = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), toks[0] if toks else None)
    if not yes:
        continue
    mk.append(dict(condition_id=m["condition_id"], q=m["question"], slug=m.get("event_slug"), end=m.get("end_date"),
                   rate=sum(float(c.get("rate_per_day") or 0) for c in m["rewards_config"]),
                   min_size=float(m.get("rewards_min_size") or 0), v=float(m.get("rewards_max_spread") or 0) / 100,
                   token=yes["token_id"], vol24=m.get("volume_24hr")))
mk = pd.DataFrame(mk).drop_duplicates("condition_id")

books = {}
toks = mk["token"].tolist()
for i in range(0, len(toks), 500):
    r = s.post("https://clob.polymarket.com/books", json=[{"token_id": t} for t in toks[i:i + 500]], timeout=60)
    if r.status_code != 200:
        print("books", r.status_code, r.text[:200], flush=True)
        continue
    for b in r.json():
        books[b["asset_id"]] = b
    time.sleep(0.2)
print("訂單簿：", len(books), flush=True)

out = []
for r in mk.itertuples(index=False):
    b = books.get(r.token)
    if not b or r.v <= 0:
        continue
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    if not bids or not asks:
        out.append(dict(r._asdict(), ok=False, why="單邊或空的訂單簿"))
        continue
    best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
    big_b = [p for p, q in bids if q >= r.min_size]
    big_a = [p for p, q in asks if q >= r.min_size]
    if not big_b or not big_a:
        out.append(dict(r._asdict(), ok=False, why="沒有達到最低股數的報價"))
        continue
    mid = (max(big_b) + min(big_a)) / 2

    def S(dist):
        return ((r.v - dist) / r.v) ** 2 if dist < r.v else 0.0

    q_b = sum(S(abs(mid - p)) * q for p, q in bids)
    q_a = sum(S(abs(mid - p)) * q for p, q in asks)
    two_sided_only = not (0.10 <= mid <= 0.90)
    comp = min(q_b, q_a) if two_sided_only else max(min(q_b, q_a), max(q_b, q_a) / 3)
    spread = best_ask - best_bid
    end = pd.Timestamp(r.end) if r.end else None
    days_left = (end - pd.Timestamp.now(tz="UTC")).total_seconds() / 86400 if end is not None else None
    why = []
    if spread > r.v + 1e-9:
        why.append("價差大於最大合格價差")
    if days_left is not None and days_left < 0:
        why.append("已過截止日")
    if not (0.03 <= mid <= 0.97):
        why.append("中價太接近 0 或 1")
    d = 0.7 * r.v
    out.append(dict(r._asdict(), mid=mid, spread=spread, comp=comp, days_left=days_left,
                    score_per_dollar=(0.3 ** 2) / max(1 - 2 * d, 0.05), ok=not why, why="、".join(why)))
u = pd.DataFrame(out)
u.to_csv("universe.csv", index=False)
print(u["ok"].value_counts().to_dict())
print(u[~u["ok"]]["why"].value_counts().head(8).to_string())
ok = u[u["ok"]]
print(f"可以套用的市場 {len(ok)} 個，獎勵合計 ${ok['rate'].sum():,.0f}/天（全部 ${u['rate'].sum():,.0f}/天）")
