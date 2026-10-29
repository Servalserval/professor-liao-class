import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from pmopt import history as H
from pmopt.__main__ import main
from pmopt.pricing import digital_call_flat


def test_settle_time_handles_daylight_saving():
    # 2026 年美國 11/1 結束夏令時間：10/31 中午 = 16:00 UTC，11/2 中午 = 17:00 UTC
    assert H.settle_time(date(2026, 10, 31)) == datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    assert H.settle_time(date(2026, 11, 2)) == datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)


def test_daily_slugs():
    assert H.daily_slugs(date(2026, 10, 3)) == ["bitcoin-above-on-october-3-2026", "bitcoin-above-on-october-3"]


@pytest.mark.parametrize("prices, closed, expected", [
    ('["1", "0"]', True, 1),
    ('["0", "1"]', True, 0),
    ('["0.5", "0.5"]', True, None),   # 取消或 50/50
    ('["0.62", "0.38"]', False, None),  # 還沒結算
    (None, True, None),
])
def test_outcome_parsing(prices, closed, expected):
    assert H._outcome({"outcomePrices": prices, "closed": closed}) == expected


def test_price_at_takes_last_point_before_time():
    hist = [{"t": 100, "p": 0.3}, {"t": 400, "p": 0.5}, {"t": 200, "p": 0.4}]
    assert H.price_at(hist, 300) == (0.4, (300 - 200) / 60)
    assert H.price_at(hist, 50) == (None, None)


def test_clustered_se_counts_days_not_rows():
    # 同一天的 10 筆完全一樣：群組標準誤應該等於「以天為單位」的標準誤，而不是除以 √(筆數)
    per_day = pd.Series([0.1, -0.3, 0.2, 0.0, 0.4])
    x = per_day.repeat(10).reset_index(drop=True)
    days = pd.Series(range(5)).repeat(10).reset_index(drop=True)
    m, se = H.clustered_mean_se(x, days)
    assert m == pytest.approx(per_day.mean())
    assert se == pytest.approx(per_day.std(ddof=1) / math.sqrt(5), rel=1e-9)


# ---------- 用假的 API 回應跑完整流程 ----------

DAYS = [date(2026, 10, 26), date(2026, 10, 27)]
STRIKES = [76_000, 78_000, 80_000]


def _spot(ms: int) -> float:
    """假的 BTC 價格：隨時間緩慢上升。"""
    return 77_000 + (ms / 1000 - 1_792_000_000) / 3600 * 10


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeAPI:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/events"):
            slug = params["slug"]
            for d in DAYS:
                if slug == H.daily_slugs(d)[0]:
                    settle_close = _spot(int(H.settle_time(d).timestamp() * 1000))
                    markets = [dict(
                        question=f"Will the price of Bitcoin be above ${k:,} on {d:%B} {d.day}?",
                        closed=True, clobTokenIds=json.dumps([f"{d}-{k}-yes", f"{d}-{k}-no"]),
                        outcomePrices=json.dumps(["1", "0"] if settle_close > k else ["0", "1"]),
                    ) for k in STRIKES]
                    return _Resp([dict(slug=slug, markets=markets)])
            return _Resp([])
        if url.endswith("/prices-history"):
            if params["fidelity"] == 10 and params["market"].endswith("76000-yes"):
                return _Resp({"history": []})  # 測試退回較粗的粒度
            start, end, step = params["startTs"], params["endTs"], params["fidelity"] * 60
            return _Resp({"history": [{"t": t, "p": 0.5} for t in range(start, end + 1, step)]})
        if url.endswith("/klines"):
            ms = params["startTime"]
            c = _spot(ms)
            return _Resp([[ms, str(c), str(c), str(c), str(c), "1", ms + 59_999]])
        if url.endswith("/get_volatility_index_data"):
            s, e = params["start_timestamp"], params["end_timestamp"]
            hour = 3_600_000
            data = [[t, 40, 40, 40, 40.0] for t in range((s // hour + 1) * hour, e + 1, hour)]
            return _Resp({"result": {"data": data, "continuation": None}})
        raise AssertionError(url)


def test_collect_history_end_to_end(tmp_path):
    api = _FakeAPI()
    f = H.Fetcher(session=api, cache_dir=tmp_path / "cache", pause=0)
    df = H.collect_history(DAYS[0], DAYS[1], (24.0, 6.0), fetcher=f)
    assert len(df) == len(DAYS) * len(STRIKES) * 2
    assert df["pm_price"].eq(0.5).all() and df["dvol"].eq(40.0).all()
    assert set(df.loc[df["strike"] == 76_000, "fidelity_min"]) == {60}
    assert df["rule_check"].astype(bool).all()  # 我們自己用 K 線算的結果和「結算」一致
    # 現貨取的是「結算前 h 小時」那一分鐘
    r = df[(df["day"] == "2026-10-26") & (df["horizon_h"] == 24.0)].iloc[0]
    t = H.settle_time(DAYS[0]) - timedelta(hours=24)
    assert r["spot"] == pytest.approx(_spot(int(t.timestamp() * 1000)))
    assert r["model_prob"] == pytest.approx(digital_call_flat(r["spot"], r["strike"], 24 / 8760, 0.40))
    # 第二次跑完全走快取，不再呼叫 API
    n_calls = len(api.calls)
    H.collect_history(DAYS[0], DAYS[1], (24.0, 6.0), fetcher=H.Fetcher(session=api, cache_dir=tmp_path / "cache"))
    assert len(api.calls) == n_calls


# ---------- 合成世界：分析能不能抓到「尾部被高估」 ----------

def _synthetic(n_days=300, overprice=0.04, seed=3):
    """真實世界的機率 = 模型機率；Polymarket 在尾部（< 25% 或 > 75%）往 50% 方向偏 overprice。"""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        spot, vol, h = 80_000.0, 0.45, 24.0
        T = h / 8760
        z = rng.standard_normal()
        s_T = spot * math.exp(-0.5 * vol**2 * T + vol * math.sqrt(T) * z)  # 同一天所有履約價共用一條路徑
        for k in range(74_000, 86_001, 2_000):
            p = digital_call_flat(spot, k, T, vol)
            pm = p + overprice if p < 0.25 else (p - overprice if p > 0.75 else p)
            rows.append(dict(day=f"d{d}", strike=k, horizon_h=h, pm_price=min(max(pm, 0.001), 0.999),
                             price_age_min=5.0, spot=spot, dvol=vol * 100, outcome=int(s_T > k)))
    return H.add_model(pd.DataFrame(rows))


def test_analysis_detects_tail_overpricing():
    df = H.usable(_synthetic())
    sc = H.scores(df).iloc[0]
    assert sc["brier_diff"] > 2 * sc["brier_diff_se"]   # Polymarket 明顯比較不準
    cal = H.calibration_table(df).set_index("bucket")
    low = cal.iloc[1]                                    # 價格 3%–10% 那一組
    assert low["pm_minus_realized"] > 0                  # 報價高於實際發生頻率
    st = H.strategy(df, threshold=0.03, half_spread=0.0)
    assert (st["mean_pnl"] > 0).all()


def test_analysis_finds_nothing_when_market_is_calibrated():
    df = H.usable(_synthetic(overprice=0.0))
    sc = H.scores(df).iloc[0]
    assert abs(sc["brier_diff"]) < 1e-12
    assert H.strategy(df, threshold=0.03).empty


def test_cli_backtest_from_csv(tmp_path, capsys):
    csv = tmp_path / "history.csv"
    _synthetic(n_days=60).to_csv(csv, index=False)
    assert main(["backtest", "--from-csv", str(csv), "--out", str(tmp_path / "bt")]) == 0
    out = capsys.readouterr().out
    assert "brier_diff" in out and "依 Polymarket 價格分組" in out
    assert (tmp_path / "bt" / "24h" / "reliability.png").exists()
