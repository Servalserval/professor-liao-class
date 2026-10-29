import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pmopt.consistency import evaluate_near_arbs, find_pairs, gap_probability, public_columns
from pmopt.polymarket import load_snapshot
from pmopt.pricing import one_touch
from pmopt.scanner import ScanConfig
from pmopt.surface import VolSurface

SNAP = Path(__file__).resolve().parents[1] / "data" / "snapshot_2026-10-29"
NOW = datetime(2026, 10, 29, 5, 59, 14, tzinfo=timezone.utc)


def _mc_gap(S, B, lo, hi, T1, T2, sigma, n_paths=200_000, n_steps=240, seed=11):
    """Monte Carlo：P(lo < S_T1 ≤ hi，且 [0, T2] 都沒碰到 B)。

    每一步用布朗橋算「兩個離散點之間偷偷碰到 B」的機率，用存活權重相乘，比直接抽樣精準。
    """
    rng = np.random.default_rng(seed)
    dt = T2 / n_steps
    i1 = round(T1 / dt)
    b = math.log(B / S)
    x = np.zeros(n_paths)
    alive = np.ones(n_paths)
    in_band = None
    for i in range(1, n_steps + 1):
        x_new = x - 0.5 * sigma**2 * dt + sigma * math.sqrt(dt) * rng.standard_normal(n_paths)
        crossed = (x_new <= b) if B < S else (x_new >= b)
        p_bridge = np.exp(-2 * np.clip((b - x) * (b - x_new), 0, None) / (sigma**2 * dt))
        alive *= np.where(crossed, 0.0, 1.0 - p_bridge)
        x = x_new
        if i == i1:
            s_t1 = S * np.exp(x)
            in_band = (s_t1 > lo) & (s_t1 <= hi)
    return float((alive * in_band).mean())


@pytest.mark.parametrize("S, B, lo, hi", [
    (80_000.0, 77_500.0, 77_500.0, 78_000.0),   # 向下觸價，缺口在觸價價位上方
    (80_000.0, 77_500.0, 77_500.0, 80_000.0),   # 比較寬的缺口
    (80_000.0, 77_500.0, 77_500.0, 200_000.0),  # 涵蓋 B 上方所有價格：等於 [0, T2] 整段都沒碰到的機率
    (80_000.0, 87_500.0, 86_000.0, 87_500.0),   # 向上觸價，缺口在觸價價位下方
])
def test_gap_probability_matches_monte_carlo(S, B, lo, hi):
    T1, T2, sigma = 58 / 8760, 70 / 8760, 0.35
    analytic = gap_probability(S, B, lo, hi, T1, T2, sigma)
    mc = _mc_gap(S, B, lo, hi, T1, T2, sigma)
    assert analytic == pytest.approx(mc, rel=0.01, abs=4e-4)


def test_gap_over_whole_survival_region_is_no_touch_probability():
    """缺口如果涵蓋 B 另一側的所有價格、而且 T2 = T1，就等於「整段沒碰到 B」的機率。"""
    S, B, T, sigma = 80_000.0, 76_000.0, 3 / 365, 0.4
    p = gap_probability(S, B, B, S * 20, T, T, sigma)
    assert p == pytest.approx(1 - one_touch(S, B, T, sigma), abs=1e-6)


def test_gap_probability_rejects_gap_on_wrong_side():
    with pytest.raises(ValueError):
        gap_probability(80_000.0, 77_500.0, 77_000.0, 78_000.0, 0.01, 0.01, 0.4)


def _mk(question, end, bid, ask):
    return dict(event_slug="x", question=question, end_date=end, best_bid=bid, best_ask=ask, closed=False)


DAY = "2026-10-31T16:00:00Z"
MONTH_END = "2026-11-01T04:00:00Z"


def test_monotonicity_violation_is_detected():
    # above 80k 的賣價 0.40 比 above 82k 的買價 0.45 還低：買 80k Yes + 買 82k No 只要 0.95，至少拿回 $1
    m = pd.DataFrame([
        _mk("Will the price of Bitcoin be above $80,000 on October 31?", DAY, 0.38, 0.40),
        _mk("Will the price of Bitcoin be above $82,000 on October 31?", DAY, 0.45, 0.47),
    ])
    p = find_pairs(m, NOW)
    top = p.iloc[0]
    assert top["model_free"] and top["relation"] == "到期盤單調"
    assert top["cost"] == pytest.approx(0.40 + 0.55)
    assert top["slack"] == pytest.approx(0.05)


def test_touch_vs_terminal_pairs_and_gaps():
    m = pd.DataFrame([
        _mk("Will Bitcoin dip to $77,500 in October?", MONTH_END, 0.08, 0.09),
        _mk("Will the price of Bitcoin be above $76,000 on October 31?", DAY, 0.945, 0.965),
        _mk("Will the price of Bitcoin be above $78,000 on October 31?", DAY, 0.84, 0.87),
        # 到期時間在觸價窗口之後，不能配對
        _mk("Will the price of Bitcoin be above $78,000 on November 2?", "2026-11-02T17:00:00Z", 0.5, 0.6),
    ])
    p = find_pairs(m, NOW).query("relation == '觸價 vs 到期'").set_index("pair")
    assert len(p) == 2
    safe = p.loc["觸及 ↓77,500 Yes + 高於 76,000 Yes"]
    risky = p.loc["觸及 ↓77,500 Yes + 高於 78,000 Yes"]
    assert safe["model_free"] and safe["cost"] == pytest.approx(1.055)
    assert not risky["model_free"] and (risky["gap_lo"], risky["gap_hi"]) == (77_500, 78_000)
    assert risky["slack"] == pytest.approx(0.04)


def test_touch_up_dominance_direction():
    """向上觸價：收盤 > K ≥ B 才保證碰到 B，所以 K ≥ B 是無模型、K < B 才有缺口 (K, B)。"""
    m = pd.DataFrame([
        _mk("Will Bitcoin reach $87,500 in October?", MONTH_END, 0.033, 0.035),
        _mk("Will the price of Bitcoin be above $86,000 on October 31?", DAY, 0.041, 0.061),
        _mk("Will the price of Bitcoin be above $88,000 on October 31?", DAY, 0.014, 0.023),
    ])
    p = find_pairs(m, NOW).query("relation == '觸價 vs 到期'").set_index("pair")
    safe = p.loc["觸及 ↑87,500 Yes + 高於 88,000 No"]
    risky = p.loc["觸及 ↑87,500 Yes + 高於 86,000 No"]
    assert safe["model_free"] and safe["cost"] == pytest.approx(0.035 + (1 - 0.014))
    assert not risky["model_free"] and (risky["gap_lo"], risky["gap_hi"]) == (86_000, 87_500)


def test_fees_reduce_slack():
    m = load_snapshot(SNAP / "polymarket_markets.csv")
    a = find_pairs(m, NOW, ScanConfig(taker_fee_coeff=0.0)).set_index("pair")["slack"]
    b = find_pairs(m, NOW, ScanConfig(taker_fee_coeff=0.07)).set_index("pair")["slack"]
    assert (b <= a.loc[b.index] + 1e-12).all()


@pytest.fixture(scope="module")
def snapshot_pairs():
    surf = VolSurface.from_deribit(pd.read_csv(SNAP / "deribit_btc_options.csv"), NOW)
    markets = load_snapshot(SNAP / "polymarket_markets.csv")
    return surf, find_pairs(markets, NOW)


def test_snapshot_has_no_model_free_arbitrage(snapshot_pairs):
    """快照裡所有無模型的關係都守住了（成本 ≥ $1，頂多剛好等於）。"""
    _, pairs = snapshot_pairs
    assert (pairs.loc[pairs["model_free"], "slack"] <= 1e-9).all()


def test_snapshot_best_near_arb(snapshot_pairs):
    surf, pairs = snapshot_pairs
    ev = evaluate_near_arbs(pairs, surf).set_index("pair")
    r = ev.loc["觸及 ↓77,500 Yes + 高於 78,000 Yes"]
    assert r["cost"] == pytest.approx(0.96)
    assert 0 < r["p_gap"] < 0.01          # 兩腳都歸零的機率很小
    assert r["ev"] > 0.05                 # 而且期望值為正
    assert not any(c.startswith("_") for c in public_columns(ev.reset_index()).columns)
