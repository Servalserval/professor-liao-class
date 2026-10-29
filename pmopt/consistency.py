"""Polymarket 內部一致性：不用期權，只拿 Polymarket 自己的兩組市場互相比。

兩組市場用同一個結算來源（Binance BTC/USDT 的 1 分鐘 K 線）：
- 到期價格盤 above(K, t)：t 那根 K 線的「收盤價」> K 才算 Yes
- 觸價盤 touch_down(B)：窗口內任何一根 K 線的「最低價」≤ B 就算 Yes；touch_up(B) 看「最高價」≥ B

只要 t 落在觸價窗口內，就有不需要任何模型的「支配關係」：
- 收盤價 ≤ B ⇒ 那根 K 線的最低價 ≤ B ⇒ touch_down(B) 一定是 Yes。
  所以「touch_down(B) Yes + above(K) Yes」在 K ≤ B 時，不管發生什麼都至少付 $1。
- 收盤價 > K ≥ B ⇒ 那根 K 線的最高價 ≥ B ⇒ touch_up(B) 一定是 Yes。
  所以「touch_up(B) Yes + above(K) No」在 K ≥ B 時至少付 $1。
同一組市場內的單調性也一樣：
- K1 < K2：「above(K1) Yes + above(K2) No」至少付 $1。
- B1 < B2：「touch_up(B1) Yes + touch_up(B2) No」至少付 $1；touch_down 方向相反。

這種組合如果用賣價買進的成本 < $1，就是無風險套利（還沒算資金成本）。

履約價沒有剛好對上時（例如觸價盤是 77,500，到期盤只有 76,000 和 78,000），
組合會有一個「缺口」：touch_down(77,500) + above(78,000)，如果收盤價落在 (77,500, 78,000]
而且整個窗口都沒碰到 77,500，兩邊都付 $0。這就不是無風險，而是「近似套利」，
要用模型估計掉進缺口的機率（gap_probability）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from scipy.integrate import quad

from .parse import parse_question
from .polymarket import parse_time
from .pricing import digital_call_flat, norm_pdf, one_touch
from .scanner import ScanConfig, _fee
from .surface import VolSurface, year_frac


def gap_probability(S: float, B: float, lo: float, hi: float, T1: float, T2: float, sigma: float) -> float:
    """P( lo < S_T1 ≤ hi，而且 [0, T2] 整段都沒有碰到 B )。

    對數常態、forward 測度（漂移 −σ²/2）、連續監看。B 在 S 下方時缺口要在 B 上方，反之亦然。

    令 X_t = ln(S_t/S)，b = ln(B/S)，μ = −σ²/2。在 [0, T1] 沒碰到 b、而且 X_T1 落在 x 的機率密度
    （反射原理，有漂移的版本）：

        g(x) = [ φ((x − μT1)/s) − exp(2μb/σ²) · φ((x − 2b − μT1)/s) ] / s，  s = σ√T1

    之後 (T1, T2] 這段也不能碰到：乘上 1 − one_touch(S·e^x, B, T2 − T1, σ)，再對缺口積分。
    """
    if sigma <= 0 or T1 <= 0 or hi <= lo:
        return 0.0
    down = B < S
    if (down and lo < B) or (not down and hi > B):
        raise ValueError("缺口必須在觸價價位的另一側（B 在下方時 lo ≥ B；B 在上方時 hi ≤ B）")
    b, mu, s = math.log(B / S), -0.5 * sigma * sigma, sigma * math.sqrt(T1)
    refl = math.exp(2 * mu * b / (sigma * sigma))

    def integrand(x: float) -> float:
        g = (norm_pdf((x - mu * T1) / s) - refl * norm_pdf((x - 2 * b - mu * T1) / s)) / s
        later = 1.0 - one_touch(S * math.exp(x), B, T2 - T1, sigma) if T2 > T1 else 1.0
        return max(g, 0.0) * later

    val, _ = quad(integrand, math.log(lo / S), math.log(hi / S), limit=200)
    return min(max(val, 0.0), 1.0)


@dataclass
class _Mkt:
    label: str
    kind: str
    strike: float
    end: datetime
    bid: float | None
    ask: float | None


def _markets(markets: pd.DataFrame, asset: str) -> list[_Mkt]:
    out = []
    for m in markets.itertuples(index=False):
        if getattr(m, "closed", False):
            continue
        spec = parse_question(m.question)
        if spec is None or spec.asset != asset or spec.kind not in ("above", "touch_up", "touch_down"):
            continue
        bid = None if pd.isna(m.best_bid) else float(m.best_bid)
        ask = None if pd.isna(m.best_ask) else float(m.best_ask)
        out.append(_Mkt(m.question, spec.kind, spec.strike, parse_time(m.end_date), bid, ask))
    return out


def _yes(m: _Mkt) -> float | None:
    """買 Yes 的價格 = Yes 的賣價。"""
    return m.ask


def _no(m: _Mkt) -> float | None:
    """買 No 的價格 = 1 − Yes 的買價（Polymarket 的 Yes / No 共用同一本訂單簿）。"""
    return None if m.bid is None else 1.0 - m.bid


def _short(kind: str, strike: float) -> str:
    return {"above": f"高於 {strike:,.0f}", "touch_up": f"觸及 ↑{strike:,.0f}",
            "touch_down": f"觸及 ↓{strike:,.0f}"}[kind]


def find_pairs(markets: pd.DataFrame, now: datetime, cfg: ScanConfig | None = None) -> pd.DataFrame:
    """列出所有「至少付 $1」或「只在缺口裡付 $0」的兩腳組合，以及用可成交價買進的成本。

    回傳欄位：
    - relation   ：哪一種關係（到期盤單調 / 觸價盤單調 / 觸價 vs 到期）
    - pair       ：要買的兩腳（Yes 或 No），p1、p2 是各自的買進價格
    - cost       ：兩腳的買進成本（美元），cost_fee 是加上 taker 手續費模型之後
    - slack      ：1 − cost_fee。無模型組合 slack > 0 就是套利
    - gap_lo/hi  ：缺口（空的代表無模型）
    """
    cfg = cfg or ScanConfig()
    ms = [m for m in _markets(markets, cfg.asset) if m.end > now]
    rows = []

    def add(relation, m1, side1, p1, m2, side2, p2, gap=(None, None), touch=None, above=None):
        if p1 is None or p2 is None:
            return
        cost = p1 + p2
        fee = _fee(p1, cfg.taker_fee_coeff) + _fee(p2, cfg.taker_fee_coeff)
        rows.append(dict(
            relation=relation,
            pair=f"{_short(m1.kind, m1.strike)} {side1} + {_short(m2.kind, m2.strike)} {side2}",
            p1=p1, p2=p2,
            model_free=gap[0] is None,
            gap_lo=gap[0], gap_hi=gap[1],
            cost=round(cost, 4), cost_fee=round(cost + fee, 4), slack=round(1.0 - cost - fee, 4),
            # 給模型用的資訊（近似套利才需要）
            _touch_kind=touch.kind if touch else None, _B=touch.strike if touch else None,
            _T_touch=year_frac(now, touch.end) if touch else None,
            _K=above.strike if above else None, _T_above=year_frac(now, above.end) if above else None,
            _above_side=side2 if above else None,
        ))

    # 1) 同一組市場內的單調性
    for kind, relation in (("above", "到期盤單調"), ("touch_up", "觸價盤單調"), ("touch_down", "觸價盤單調")):
        by_end: dict[datetime, list[_Mkt]] = {}
        for m in ms:
            if m.kind == kind:
                by_end.setdefault(m.end, []).append(m)
        for group in by_end.values():
            # 依「比較容易成立」排序：above 與 touch_up 是低履約價較容易，touch_down 是高履約價較容易
            group.sort(key=lambda m: m.strike, reverse=(kind == "touch_down"))
            for i, easy in enumerate(group):
                for hard in group[i + 1:]:
                    add(relation, easy, "Yes", _yes(easy), hard, "No", _no(hard))

    # 2) 觸價盤 vs 到期盤：到期時間要落在觸價窗口內
    aboves = [m for m in ms if m.kind == "above"]
    for t in (m for m in ms if m.kind in ("touch_up", "touch_down")):
        cands = [a for a in aboves if a.end <= t.end]
        if t.kind == "touch_down":
            # touch_down(B) Yes + above(K) Yes：K ≤ B 無模型；K > B 有缺口 (B, K]
            safe = [a for a in cands if a.strike <= t.strike]
            risky = [a for a in cands if a.strike > t.strike]
            for a in (max(safe, key=lambda a: a.strike, default=None),
                      min(risky, key=lambda a: a.strike, default=None)):
                if a is not None:
                    gap = (None, None) if a.strike <= t.strike else (t.strike, a.strike)
                    add("觸價 vs 到期", t, "Yes", _yes(t), a, "Yes", _yes(a), gap, touch=t, above=a)
        else:
            # touch_up(B) Yes + above(K) No：K ≥ B 無模型；K < B 有缺口 (K, B)
            safe = [a for a in cands if a.strike >= t.strike]
            risky = [a for a in cands if a.strike < t.strike]
            for a in (min(safe, key=lambda a: a.strike, default=None),
                      max(risky, key=lambda a: a.strike, default=None)):
                if a is not None:
                    gap = (None, None) if a.strike >= t.strike else (a.strike, t.strike)
                    add("觸價 vs 到期", t, "Yes", _yes(t), a, "No", _no(a), gap, touch=t, above=a)

    cols = ["relation", "pair", "p1", "p2", "model_free", "gap_lo", "gap_hi", "cost", "cost_fee", "slack"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows).sort_values("slack", ascending=False).reset_index(drop=True)


def evaluate_near_arbs(pairs: pd.DataFrame, surface: VolSurface, sigma: float | None = None) -> pd.DataFrame:
    """對「觸價 vs 到期」的組合，用單一波動率的對數常態模型估計：

    - p_gap ：掉進缺口、兩腳都付 $0 的機率（全部虧掉 cost_fee）
    - ev    ：期望報酬 = P(第一腳付) + P(第二腳付) − cost_fee（兩腳都付時拿 $2）

    sigma 不給的話，用曲面在觸價價位、觸價窗口結束時的波動率。
    現貨用 surface.spot；比較 Polymarket 自己的兩組市場時，建議傳入「中心對齊」後的曲面。
    """
    out = pairs[pairs["relation"] == "觸價 vs 到期"].copy()
    if out.empty:
        return out
    S = surface.spot
    res = []
    for r in out.to_dict("records"):  # itertuples 會把底線開頭的欄位改名，所以用 dict
        B, K, T2, T1 = r["_B"], r["_K"], r["_T_touch"], r["_T_above"]
        vol = sigma if sigma is not None else surface.vol(B, T2)
        p_touch = one_touch(S, B, T2, vol)
        p_above = digital_call_flat(S, K, T1, vol)
        p_leg2 = p_above if r["_above_side"] == "Yes" else 1.0 - p_above
        p_gap = 0.0 if r["model_free"] else gap_probability(S, B, r["gap_lo"], r["gap_hi"], T1, T2, vol)
        res.append(dict(sigma=round(vol * 100, 1), p_touch=round(p_touch, 4), p_leg2=round(p_leg2, 4),
                        p_gap=round(p_gap, 4), ev=round(p_touch + p_leg2 - r["cost_fee"], 4)))
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(res)], axis=1)


def public_columns(df: pd.DataFrame) -> pd.DataFrame:
    """去掉內部用的欄位（底線開頭），方便印出與存檔。"""
    return df[[c for c in df.columns if not c.startswith("_")]]
