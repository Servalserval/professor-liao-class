"""選市場、配本金。

每個市場每天的預期淨額（本金 C）：
    獎勵(C) = R × 掛單比例 × sC / (sC + K)      R = 每日獎金、s = 每 $1 的分數、K = 競爭者分數
    成本(C) = (門檻倍數 × 預期虧損率 + 資金成本) × C
最適本金 = argmax 獎勵 − 成本，再套用單一市場、單一事件上限；總額超過預算就調高門檻倍數。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import MMConfig
from .feeds import PublicData
from .rules import Book, MarketInfo


def category(question: str, slug: str = "") -> str:
    t = f"{question} {slug}".lower()
    if any(k in t for k in (" vs", "o/u", "spread:", "win on 20", "nfl", "nba", "mlb", "nhl", "ufc", "premier", "laliga",
                            "serie", "bundesliga", "champions", "world series", "worlds 20", "lol", "cs2", "dota", "atp", "wta")):
        return "運動／電競"
    if any(k in t for k in ("election", "senate", "house", "governor", "president", "prime minister", "mandate",
                            "balance of power", "mayor", "parliament", "party", "nominee", "trump", "minister", "seats")):
        return "政治"
    if any(k in t for k in ("fed ", "interest rate", "cpi", "inflation", "gdp", "unemployment", "jobs", "recession", "bps")):
        return "總經"
    if any(k in t for k in ("bitcoin", "btc", "ethereum", "eth ", "solana", "crypto", "xrp")):
        return "加密貨幣"
    return "其他"


@dataclass
class Candidate:
    condition_id: str
    question: str
    slug: str
    category: str
    yes_token: str
    no_token: str
    rate: float
    v: float
    reward_min_size: float
    end_ts: float | None
    mid: float
    spread: float
    comp: float


def competitor_score(book: Book, mid: float, v: float) -> float:
    """競爭者在最大合格價差內的獎勵分數（保守估計，見 docs/market-making.md）。"""
    def s(dist):
        return ((v - dist) / v) ** 2 if dist < v else 0.0

    q_b = sum(s(abs(mid - p)) * q for p, q in book.bids)
    q_a = sum(s(abs(mid - p)) * q for p, q in book.asks)
    if 0.10 <= mid <= 0.90:
        return max(min(q_b, q_a), max(q_b, q_a) / 3)
    return min(q_b, q_a)


def candidates(rows: list[dict], books: dict[str, Book], cfg: MMConfig, now: float) -> list[Candidate]:
    out, seen = [], set()
    for m in rows:
        if m["condition_id"] in seen:      # 獎勵 API 偶爾會在兩頁重複出現同一個市場
            continue
        seen.add(m["condition_id"])
        toks = m.get("tokens") or []
        yes = next((t for t in toks if str(t.get("outcome", "")).lower() == "yes"), None)
        no = next((t for t in toks if str(t.get("outcome", "")).lower() == "no"), None)
        if not yes or not no:
            continue
        v = float(m.get("rewards_max_spread") or 0) / 100
        b = books.get(str(yes["token_id"]))
        if v <= 0 or b is None or not b.bids or not b.asks:
            continue
        min_size = float(m.get("rewards_min_size") or 0)
        big_b = [p for p, q in b.bids if q >= min_size]
        big_a = [p for p, q in b.asks if q >= min_size]
        if not big_b or not big_a:
            continue
        mid = (max(big_b) + min(big_a)) / 2
        spread = min(p for p, _ in b.asks) - max(p for p, _ in b.bids)
        end = m.get("end_date")
        end_ts = pd.Timestamp(end).timestamp() if end else None
        if spread > v + 1e-9:
            continue
        if end_ts is not None and end_ts - now < cfg.min_days_left * 86400:
            continue
        if not cfg.mid_band[0] <= mid <= cfg.mid_band[1]:
            continue
        cat = category(m.get("question", ""), m.get("event_slug") or "")
        if cat in cfg.exclude_categories:
            continue
        out.append(Candidate(m["condition_id"], m.get("question", ""), m.get("event_slug") or "", cat,
                             str(yes["token_id"]), str(no["token_id"]),
                             sum(float(c.get("rate_per_day") or 0) for c in m["rewards_config"]),
                             v, min_size, end_ts, mid, spread, competitor_score(b, mid, v)))
    return out


def best_capital(R, cov, s, K, h, c_min, c_max) -> tuple[float, float, float]:
    """在 [c_min, c_max] 上找 R·cov·sC/(sC+K) − hC 的最大值。回傳 (本金, 預期淨額, 預期獎勵)。"""
    if c_max < c_min:
        return 0.0, 0.0, 0.0
    grid = np.unique(np.concatenate([np.geomspace(max(c_min, 1), max(c_max, c_min + 1), 160), [c_min]]))
    reward = R * cov * s * grid / (s * grid + K) if K > 0 else np.full_like(grid, R * cov)
    val = reward - h * grid
    i = int(np.argmax(val))
    return (float(grid[i]), float(val[i]), float(reward[i])) if val[i] > 0 else (0.0, 0.0, 0.0)


def allocate(cands: list[Candidate], cfg: MMConfig, mult: float) -> pd.DataFrame:
    rows = []
    for c in cands:
        d = cfg.dist_frac * c.v
        s = (1 - cfg.dist_frac) ** 2 / max(1 - 2 * d, 0.05)
        c_min = max(c.reward_min_size, 5) * (1 - 2 * d)
        c_max = min(cfg.market_cap, c.comp / s) if c.comp > 0 else c_min
        h = mult * cfg.hurdle_by_category.get(c.category, cfg.hurdle_by_category["_"]) + cfg.cost_of_capital / 365
        cov = cfg.coverage_by_category.get(c.category, cfg.coverage_by_category["_"])
        cap, net, reward = best_capital(c.rate, cov, s, c.comp, h, c_min, max(c_max, c_min))
        if cap > 0:
            rows.append(dict(condition_id=c.condition_id, capital=cap, exp_net=net, exp_reward=reward, s=s, cov=cov, h=h,
                             slug=c.slug, category=c.category, rate=c.rate, comp=c.comp, question=c.question))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # 單一事件上限：超過就把同一事件的市場等比例縮小，獎勵沿同一條曲線重算
    total = df.groupby("slug")["capital"].transform("sum")
    scale = np.minimum(1.0, cfg.event_cap / total)
    df["capital"] = df["capital"] * scale
    df["exp_reward"] = np.where(df["comp"] > 0, df["rate"] * df["cov"] * df["s"] * df["capital"] / (df["s"] * df["capital"] + df["comp"]),
                                df["rate"] * df["cov"])
    df["exp_net"] = df["exp_reward"] - df["h"] * df["capital"]
    return df


def solve_budget(cands: list[Candidate], cfg: MMConfig) -> tuple[pd.DataFrame, float]:
    """從 cfg.hurdle_mult 開始，總本金超過預算就調高門檻倍數。"""
    mult = cfg.hurdle_mult
    df = allocate(cands, cfg, mult)
    if df.empty or df["capital"].sum() <= cfg.budget:
        return df, mult
    lo, hi = mult, mult * 2
    while allocate(cands, cfg, hi)["capital"].sum() > cfg.budget:
        lo, hi = hi, hi * 2
    for _ in range(30):
        mid = math.sqrt(lo * hi)
        if allocate(cands, cfg, mid)["capital"].sum() > cfg.budget:
            lo = mid
        else:
            hi = mid
    return allocate(cands, cfg, hi), hi


def _ts(x) -> float | None:
    if not x:
        return None
    try:
        return pd.Timestamp(str(x).replace(" ", "T")).timestamp()
    except ValueError:
        return None


def build_universe(cfg: MMConfig, data: PublicData, now: float | None = None) -> tuple[list[MarketInfo], pd.DataFrame, float]:
    """回傳 (要掛的市場, 配置表, 實際用的門檻倍數)。"""
    now = now or time.time()
    rows = data.reward_markets()
    yes_tokens = []
    for m in rows:
        for t in m.get("tokens") or []:
            if str(t.get("outcome", "")).lower() == "yes":
                yes_tokens.append(str(t["token_id"]))
    books = data.books(yes_tokens)
    cands = candidates(rows, books, cfg, now)
    plan, mult = solve_budget(cands, cfg)
    if plan.empty:
        return [], plan, mult
    plan = plan.sort_values("exp_net", ascending=False)
    if cfg.max_markets:
        plan = plan.head(cfg.max_markets)
    by_id = {c.condition_id: c for c in cands}
    gamma = data.gamma_markets(plan["condition_id"].tolist())
    infos = []
    for r in plan.itertuples(index=False):
        c, g = by_id[r.condition_id], gamma.get(r.condition_id, {})
        sched = g.get("feeSchedule") or {}
        fee = float(sched.get("rate", 0.0)) if g.get("feesEnabled") else 0.0
        infos.append(MarketInfo(
            condition_id=c.condition_id, question=c.question, slug=c.slug, category=c.category,
            yes_token=c.yes_token, no_token=c.no_token, rate=c.rate, v=c.v, reward_min_size=c.reward_min_size,
            comp=c.comp, capital=float(r.capital), end_ts=_ts(g.get("endDate")) or c.end_ts,
            game_start_ts=_ts(g.get("gameStartTime")), fee_rate=fee, neg_risk=bool(g.get("negRisk", False)),
        ))
    # 獎勵 API 的截止日有時比 Gamma 晚；比賽已經開始的運動盤也拿掉（規則反正不會掛）
    infos = [i for i in infos
             if (i.end_ts is None or i.end_ts - now >= cfg.min_days_left * 86400)
             and (i.game_start_ts is None or i.game_start_ts - cfg.sports_stop_before_min * 60 > now)]
    keep = {i.condition_id for i in infos}
    return infos, plan[plan["condition_id"].isin(keep)], mult
