"""歷史回測：已經結算的 Polymarket 每日「高於 K」盤，價格準不準？

問題：結算前 h 小時，Polymarket 報 p；事後真的發生的頻率是多少？
跟一個最簡單的期權模型（Deribit DVOL 當波動率、對數常態）比，誰比較準？

這是回答「期權那邊是對的嗎？」最直接的辦法：不用假設任何一邊是真相，直接看結果。

資料來源（都是公開 API，不需要帳號）：
- Polymarket Gamma：GET /events?slug=bitcoin-above-on-<月>-<日>[-<年>]
  已結算的市場 outcomePrices 會是 '["1", "0"]'（Yes 贏）或 '["0", "1"]'（No 贏）
- Polymarket CLOB：GET /prices-history?market=<Yes token>&startTs=&endTs=&fidelity=<分鐘>
  回傳 {"history": [{"t": 秒, "p": 價格}, ...]}
- Binance：GET /api/v3/klines?symbol=BTCUSDT&interval=1m&startTime=<毫秒>&limit=1
  回傳 [[開盤時間, 開, 高, 低, 收, ...]]；拿來取當時的現貨，並自己驗證結算結果
- Deribit：GET /public/get_volatility_index_data?currency=BTC&resolution=3600
  回傳 {"result": {"data": [[毫秒, 開, 高, 低, 收], ...], "continuation": ...}}

⚠ 寫這個模組時，開發環境連不到上述 API。格式依官方文件撰寫，並用模擬回應測試；
  第一次實際執行請加 --verbose，確認每一步真的拿到資料。

方法上要注意的地方：
- 同一天的各個履約價看的是同一條價格路徑，結果高度相關。算標準誤時要以「天」為群組
  （cluster），不然會嚴重高估顯著性。
- prices-history 給的是成交／中價的時間序列，不是可成交的買賣價；策略報酬要另外扣價差。
- DVOL 是 30 天的隱含波動率，拿來算 1 天內的機率只是近似（週末、事件前後會有落差）。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from .deribit import BASE as DERIBIT
from .parse import parse_question
from .polymarket import CLOB, ET, GAMMA, _MONTH_NAMES, _parse_token_ids
from .pricing import digital_call_flat

BINANCE_BASES = ["https://api.binance.com", "https://data-api.binance.vision"]  # 後者是官方的公開行情鏡像
HOURS_PER_YEAR = 365.0 * 24


class Fetcher:
    """帶快取的 GET：同一個網址＋參數只抓一次，存成 JSON 檔，重跑時不用再連網。"""

    def __init__(self, session: requests.Session | None = None, cache_dir: Path | None = None,
                 pause: float = 0.15, verbose: bool = False):
        self.s = session or requests.Session()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.pause = pause
        self.verbose = verbose
        self._mem: dict[str, object] = {}
        self.dead_bases: set[str] = set()  # 回過 451／403 的網址（例如 api.binance.com 擋美國 IP），之後跳過

    def get_json(self, url: str, params: dict):
        key = hashlib.sha1(json.dumps([url, sorted(params.items())], default=str).encode()).hexdigest()
        if key in self._mem:
            return self._mem[key]
        path = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            r = self.s.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            if self.verbose:
                print(f"  GET {url} {params} → {str(data)[:120]}")
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(self.pause)
        self._mem[key] = data
        return data


def settle_time(day: date) -> datetime:
    """每日盤的結算時刻：美東時間中午 12:00，換成 UTC（自動處理夏令時間）。

    規則原文是「the Binance 1 minute candle for BTC/USDT closing at 12:00 in the ET timezone」，
    也就是 11:59 開盤、12:00 收盤的那根。Binance 的 K 線以開盤時間標示，所以要往前一分鐘取。
    """
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=ET).astimezone(timezone.utc)


def daily_slugs(day: date, asset_name: str = "bitcoin") -> list[str]:
    base = f"{asset_name}-above-on-{_MONTH_NAMES[day.month - 1]}-{day.day}"
    return [f"{base}-{day.year}", base]


def _outcome(m: dict) -> int | None:
    """已結算：Yes 贏回傳 1、No 贏回傳 0；還沒結算（或被取消、50/50）回傳 None。"""
    raw = m.get("outcomePrices")
    try:
        prices = [float(x) for x in (json.loads(raw) if isinstance(raw, str) else raw)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(prices) < 2 or not m.get("closed", False):
        return None
    if prices[0] >= 0.99 and prices[1] <= 0.01:
        return 1
    if prices[0] <= 0.01 and prices[1] >= 0.99:
        return 0
    return None


def resolved_markets(f: Fetcher, day: date, asset: str = "BTC") -> list[dict]:
    name = {"BTC": "bitcoin", "ETH": "ethereum"}[asset]
    for slug in daily_slugs(day, name):
        try:
            events = f.get_json(f"{GAMMA}/events", {"slug": slug})
        except requests.RequestException:
            continue
        rows = []
        for ev in events or []:
            # 沒有年份的 slug 可能是前一年的同一天（命名在 2026 年中才加上年份），要核對結算日期
            end = ev.get("endDate")
            if not end or datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(ET).date() != day:
                continue
            for m in ev.get("markets", []):
                spec = parse_question(m.get("question", ""))
                if spec is None or spec.kind != "above" or spec.asset != asset:
                    continue
                out = _outcome(m)
                tokens = _parse_token_ids(m.get("clobTokenIds"))
                if out is None or not tokens:
                    continue
                rows.append(dict(day=day, slug=slug, strike=spec.strike, token_yes=tokens[0], outcome=out))
        if rows:
            return rows
    return []


def price_at(history: list[dict], ts: int) -> tuple[float | None, float | None]:
    """時間 ts（秒）之前最後一筆價格，以及它離 ts 多久（分鐘）。"""
    best = None
    for pt in history:
        t = int(pt["t"])
        if t <= ts and (best is None or t > best[0]):
            best = (t, float(pt["p"]))
    if best is None:
        return None, None
    return best[1], (ts - best[0]) / 60.0


def price_history(f: Fetcher, token: str, start_ts: int, end_ts: int,
                  fidelities: tuple[int, ...] = (10, 60, 720)) -> tuple[list[dict], int | None]:
    """依序試不同的時間粒度（分鐘）。已結算的市場有時細粒度會回傳空的，就退回比較粗的。"""
    for fid in fidelities:
        try:
            data = f.get_json(f"{CLOB}/prices-history",
                              {"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": fid})
        except requests.RequestException:
            continue
        hist = (data or {}).get("history") or []
        if hist:
            return hist, fid
    return [], None


def binance_close(f: Fetcher, when: datetime, symbol: str = "BTCUSDT") -> float | None:
    """從 when 開始的那根 1 分鐘 K 線的收盤價。"""
    ms = int(when.timestamp() * 1000)
    for base in BINANCE_BASES:
        if base in f.dead_bases:
            continue
        try:
            rows = f.get_json(f"{base}/api/v3/klines",
                              {"symbol": symbol, "interval": "1m", "startTime": ms, "limit": 1})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (403, 451):
                f.dead_bases.add(base)
            continue
        except requests.RequestException:
            continue
        if rows and int(rows[0][0]) == ms:
            return float(rows[0][4])
    return None


def dvol_series(f: Fetcher, start: datetime, end: datetime, currency: str = "BTC") -> pd.Series:
    """Deribit DVOL（年化 %），每小時一筆，index 是 UTC 秒。"""
    out: dict[int, float] = {}
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    while True:
        data = f.get_json(f"{DERIBIT}/get_volatility_index_data",
                          {"currency": currency, "start_timestamp": s_ms, "end_timestamp": e_ms, "resolution": "3600"})
        res = (data or {}).get("result") or {}
        for row in res.get("data") or []:
            out[int(row[0]) // 1000] = float(row[4])
        cont = res.get("continuation")
        # continuation 是下一頁的結束時間（往回翻）；沒有或沒前進就停
        if not cont or int(cont) <= s_ms or int(cont) >= e_ms:
            break
        e_ms = int(cont)
    return pd.Series(out, dtype=float).sort_index()


def _asof(series: pd.Series, ts: int) -> float | None:
    s = series[series.index <= ts]
    return None if s.empty else float(s.iloc[-1])


def collect_history(start: date, end: date, horizons_h: tuple[float, ...] = (24.0,), asset: str = "BTC",
                    fetcher: Fetcher | None = None) -> pd.DataFrame:
    """每一天、每個履約價、每個「結算前 h 小時」記一筆：Polymarket 價格、現貨、DVOL、結果。"""
    f = fetcher or Fetcher()
    span = timedelta(hours=max(horizons_h) + 3)
    try:
        dvol = dvol_series(f, settle_time(start) - span, settle_time(end), asset)
    except requests.RequestException as e:  # 不讓整個回測中斷：Polymarket 的價格與結果照樣存下來
        print(f"  [deribit] 抓不到 DVOL：{e}")
        dvol = pd.Series(dtype=float)
    rows = []
    day = start
    while day <= end:
        markets = resolved_markets(f, day, asset)
        settle = settle_time(day)
        if markets:
            # 規則寫的是「收在 12:00」那根（11:59 開盤）；12:00 開盤那根也記下來，用實際結算判斷哪一根才對
            settle_close = binance_close(f, settle - timedelta(minutes=1), f"{asset}USDT")
            settle_close_alt = binance_close(f, settle, f"{asset}USDT")
            spots = {h: binance_close(f, settle - timedelta(hours=h), f"{asset}USDT") for h in horizons_h}
            for m in markets:
                hist, fid = price_history(f, m["token_yes"], int((settle - span).timestamp()), int(settle.timestamp()))
                for h in horizons_h:
                    t = settle - timedelta(hours=h)
                    p, age = price_at(hist, int(t.timestamp()))
                    rows.append(dict(
                        day=day.isoformat(), strike=m["strike"], horizon_h=h, pm_price=p, price_age_min=age,
                        fidelity_min=fid, spot=spots[h], dvol=_asof(dvol, int(t.timestamp())),
                        outcome=m["outcome"], binance_settle_close=settle_close,
                        binance_settle_close_alt=settle_close_alt,
                    ))
        elif f.verbose:
            print(f"  {day}：找不到已結算的每日盤")
        day += timedelta(days=1)
    df = pd.DataFrame(rows)
    return add_model(df) if not df.empty else df


def add_model(df: pd.DataFrame, vol_scale: float = 1.0) -> pd.DataFrame:
    """加上模型機率，以及「我們用 Binance 自己算的結果」和 Polymarket 結算是否一致。"""
    df = df.copy()
    ok = df["spot"].notna() & df["dvol"].notna()
    df["model_prob"] = np.nan
    df.loc[ok, "model_prob"] = [
        digital_call_flat(s, k, h / HOURS_PER_YEAR, v / 100.0 * vol_scale)
        for s, k, h, v in zip(df.loc[ok, "spot"], df.loc[ok, "strike"], df.loc[ok, "horizon_h"], df.loc[ok, "dvol"])
    ]
    for col, name in (("binance_settle_close", "rule_check"), ("binance_settle_close_alt", "rule_check_alt")):
        if col in df:
            check = df[col].notna()
            df[name] = pd.NA
            df.loc[check, name] = (df.loc[check, col] > df.loc[check, "strike"]).astype(int) == df.loc[check, "outcome"]
    return df


# ---------- 分析 ----------

def clustered_mean_se(x: pd.Series, cluster: pd.Series) -> tuple[float, float]:
    """平均值，以及以 cluster（這裡是「天」）為群組的標準誤。

    同一天的市場結果高度相關，把它們當成獨立樣本會讓標準誤小得離譜。
    """
    x = pd.Series(x, dtype=float).reset_index(drop=True)
    cluster = pd.Series(cluster).reset_index(drop=True)
    n = len(x)
    if n == 0:
        return float("nan"), float("nan")
    mean = float(x.mean())
    sums = (x - mean).groupby(cluster).sum()
    g = len(sums)
    if g < 2:
        return mean, float("nan")
    se = math.sqrt(float((sums ** 2).sum()) * g / (g - 1)) / n
    return mean, se


def usable(df: pd.DataFrame, max_age_min: float = 180.0) -> pd.DataFrame:
    """只留下有價格、價格不太舊、模型也算得出來的樣本。"""
    return df[df["pm_price"].notna() & (df["price_age_min"] <= max_age_min) & df["model_prob"].notna()].copy()


def scores(df: pd.DataFrame) -> pd.DataFrame:
    """Brier score（越小越準）與 log loss，Polymarket vs 模型，依 horizon 分開。"""
    out = []
    for h, g in df.groupby("horizon_h"):
        y = g["outcome"].astype(float)
        row = dict(horizon_h=h, n=len(g), n_days=g["day"].nunique())
        for name, col in (("pm", "pm_price"), ("model", "model_prob")):
            p = g[col].clip(1e-4, 1 - 1e-4)
            row[f"brier_{name}"] = round(float(((p - y) ** 2).mean()), 5)
            row[f"logloss_{name}"] = round(float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), 5)
        # 兩者 Brier 差異的群組標準誤：差異 > 0 代表 Polymarket 比較不準
        diff = (g["pm_price"] - y) ** 2 - (g["model_prob"] - y) ** 2
        m, se = clustered_mean_se(diff, g["day"])
        row["brier_diff"], row["brier_diff_se"] = round(m, 5), round(se, 5)
        out.append(row)
    return pd.DataFrame(out)


DEFAULT_BINS = (0.0, 0.03, 0.1, 0.25, 0.5, 0.75, 0.9, 0.97, 1.0)


def calibration_table(df: pd.DataFrame, by: str = "pm_price", bins=DEFAULT_BINS) -> pd.DataFrame:
    """依價格分組：平均報價、平均模型機率、實際發生頻率，以及「報價 − 實際」的群組標準誤。"""
    d = df.assign(bucket=pd.cut(df[by], list(bins), include_lowest=True))
    out = []
    for b, g in d.groupby("bucket", observed=True):
        bias, se = clustered_mean_se(g["pm_price"] - g["outcome"], g["day"])
        _, realized_se = clustered_mean_se(g["outcome"], g["day"])
        out.append(dict(bucket=str(b), n=len(g), n_days=g["day"].nunique(),
                        pm_price=round(g["pm_price"].mean(), 4), model_prob=round(g["model_prob"].mean(), 4),
                        realized=round(g["outcome"].mean(), 4), realized_se=round(realized_se, 4),
                        pm_minus_realized=round(bias, 4), se=round(se, 4)))
    return pd.DataFrame(out)


def strategy(df: pd.DataFrame, threshold: float = 0.05, half_spread: float = 0.01,
             fee_coeff: float = 0.0) -> pd.DataFrame:
    """很單純的規則：Polymarket 比模型貴超過門檻就賣 Yes，便宜超過門檻就買 Yes，抱到結算。

    每股報酬：賣 Yes = p − 結果，買 Yes = 結果 − p，再扣 half_spread（prices-history 不是可成交價）
    和 taker 手續費 fee_coeff × p × (1 − p)（官方公式；賣 Yes 等同用 1 − p 買 No，費用相同）。
    """
    gap = df["pm_price"] - df["model_prob"]
    sell, buy = gap > threshold, gap < -threshold
    cost = half_spread + fee_coeff * df["pm_price"] * (1 - df["pm_price"])
    pnl = pd.Series(np.nan, index=df.index)
    pnl[sell] = df.loc[sell, "pm_price"] - df.loc[sell, "outcome"] - cost[sell]
    pnl[buy] = df.loc[buy, "outcome"] - df.loc[buy, "pm_price"] - cost[buy]
    trades = df.assign(side=np.where(sell, "賣 Yes", np.where(buy, "買 Yes", "")), pnl=pnl)[sell | buy]
    out = []
    for (h, side), g in trades.groupby(["horizon_h", "side"]):
        m, se = clustered_mean_se(g["pnl"], g["day"])
        out.append(dict(horizon_h=h, side=side, n=len(g), n_days=g["day"].nunique(),
                        mean_pnl=round(m, 4), se=round(se, 4), t_stat=round(m / se, 2) if se and se > 0 else None))
    return pd.DataFrame(out)
