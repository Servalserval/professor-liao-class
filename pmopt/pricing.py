"""定價核心：把「期權的波動率」換算成「預測市場的機率」。

這裡的公式都假設：
- 價格服從對數常態（Black-Scholes 世界），
- 幾天內的利率與資金成本可以忽略（不折現），
- 以 forward（遠期價）當作中心。

預測市場的三種常見題型對應到三種「二元期權」：
- 「X 日某時刻收盤價高於 K？」  → 歐式數位買權（cash-or-nothing call）
- 「X 日某時刻價格介於 L 和 H？」→ 兩個數位買權相減
- 「這段期間內會不會碰到 B？」  → 觸價期權（one-touch）
"""

from __future__ import annotations

import math
from typing import Callable

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    s = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    return d1, d1 - s


def bs_call(F: float, K: float, T: float, sigma: float) -> float:
    """不折現的 Black 買權價格（以 forward F 為底）。"""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0)
    d1, d2 = _d1_d2(F, K, T, sigma)
    return F * norm_cdf(d1) - K * norm_cdf(d2)


def digital_call_flat(F: float, K: float, T: float, sigma: float) -> float:
    """P(S_T > K)，假設整條微笑曲線是平的（只用一個波動率）。"""
    if T <= 0 or sigma <= 0:
        return 1.0 if F > K else 0.0
    _, d2 = _d1_d2(F, K, T, sigma)
    return norm_cdf(d2)


def digital_call_smile(
    F: float,
    K: float,
    T: float,
    vol_of_strike: Callable[[float], float],
    rel_bump: float = 1e-4,
) -> float:
    """P(S_T > K)，有考慮波動率微笑（skew）。

    數位買權 = 買權價格對履約價的負微分：D(K) = -dC/dK。
    當波動率隨履約價變動時，C(K) = BS(K, σ(K))，所以不能只用 N(d2)，
    而要把 σ(K) 的斜率也算進去。這裡直接用中央差分，讓 skew 自然被包含：

        D(K) ≈ -[C(K+h, σ(K+h)) - C(K-h, σ(K-h))] / (2h)
    """
    if T <= 0:
        return 1.0 if F > K else 0.0
    h = K * rel_bump
    c_up = bs_call(F, K + h, T, vol_of_strike(K + h))
    c_dn = bs_call(F, K - h, T, vol_of_strike(K - h))
    p = -(c_up - c_dn) / (2.0 * h)
    return min(max(p, 0.0), 1.0)


def digital_call_smile_analytic(
    F: float, K: float, T: float, sigma: float, dsigma_dK: float
) -> float:
    """解析版：D(K) = N(d2) - vega(K) * dσ/dK。用來驗證差分版本。"""
    d1, d2 = _d1_d2(F, K, T, sigma)
    vega = F * norm_pdf(d1) * math.sqrt(T)  # 不折現的 vega（σ 以 1.00 = 100% 計）
    return norm_cdf(d2) - vega * dsigma_dK


def one_touch(S: float, B: float, T: float, sigma: float) -> float:
    """在 [0, T] 期間內「至少碰到一次」B 的機率（連續監看、零漂移的 forward 測度）。

    令 X_t = ln(S_t/S_0) = -σ²t/2 + σW_t，b = ln(B/S_0)。
    向上觸價（b > 0）的經典結果（反射原理）：

        P = N((-b - σ²T/2)/(σ√T)) + (S_0/B) · N((-b + σ²T/2)/(σ√T))

    向下觸價（b < 0）用對稱的形式：

        P = N((b + σ²T/2)/(σ√T)) + (S_0/B) · N((b - σ²T/2)/(σ√T))

    直覺檢查：漂移很小時，P ≈ 2 × P(S_T 超過 B)，也就是常聽到的「觸價機率約是到期機率的兩倍」。
    """
    if B == S:
        return 1.0
    if T <= 0 or sigma <= 0:
        return 0.0
    b = math.log(B / S)
    s = sigma * math.sqrt(T)
    half_var = 0.5 * sigma * sigma * T
    ratio = S / B
    if b > 0:
        p = norm_cdf((-b - half_var) / s) + ratio * norm_cdf((-b + half_var) / s)
    else:
        p = norm_cdf((b + half_var) / s) + ratio * norm_cdf((b - half_var) / s)
    return min(max(p, 0.0), 1.0)


def implied_vol_digital(prob: float, F: float, K: float, T: float) -> float | None:
    """由「高於 K 的機率」反推平坦波動率。

    注意：價外（K > F）數位買權的價格對 σ 不是單調的——σ 從 0 增加時機率先上升，
    超過 σ√T = sqrt(2|ln(F/K)|) 之後反而下降。所以只在單調的那一段找解。
    """
    if T <= 0:
        return None
    hi = 5.0
    if K > F:
        hi = min(hi, math.sqrt(2.0 * abs(math.log(F / K))) / math.sqrt(T))
    return implied_vol_from_prob(prob, lambda s: digital_call_flat(F, K, T, s), lo=0.01, hi=hi)


def implied_vol_from_prob(
    prob: float,
    pricer: Callable[[float], float],
    lo: float = 0.01,
    hi: float = 5.0,
    tol: float = 1e-7,
) -> float | None:
    """反推：給定一個機率（例如 Polymarket 的 mid），找出讓 pricer(σ) 等於它的 σ。

    pricer 必須對 σ 單調（數位期權在價外時遞增、價內時遞減，觸價期權一定遞增）。
    找不到解就回傳 None。
    """
    f_lo, f_hi = pricer(lo) - prob, pricer(hi) - prob
    if f_lo == 0:
        return lo
    if f_lo * f_hi > 0:
        return None
    a, b = lo, hi
    for _ in range(200):
        m = 0.5 * (a + b)
        f_m = pricer(m) - prob
        if abs(f_m) < tol or (b - a) < tol:
            return m
        if f_lo * f_m < 0:
            b = m
        else:
            a, f_lo = m, f_m
    return 0.5 * (a + b)
