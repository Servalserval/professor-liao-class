"""掛單嚕獎勵的逐分鐘回測：比較各種「什麼時候不要掛」的規則。

模擬（每個市場 $1,000、兩邊各 N 股、離中價 d）：
- 每分鐘開頭用「當下以前」的資料決定要不要掛；中價 = 最後一筆 1 分鐘價格。
- 被吃：這一分鐘內有吃單成交打到或穿過我的價位（賣單價 ≤ 我的買價，或買單價 ≥ 我的賣價）。
- 成交盈虧：用 10 分鐘後的價格結算，再扣 0.5¢／股的平倉成本；加上 20% 手續費返佣。
- 獎勵：只有掛單的分鐘才有，= 每日獎金 / 1440 × 我的分數 / (我的分數 + 競爭者分數)。
  競爭者分數用一個時間點的訂單簿快照（假設整週不變）。
參數在前半週選、在後半週驗證。
"""
import json
import pickle
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

H = 10            # 成交後幾分鐘結算盈虧
UNWIND = 0.005    # 平倉成本（每股）
CAPITAL = 1000.0
DAY = 1440

raw = pickle.load(open("reward_mm_raw.pkl", "rb"))
mm = pd.read_csv("reward_mm.csv")
mm = mm[(mm["capital"] == 1000) & (mm["dist_c"] != 1.0) & (mm["best_spread_c"] <= mm["max_spread_c"])]
cid = pd.read_csv("rewards_all.csv")[["q", "condition_id"]]
cid["q70"] = cid["q"].str[:70]
meta = mm.merge(cid.drop_duplicates("q70"), left_on="q", right_on="q70", suffixes=("", "_full"))


@dataclass
class Market:
    q: str
    category: str
    v: float            # 最大合格價差（價格單位）
    rate: float
    comp: float
    fee_rate: float
    t0: int
    p: np.ndarray       # 每分鐘價格（前值填補）
    end_ts: int | None
    min_sell: np.ndarray = field(repr=False, default=None)   # 這一分鐘吃單賣出的最低價
    max_buy: np.ndarray = field(repr=False, default=None)    # 這一分鐘吃單買入的最高價
    vol: np.ndarray = field(repr=False, default=None)        # 這一分鐘的成交股數
    big: np.ndarray = field(repr=False, default=None)        # 這一分鐘最大單筆股數


def load() -> list[Market]:
    out = []
    for r in meta.itertuples(index=False):
        pf, tf = Path("prices") / f"{r.condition_id}.csv", Path("trades") / f"{r.condition_id}.csv"
        if not pf.exists() or not tf.exists():
            continue
        px = pd.read_csv(pf)
        if len(px) < 2 * DAY:
            continue
        t0 = int(px["ts"].min() // 60 * 60)
        n = int((px["ts"].max() - t0) // 60) + 1
        grid = pd.Series(np.nan, index=np.arange(n))
        grid.loc[((px["ts"] - t0) // 60).astype(int).to_numpy()] = px["price"].to_numpy()
        p = grid.ffill().bfill().to_numpy()
        tr = pd.read_csv(tf)
        tr = tr[(tr["ts"] >= t0) & (tr["ts"] < t0 + n * 60)]
        b = ((tr["ts"] - t0) // 60).astype(int)
        min_sell = np.full(n, np.inf)
        max_buy = np.full(n, -np.inf)
        vol = np.zeros(n)
        big = np.zeros(n)
        sells, buys = tr["dir"] < 0, tr["dir"] > 0
        np.minimum.at(min_sell, b[sells].to_numpy(), tr.loc[sells, "yes_price"].to_numpy())
        np.maximum.at(max_buy, b[buys].to_numpy(), tr.loc[buys, "yes_price"].to_numpy())
        np.add.at(vol, b.to_numpy(), tr["shares"].to_numpy())
        np.maximum.at(big, b.to_numpy(), tr["shares"].to_numpy())
        m = raw[r.condition_id]["m"]
        sched = m.get("feeSchedule") or {}
        fee = float(sched.get("rate", 0.0)) if m.get("feesEnabled") else 0.0
        end = int(pd.Timestamp(m["endDate"]).timestamp()) if m.get("endDate") else None
        out.append(Market(r.q, r.category, r.max_spread_c / 100, r.rate, r.comp_score, fee, t0, p, end,
                          min_sell, max_buy, vol, big))
    return out


@dataclass(frozen=True)
class Rules:
    dist_frac: float = 0.5        # 掛單距離 = 最大合格價差 × 這個比例
    ttl_hours: float | None = None    # 距離到期少於幾小時就停（已過到期一律不掛）
    vol_window: int = 0           # 波動暫停：過去幾分鐘的價格區間
    vol_mult: float = 0.0         #   區間 > vol_mult × 掛單距離 就暫停
    volu_window: int = 0          # 成交量暫停：過去幾分鐘成交量
    volu_mult: float = 0.0        #   > volu_mult × 過去 24 小時的平均 就暫停
    cooldown: int = 0             # 跳價或被吃之後暫停幾分鐘
    stop_loss: float | None = None    # 單日成交虧損超過多少美元就停到當天結束
    band: tuple[float, float] | None = None   # 只在中價落在這個區間時掛
    big_mult: float = 0.0         # 最近 10 分鐘單筆成交 > big_mult × 我的股數 → 暫停 30 分鐘
    adaptive: float = 0.0         # 波動大時（60 分鐘區間 > 掛單距離）改掛在這個比例（0 = 不用）
    trail_hours: int = 0          # 過去幾小時「假設一直掛」的淨額 < 0 就暫停（0 = 不用）
    one_sided: bool = False       # 波動暫停觸發時，改成只掛「趨勢反方向」的單邊（分數 ÷ 3），而不是完全暫停

    def label(self) -> str:
        parts = []
        if self.dist_frac != 0.5:
            parts.append(f"距離 {self.dist_frac:g}v")
        if self.ttl_hours is not None:
            parts.append(f"到期前 {self.ttl_hours:g}h 停")
        if self.vol_window:
            parts.append(f"{self.vol_window} 分鐘波動 > {self.vol_mult:g}d 暫停")
        if self.volu_window:
            parts.append(f"{self.volu_window} 分鐘量 > {self.volu_mult:g}× 暫停")
        if self.cooldown:
            parts.append(f"跳價/被吃後冷卻 {self.cooldown} 分")
        if self.stop_loss is not None:
            parts.append(f"單日虧 ${self.stop_loss:g} 停")
        if self.band:
            parts.append(f"只掛 {self.band[0]:g}–{self.band[1]:g}")
        if self.big_mult:
            parts.append(f"大單 > {self.big_mult:g}N 暫停")
        if self.adaptive:
            parts.append(f"波動大改掛 {self.adaptive:g}v")
        if self.trail_hours:
            parts.append(f"過去 {self.trail_hours}h 虧就暫停")
        if self.one_sided:
            parts.append("波動大改單邊")
        return "、".join(parts) or "基準（一直掛）"


def rolling_range(p: np.ndarray, w: int) -> np.ndarray:
    """過去 w 分鐘（含當下）的最高價 − 最低價。"""
    s = pd.Series(p)
    return (s.rolling(w + 1, min_periods=1).max() - s.rolling(w + 1, min_periods=1).min()).to_numpy()


def trailing_sum(x: np.ndarray, w: int) -> np.ndarray:
    """前 w 分鐘（不含當下這一分鐘）的總和。"""
    c = np.concatenate([[0.0], np.cumsum(x)])
    i = np.arange(len(x))
    return c[i] - c[np.maximum(i - w, 0)]


def per_minute(mk: Market, frac: float):
    """以距離 frac × v 掛單時，每分鐘的獎勵、成交盈虧、返佣、是否被吃。"""
    d = frac * mk.v
    n_sh = CAPITAL / max(1 - 2 * d, 0.05)
    score = (1 - frac) ** 2 * n_sh
    reward = mk.rate / DAY * score / (score + mk.comp)
    bid, ask = mk.p - d, mk.p + d
    later = mk.p[np.minimum(np.arange(len(mk.p)) + H, len(mk.p) - 1)]
    bid_fill = mk.min_sell <= bid + 1e-9
    ask_fill = mk.max_buy >= ask - 1e-9
    pnl_bid = np.where(bid_fill, n_sh * (later - bid - UNWIND), 0.0)
    pnl_ask = np.where(ask_fill, n_sh * (ask - later - UNWIND), 0.0)
    reb_bid = 0.2 * mk.fee_rate * n_sh * np.where(bid_fill, bid * (1 - bid), 0)
    reb_ask = 0.2 * mk.fee_rate * n_sh * np.where(ask_fill, ask * (1 - ask), 0)
    valid = (bid > 0.001) & (ask < 0.999)
    return dict(d=d, n=n_sh, reward=np.where(valid, reward, 0.0), pnl=pnl_bid + pnl_ask, rebate=reb_bid + reb_ask,
                filled=bid_fill | ask_fill, valid=valid, pnl_bid=pnl_bid, pnl_ask=pnl_ask, reb_bid=reb_bid, reb_ask=reb_ask,
                bid_fill=bid_fill, ask_fill=ask_fill)


_cache: dict = {}


def run(mk: Market, rules: Rules, split_ts: int) -> dict:
    """回傳前半、後半週各自的獎勵、成交盈虧、返佣、掛單時間比例。"""
    key = (id(mk), rules.dist_frac)
    if key not in _cache:
        _cache[key] = per_minute(mk, rules.dist_frac)
    base = _cache[key]
    alt = None
    if rules.adaptive:
        k2 = (id(mk), rules.adaptive)
        if k2 not in _cache:
            _cache[k2] = per_minute(mk, rules.adaptive)
        alt = _cache[k2]
    n = len(mk.p)
    ts = mk.t0 + 60 * np.arange(n)
    on = base["valid"].copy()
    if rules.ttl_hours is not None and mk.end_ts is not None:
        on &= (mk.end_ts - ts) / 3600 > rules.ttl_hours
    one = np.zeros(n, dtype=bool)
    if rules.vol_window:
        calm = rolling_range(mk.p, rules.vol_window) <= rules.vol_mult * base["d"]
        if rules.one_sided:
            one = ~calm & on          # 這些分鐘改掛單邊
        else:
            on &= calm
    if rules.volu_window:
        recent = trailing_sum(mk.vol, rules.volu_window)
        baseline = trailing_sum(mk.vol, DAY) / DAY * rules.volu_window
        on &= ~((recent > rules.volu_mult * baseline) & (baseline > 0))
    if rules.band:
        on &= (mk.p >= rules.band[0]) & (mk.p <= rules.band[1])
    if rules.big_mult:
        recent_big = pd.Series(mk.big).shift(1).rolling(10, min_periods=1).max().fillna(0).to_numpy()
        trig = recent_big > rules.big_mult * base["n"]
        # 觸發後暫停 30 分鐘
        idx = np.flatnonzero(trig)
        block = np.zeros(n + 31, dtype=int)
        np.add.at(block, idx, 1)
        np.add.at(block, idx + 30, -1)
        on &= np.cumsum(block)[:n] == 0
    if rules.trail_hours:
        shadow = base["reward"] + base["pnl"] + base["rebate"]   # 假設一直掛的每分鐘淨額，只用市場資料就算得出來
        # 第 j 分鐘的成交盈虧要等 H 分鐘後的價格才知道：第 i 分鐘只能看到 j ≤ i − H − 1 的部分（避免偷看未來）
        known = np.concatenate([np.zeros(H + 1), shadow[:-(H + 1)]]) if n > H + 1 else np.zeros(n)
        on &= trailing_sum(known, rules.trail_hours * 60) >= 0
    use_alt = np.zeros(n, dtype=bool)
    if alt is not None:
        use_alt = rolling_range(mk.p, 60) > base["d"]
    src = {k: (np.where(use_alt, alt[k], base[k]) if alt is not None else base[k])
           for k in ("reward", "pnl", "rebate", "filled", "pnl_bid", "pnl_ask", "reb_bid", "reb_ask", "bid_fill", "ask_fill")}
    reward, pnl, rebate, filled = src["reward"].copy(), src["pnl"].copy(), src["rebate"].copy(), src["filled"].copy()
    if one.any():
        up = mk.p - mk.p[np.maximum(np.arange(n) - rules.vol_window, 0)] > 0     # 價格在漲 → 只掛買單
        single_ok = (mk.p >= 0.10) & (mk.p <= 0.90)                               # 單邊只在 10%–90% 才有分
        reward = np.where(one, np.where(single_ok, src["reward"] / 3, 0.0), reward)
        pnl = np.where(one, np.where(up, src["pnl_bid"], src["pnl_ask"]), pnl)
        rebate = np.where(one, np.where(up, src["reb_bid"], src["reb_ask"]), rebate)
        filled = np.where(one, np.where(up, src["bid_fill"], src["ask_fill"]), filled)

    # 跟路徑有關的規則：跳價／被吃後冷卻、單日停損
    if rules.cooldown or rules.stop_loss is not None:
        jump = np.abs(np.diff(mk.p, prepend=mk.p[0])) > base["d"]
        day = (ts // 86400).astype(int)
        quiet_until, cur_day, day_pnl, stopped = -1, -1, 0.0, False
        booked = np.zeros(n + H + 1)   # 第 i 分鐘被吃的盈虧，要到 i + H 才算「知道」
        on2 = on.copy()
        for i in range(n):
            if day[i] != cur_day:
                cur_day, day_pnl, stopped = day[i], 0.0, False
            day_pnl += booked[i]
            if rules.stop_loss is not None and day_pnl < -rules.stop_loss:
                stopped = True
            if rules.cooldown and jump[i]:
                quiet_until = max(quiet_until, i + rules.cooldown)
            if stopped or i < quiet_until or not on[i]:
                on2[i] = False
                continue
            if filled[i]:
                booked[i + H] += pnl[i]
                if rules.cooldown:
                    quiet_until = i + 1 + rules.cooldown
        on = on2

    first = ts < split_ts
    res = {}
    for name, sel in (("first", first), ("second", ~first)):
        m = on & sel
        days = max(sel.sum() / DAY, 1e-9)
        res[name] = dict(reward=reward[m].sum() / days, pnl=pnl[m].sum() / days, rebate=rebate[m].sum() / days,
                         coverage=m.sum() / max(sel.sum(), 1), fills=int(filled[m].sum()))
        res[name]["net"] = res[name]["reward"] + res[name]["pnl"] + res[name]["rebate"]
    return res


def evaluate(markets, rules, split_ts):
    rows = []
    for mk in markets:
        r = run(mk, rules, split_ts)
        for half in ("first", "second"):
            rows.append(dict(q=mk.q, category=mk.category, half=half, **r[half]))
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, half: str) -> dict:
    g = df[df["half"] == half]
    return dict(總淨額=round(g["net"].sum(), 1), 獎勵=round(g["reward"].sum(), 1), 成交盈虧=round(g["pnl"].sum(), 1),
                返佣=round(g["rebate"].sum(), 1), 賺錢市場=f"{int((g['net'] > 0).sum())}/{len(g)}",
                中位數=round(g["net"].median(), 2), 最差=round(g["net"].min(), 1),
                掛單時間=f"{g['coverage'].mean() * 100:.0f}%")


def single_rule_table(markets, grids, base, split, train, test):
    rows = []
    b = evaluate(markets, base, split)
    rows.append(dict(規則="基準（一直掛）", 參數=base.label(), **summarize(b, test)))
    for fam, cands in grids.items():
        scored = [(evaluate(markets, c, split), c) for c in cands]
        df_best, c_best = max(scored, key=lambda x: x[0].query(f"half == '{train}'")["net"].sum())
        rows.append(dict(規則=fam, 參數=c_best.label(), **summarize(df_best, test)))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    markets = load()
    starts = min(mk.t0 for mk in markets)
    ends = max(mk.t0 + 60 * len(mk.p) for mk in markets)
    split = (starts + ends) // 2
    print(f"{len(markets)} 個市場；前半 {pd.to_datetime(starts, unit='s')} ~ {pd.to_datetime(split, unit='s')}，"
          f"後半到 {pd.to_datetime(ends, unit='s')}（每天淨額合計，美元，每個市場 $1,000）\n")
    base = Rules()
    grids = {
        "掛單距離": [replace(base, dist_frac=f) for f in (0.3, 0.5, 0.7, 0.85)],
        "到期前停掛": [replace(base, ttl_hours=h) for h in (0, 12, 24, 48, 72, 120)],
        "波動暫停": [replace(base, vol_window=w, vol_mult=k) for w in (15, 60, 240) for k in (0.5, 1.0, 2.0)],
        "成交量暫停": [replace(base, volu_window=w, volu_mult=k) for w in (10, 30, 60) for k in (3, 5, 10, 20)],
        "跳價／被吃後冷卻": [replace(base, cooldown=c) for c in (15, 60, 240, 720)],
        "單日停損": [replace(base, stop_loss=x) for x in (2, 5, 10, 25, 50)],
        "價格區間": [replace(base, band=b) for b in ((0.03, 0.97), (0.05, 0.95), (0.1, 0.9), (0.2, 0.8))],
        "大單暫停": [replace(base, big_mult=k) for k in (0.5, 1, 3, 10)],
        "波動大改掛遠": [replace(base, adaptive=a) for a in (0.7, 0.85)],
        "過去虧損就暫停": [replace(base, trail_hours=h) for h in (6, 12, 24, 48)],
    }
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 40)
    a = single_rule_table(markets, grids, base, split, "first", "second")
    bb = single_rule_table(markets, grids, base, split, "second", "first")
    both = a[["規則", "參數", "總淨額", "賺錢市場", "最差"]].rename(columns={"參數": "前半選的參數", "總淨額": "→後半淨額", "賺錢市場": "後半賺錢", "最差": "後半最差"})
    both["後半選的參數"] = bb["參數"].to_numpy()
    both["→前半淨額"] = bb["總淨額"].to_numpy()
    both["前半賺錢"] = bb["賺錢市場"].to_numpy()
    both["前半最差"] = bb["最差"].to_numpy()
    print("=== 單一規則，兩個方向的樣本外結果 ===")
    print(both.to_string(index=False))

    combos = {
        "A 掛 0.7v + 單日虧 $25 停": Rules(dist_frac=0.7, stop_loss=25),
        "B = A + 跳價/被吃後冷卻 60 分": Rules(dist_frac=0.7, stop_loss=25, cooldown=60),
        "C = B + 到期前 24h 停": Rules(dist_frac=0.7, stop_loss=25, cooldown=60, ttl_hours=24),
        "D = C + 60 分鐘波動 > 2d 暫停": Rules(dist_frac=0.7, stop_loss=25, cooldown=60, ttl_hours=24, vol_window=60, vol_mult=2.0),
        "E = D + 10 分鐘量 > 5× 暫停": Rules(dist_frac=0.7, stop_loss=25, cooldown=60, ttl_hours=24, vol_window=60, vol_mult=2.0,
                                       volu_window=10, volu_mult=5),
        "F = E + 過去 24h 虧就暫停": Rules(dist_frac=0.7, stop_loss=25, cooldown=60, ttl_hours=24, vol_window=60, vol_mult=2.0,
                                      volu_window=10, volu_mult=5, trail_hours=24),
    }
    rows = []
    for name, r in combos.items():
        df = evaluate(markets, r, split)
        for half, label in (("first", "前半"), ("second", "後半")):
            rows.append(dict(組合=name, 期間=label, **summarize(df, half)))
        df.to_csv(f"mm_combo_{name[0]}.csv", index=False)
    print("\n=== 固定組合（依上表的結果挑選，所以不是乾淨的樣本外），兩個半週 ===")
    print(pd.DataFrame(rows).to_string(index=False))
