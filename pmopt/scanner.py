"""掃描器：每個 Polymarket 市場算一個「期權隱含的公允機率」，再和可成交價比較。

名詞：
- fair         ：由 Deribit 波動率曲面算出來的機率（= Yes 股的公允價格，單位美元）
- bid / ask    ：Polymarket 上 Yes 股的最佳買價 / 賣價
- edge_buy_yes ：fair - ask   → 用 ask 買進 Yes，平均每股期望賺多少
- edge_sell_yes：bid - fair   → 用 bid 賣出 Yes（等同買 No），平均每股期望賺多少
- pm_iv        ：把 Polymarket 的中價當成答案，反推它「隱含」的波動率

edge 只是「相對於期權市場的錯價」，不是無風險利潤：還沒扣避險成本，
而且假設期權市場是對的。詳見 docs/method.md。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .parse import parse_question
from .polymarket import parse_time
from .pricing import (
    digital_call_smile,
    implied_vol_digital,
    implied_vol_from_prob,
    one_touch,
)
from .surface import SECONDS_PER_YEAR, VolSurface, year_frac


@dataclass
class ScanConfig:
    asset: str = "BTC"
    # Polymarket 以 Binance BTC/USDT 結算，Deribit 以美元指數計價。
    # 1 USDT 值多少美元；若 USDT 略低於 1 美元，BTC/USDT 的報價會略高。
    usdt_usd: float = 1.0
    # Polymarket 加密貨幣盤的 taker 手續費模型（假設）：每股 fee = coeff × p × (1 - p)。
    # 形狀符合官方說明「50% 時最高、越接近 0 或 1 越低」，實際費率請查市場的 feeSchedule。
    taker_fee_coeff: float = 0.0
    # edge 超過這個門檻才標示訊號（美元／股）
    min_edge: float = 0.01


def _fee(p: float | None, coeff: float) -> float:
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return 0.0
    return coeff * p * (1.0 - p)


def _label(kind: str, strike: float, strike_hi: float | None) -> str:
    return {
        "above": f"高於 {strike:,.0f}",
        "below": f"低於 {strike:,.0f}",
        "range": f"介於 {strike:,.0f}–{(strike_hi or 0):,.0f}",
        "touch_up": f"觸及 ↑{strike:,.0f}",
        "touch_down": f"觸及 ↓{strike:,.0f}",
    }[kind]


def fair_probability(spec, T: float, surface: VolSurface, usdt_usd: float = 1.0) -> tuple[float, float]:
    """回傳 (公允機率, 使用的 Deribit 波動率)。"""
    F = surface.forward(T)
    K = spec.strike * usdt_usd
    vol = lambda x: surface.vol(x, T)  # noqa: E731
    if spec.kind == "above":
        return digital_call_smile(F, K, T, vol), vol(K)
    if spec.kind == "below":
        return 1.0 - digital_call_smile(F, K, T, vol), vol(K)
    if spec.kind == "range":
        H = spec.strike_hi * usdt_usd
        p = digital_call_smile(F, K, T, vol) - digital_call_smile(F, H, T, vol)
        return max(p, 0.0), vol(math.sqrt(K * H))
    if spec.kind in ("touch_up", "touch_down"):
        S = surface.spot
        if (spec.kind == "touch_up" and S >= K) or (spec.kind == "touch_down" and S <= K):
            return 1.0, vol(K)
        # 觸價期權對整條微笑都敏感；這裡用「觸價價位、到期時間」的波動率當近似
        return one_touch(S, K, T, vol(K)), vol(K)
    raise ValueError(spec.kind)


def pm_implied_vol(spec, prob: float, T: float, surface: VolSurface, usdt_usd: float = 1.0) -> float | None:
    if prob is None or not (0.0 < prob < 1.0):
        return None
    F, K = surface.forward(T), spec.strike * usdt_usd
    if spec.kind == "above":
        return implied_vol_digital(prob, F, K, T)
    if spec.kind == "below":
        return implied_vol_digital(1.0 - prob, F, K, T)
    if spec.kind in ("touch_up", "touch_down"):
        S = surface.spot
        return implied_vol_from_prob(prob, lambda s: one_touch(S, K, T, s))
    return None


def fit_center(markets: pd.DataFrame, surface: VolSurface, now: datetime, cfg: ScanConfig | None = None,
               band: tuple[float, float] = (0.05, 0.95)) -> float | None:
    """找出 Polymarket「到期價格盤」隱含的中心價格，回傳相對於期權 forward 的倍數。

    做法：把期權曲面整體平移 factor 倍，找出讓 fair 和 Polymarket 中價最接近（最小平方）的 factor。
    只用中價落在 band 內的市場，因為接近 0 或 1 的市場對中心價格幾乎沒有資訊。

    為什麼需要這個？如果兩份快照不同步、或其中一邊報價過時，所有市場都會往同一個方向偏，
    看起來像是「到處都有錯價」，其實只是中心價格不一樣。先把中心對齊，剩下的差異
    才比較可能是真正的「形狀」差異（例如 Polymarket 把尾部機率定得比較高）。
    """
    from scipy.optimize import minimize_scalar

    cfg = cfg or ScanConfig()
    pts = []
    for m in markets.itertuples(index=False):
        if getattr(m, "closed", False) or pd.isna(m.best_bid) or pd.isna(m.best_ask):
            continue
        spec = parse_question(m.question)
        if spec is None or spec.asset != cfg.asset or spec.kind not in ("above", "below"):
            continue
        mid = (float(m.best_bid) + float(m.best_ask)) / 2
        if not band[0] <= mid <= band[1]:
            continue
        T = year_frac(now, parse_time(m.end_date))
        if T > 0:
            pts.append((spec, T, mid))
    if len(pts) < 2:
        return None

    def sse(factor: float) -> float:
        s = surface.shifted(factor)
        return sum((fair_probability(spec, T, s, cfg.usdt_usd)[0] - mid) ** 2 for spec, T, mid in pts)

    res = minimize_scalar(sse, bounds=(0.9, 1.1), method="bounded", options={"xatol": 1e-6})
    return float(res.x)


def scan(markets: pd.DataFrame, surface: VolSurface, now: datetime, cfg: ScanConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ScanConfig()
    out = []
    for m in markets.itertuples(index=False):
        if getattr(m, "closed", False):
            continue
        spec = parse_question(m.question)
        if spec is None or spec.asset != cfg.asset:
            continue
        end = parse_time(m.end_date)
        T = year_frac(now, end)
        if T <= 0:
            continue
        fair, dvol = fair_probability(spec, T, surface, cfg.usdt_usd)
        bid = None if pd.isna(m.best_bid) else float(m.best_bid)
        ask = None if pd.isna(m.best_ask) else float(m.best_ask)
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
        e_buy = fair - ask - _fee(ask, cfg.taker_fee_coeff) if ask is not None else float("nan")
        e_sell = bid - fair - _fee(bid, cfg.taker_fee_coeff) if bid is not None else float("nan")
        best = max([x for x in (e_buy, e_sell) if not math.isnan(x)], default=float("nan"))
        if not math.isnan(best) and best >= cfg.min_edge:
            signal = "買 Yes" if best == e_buy else "賣 Yes（=買 No）"
        else:
            signal = ""
        piv = pm_implied_vol(spec, mid, T, surface, cfg.usdt_usd) if mid is not None else None
        out.append(dict(
            event_slug=getattr(m, "event_slug", ""),
            market=_label(spec.kind, spec.strike, spec.strike_hi),
            kind=spec.kind,
            strike=spec.strike,
            end_utc=end.strftime("%m-%d %H:%M"),
            hours_left=round(T * SECONDS_PER_YEAR / 3600, 1),
            deribit_vol=round(dvol * 100, 1),
            pm_iv=round(piv * 100, 1) if piv else None,
            fair=round(fair, 4),
            bid=bid,
            ask=ask,
            edge_buy_yes=round(e_buy, 4),
            edge_sell_yes=round(e_sell, 4),
            best_edge=round(best, 4),
            signal=signal,
        ))
    return pd.DataFrame(out)
