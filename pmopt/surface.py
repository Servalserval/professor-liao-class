"""從 Deribit 期權報價建立波動率曲面（volatility surface）。

步驟：
1. 解析商品名稱，例如 BTC-31OCT26-80000-C → 到期 2026-10-31 08:00 UTC、履約價 80000、買權。
2. 每個到期日各自整理一條「微笑曲線」：x 軸是 log-moneyness k = ln(K/F)，y 軸是隱含波動率，
   再用 SVI 參數化擬合成一條平滑曲線（資料點太少時改用逐點內插）。
3. 任意時間點 T 的波動率，用「總變異數」w = σ²T 在兩個相鄰到期日之間線性內插。
   這是業界常見做法，好處是 w 對時間單調，內插後不會出現不合理的時間價值。
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares

SECONDS_PER_YEAR = 365.0 * 24 * 3600
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_INSTRUMENT_RE = re.compile(r"^(?P<asset>[A-Z]+)-(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})-(?P<strike>\d+(?:d\d+)?)-(?P<cp>[CP])$")


@dataclass(frozen=True)
class Instrument:
    asset: str
    expiry: datetime  # Deribit 期權在到期日 08:00 UTC 結算
    strike: float
    cp: str  # "C" 或 "P"


def parse_instrument(name: str) -> Instrument | None:
    m = _INSTRUMENT_RE.match(name.strip())
    if not m:
        return None
    expiry = datetime(2000 + int(m["yy"]), _MONTHS[m["mon"]], int(m["day"]), 8, 0, tzinfo=timezone.utc)
    strike = float(m["strike"].replace("d", "."))
    return Instrument(m["asset"], expiry, strike, m["cp"])


def year_frac(start: datetime, end: datetime) -> float:
    """加密貨幣 24/7 交易，直接用日曆時間（秒）換算成年。"""
    return (end - start).total_seconds() / SECONDS_PER_YEAR


def svi_total_variance(params, k):
    """SVI（Stochastic Volatility Inspired）參數化：業界最常用的微笑曲線形狀。

        w(k) = a + b · ( ρ(k - m) + sqrt((k - m)² + s²) )

    a 控制整體高度、b 控制兩翼斜率、ρ 控制左右不對稱（skew）、m 平移、s 控制底部圓滑程度。
    好處是兩翼會線性延伸（不會像平坦外插那樣在資料邊界突然轉折），
    算出來的機率曲線比較不會出現「越價外機率反而越高」的不合理情況。
    """
    a, b, rho, m, s = params
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s ** 2))


def fit_svi(k: np.ndarray, vol: np.ndarray, T: float) -> tuple[np.ndarray, float]:
    """用最小平方法把 SVI 擬合到市場的隱含波動率；回傳 (參數, 誤差 RMSE，單位：波動率百分點)。"""
    w = vol ** 2 * T
    p0 = [max(w.min() * 0.5, 1e-8), max((w.max() - w.min()) / (np.ptp(k) + 1e-9), 1e-4), -0.2, 0.0, 0.05]
    lb = [-w.max(), 1e-6, -0.999, -0.5, 1e-4]
    ub = [w.max(), 10.0, 0.999, 0.5, 2.0]

    def resid(p):
        return (np.sqrt(np.maximum(svi_total_variance(p, k), 1e-12) / T) - vol) * 100

    r = least_squares(resid, p0, bounds=(lb, ub))
    return r.x, float(np.sqrt(np.mean(r.fun ** 2)))


@dataclass
class Smile:
    expiry: datetime
    T: float          # 距估值時間的年數
    forward: float
    k: np.ndarray     # log-moneyness 節點
    vol: np.ndarray   # 對應的隱含波動率（1.00 = 100%）
    method: str = "svi"  # "svi" 或 "pchip"（逐點內插，平坦外插）

    def __post_init__(self) -> None:
        self.fit_rmse = 0.0
        if self.method == "svi" and len(self.k) >= 5:
            self._svi, self.fit_rmse = fit_svi(self.k, self.vol, self.T)
        else:
            self.method = "pchip"
            self._interp = PchipInterpolator(self.k, self.vol, extrapolate=False)

    def vol_at_k(self, k: float) -> float:
        if self.method == "svi":
            return float(np.sqrt(max(svi_total_variance(self._svi, k), 1e-12) / self.T))
        # 超出資料範圍時用端點的值（平坦外插），避免外插出離譜的數字
        kk = min(max(k, float(self.k[0])), float(self.k[-1]))
        return float(self._interp(kk))


class VolSurface:
    """把多條 Smile 串成一個可以查詢任意 (K, T) 的曲面。"""

    def __init__(self, smiles: list[Smile], valuation_time: datetime, spot: float | None = None):
        if not smiles:
            raise ValueError("沒有可用的到期日，無法建立波動率曲面")
        self.smiles = sorted(smiles, key=lambda s: s.T)
        self.valuation_time = valuation_time
        self.spot = spot if spot is not None else self.smiles[0].forward

    # ---------- 建構 ----------
    @classmethod
    def from_deribit(
        cls,
        df: pd.DataFrame,
        valuation_time: datetime,
        max_abs_k: float = 0.35,
        min_nodes: int = 3,
        spot: float | None = None,
        smile_method: str = "svi",
    ) -> "VolSurface":
        """df 需要欄位：instrument_name, mark_iv（百分比）, underlying_price。

        清理規則：
        - 只保留 |ln(K/F)| <= max_abs_k 的履約價（太遠的價外期權幾乎沒有成交，報價不可靠）。
        - 同一個到期日裡，若同一個 mark_iv 數值出現在 3 個以上的履約價，視為交易所的
          填充值（沒有報價時給的固定上限），整組丟掉。
        """
        rows = []
        for r in df.itertuples(index=False):
            inst = parse_instrument(r.instrument_name)
            if inst is None or pd.isna(r.mark_iv) or r.mark_iv <= 0:
                continue
            if inst.expiry <= valuation_time:
                continue
            rows.append(dict(expiry=inst.expiry, strike=inst.strike, cp=inst.cp,
                             iv=float(r.mark_iv) / 100.0, fwd=float(r.underlying_price)))
        if not rows:
            raise ValueError("Deribit 資料裡沒有尚未到期的期權")
        data = pd.DataFrame(rows)

        smiles: list[Smile] = []
        for expiry, g in data.groupby("expiry"):
            F = float(g["fwd"].median())
            # 同一個履約價如果買權、賣權都有，優先用價外（OTM）那一個：
            # K > F 用買權、K < F 用賣權，因為價外期權的報價比較能反映波動率。
            # 只有價內的那一個也照用（Deribit 的 mark_iv 本來就是同一條微笑曲線）。
            g = g.assign(otm=((g["cp"] == "C") & (g["strike"] >= F)) | ((g["cp"] == "P") & (g["strike"] <= F)))
            picked = []
            for K, gk in g.groupby("strike"):
                use = gk[gk["otm"]] if gk["otm"].any() else gk
                picked.append((float(K), float(use["iv"].mean())))
            nodes = pd.DataFrame(picked, columns=["strike", "iv"])
            nodes["k"] = np.log(nodes["strike"] / F)
            rounded = nodes["iv"].round(6)
            vc = rounded.value_counts()
            clamped = rounded.map(vc).ge(3) if len(vc) > 1 else pd.Series(False, index=nodes.index)
            nodes = nodes[~clamped & (nodes["k"].abs() <= max_abs_k)].sort_values("k")
            if len(nodes) < min_nodes:
                continue
            smiles.append(Smile(expiry=expiry, T=year_frac(valuation_time, expiry), forward=F,
                                k=nodes["k"].to_numpy(), vol=nodes["iv"].to_numpy(), method=smile_method))
        return cls(smiles, valuation_time, spot=spot)

    # ---------- 查詢 ----------
    def forward(self, T: float) -> float:
        Ts = [s.T for s in self.smiles]
        Fs = [s.forward for s in self.smiles]
        if T <= Ts[0]:
            return Fs[0]
        if T >= Ts[-1]:
            return Fs[-1]
        return float(np.interp(T, Ts, Fs))

    def _variance_nodes(self, k: float) -> tuple[list[float], list[float]]:
        """每個到期日在 log-moneyness k 的總變異數 w_i = σ_i(k)² · T_i。

        總變異數不應隨時間下降（否則存在日曆套利）。如果資料不合理，
        就讓後面的節點至少等於前一個（取累積最大值）。
        """
        Ts, ws = [], []
        for s in self.smiles:
            w = s.vol_at_k(k) ** 2 * s.T
            ws.append(max(w, ws[-1]) if ws else w)
            Ts.append(s.T)
        return Ts, ws

    def total_variance(self, k: float, T: float) -> float:
        Ts, ws = self._variance_nodes(k)
        if T <= Ts[0]:
            return ws[0] * T / Ts[0]      # 第一個到期日之前：沿用它的波動率
        if T >= Ts[-1]:
            return ws[-1] * T / Ts[-1]    # 最後一個到期日之後：沿用它的波動率
        return float(np.interp(T, Ts, ws))

    def vol(self, K: float, T: float) -> float:
        if T <= 0:
            return self.smiles[0].vol_at_k(math.log(K / self.smiles[0].forward))
        k = math.log(K / self.forward(T))
        return math.sqrt(max(self.total_variance(k, T), 1e-12) / T)

    def shifted(self, factor: float) -> "VolSurface":
        """把現貨和所有 forward 乘上 factor，微笑曲線（以 log-moneyness 表示）保持不變。

        用途：兩份快照時間不同步時，用來模擬「如果期權那邊的中心價格跟著移動」會怎樣。
        """
        smiles = []
        for s in self.smiles:
            c = copy.copy(s)          # 微笑曲線的參數不變，只改 forward
            c.forward = s.forward * factor
            smiles.append(c)
        return VolSurface(smiles, self.valuation_time, spot=self.spot * factor)

    def describe(self) -> pd.DataFrame:
        """每個到期日的摘要，方便在課堂上展示。"""
        out = []
        for s in self.smiles:
            out.append(dict(
                expiry=s.expiry.strftime("%Y-%m-%d %H:%M UTC"),
                hours_left=round(s.T * SECONDS_PER_YEAR / 3600, 1),
                forward=round(s.forward, 2),
                n_strikes=len(s.k),
                atm_vol=round(s.vol_at_k(0.0) * 100, 2),
                smile=s.method,
                fit_rmse_volpts=round(s.fit_rmse, 2),
            ))
        return pd.DataFrame(out)
