from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pmopt.surface import VolSurface, parse_instrument, year_frac

SNAP = Path(__file__).resolve().parents[1] / "data" / "snapshot_2026-10-29"
NOW = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)


def test_parse_instrument():
    inst = parse_instrument("BTC-31OCT26-80000-C")
    assert inst.asset == "BTC"
    assert inst.expiry == datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
    assert inst.strike == 80_000 and inst.cp == "C"
    assert parse_instrument("BTC-PERPETUAL") is None
    assert parse_instrument("ETH-1NOV26-2350-P").strike == 2350


def _flat_chain(vol_by_expiry: dict[str, float], F: float = 80_000.0) -> pd.DataFrame:
    rows = []
    for exp, v in vol_by_expiry.items():
        for K in range(70_000, 90_001, 2_000):
            cp = "C" if K >= F else "P"
            rows.append(dict(instrument_name=f"BTC-{exp}-{K}-{cp}", mark_iv=v * 100, underlying_price=F))
    return pd.DataFrame(rows)


@pytest.mark.parametrize("method", ["svi", "pchip"])
def test_flat_surface_returns_flat_vol(method):
    surf = VolSurface.from_deribit(_flat_chain({"31OCT26": 0.5, "1NOV26": 0.5}), NOW, smile_method=method)
    for K in (72_000, 80_000, 88_000):
        for hours in (6, 40, 60, 200):
            assert surf.vol(K, hours / 8760) == pytest.approx(0.5, rel=1e-4)


T1 = year_frac(NOW, datetime(2026, 10, 31, 8, tzinfo=timezone.utc))
T2 = year_frac(NOW, datetime(2026, 11, 1, 8, tzinfo=timezone.utc))


def test_total_variance_interpolates_linearly_in_time():
    surf = VolSurface.from_deribit(_flat_chain({"31OCT26": 0.30, "1NOV26": 0.40}), NOW)
    tm = 0.5 * (T1 + T2)
    expected_w = 0.5 * (0.30**2 * T1 + 0.40**2 * T2)
    assert surf.total_variance(0.0, tm) == pytest.approx(expected_w, rel=1e-4)


def test_total_variance_never_decreases_even_with_calendar_arbitrage():
    # 刻意餵一組「後面到期日總變異數反而比較小」的不合理資料（日曆套利）
    surf = VolSurface.from_deribit(_flat_chain({"31OCT26": 0.40, "1NOV26": 0.30}), NOW)
    assert 0.40**2 * T1 > 0.30**2 * T2  # 確認輸入真的不合理
    ws = [surf.total_variance(0.0, T1 + i * (T2 - T1) / 10) for i in range(11)]
    assert all(a <= b + 1e-15 for a, b in zip(ws, ws[1:]))


def test_expired_options_are_ignored():
    df = _flat_chain({"28OCT26": 0.9, "31OCT26": 0.4, "1NOV26": 0.4})
    surf = VolSurface.from_deribit(df, NOW)
    assert len(surf.smiles) == 2


def test_clamped_placeholder_vols_are_dropped():
    # 正常的微笑曲線 + 三個深度價外、數值完全相同的填充值
    df = pd.DataFrame([dict(instrument_name=f"BTC-31OCT26-{K}-{'C' if K >= 80_000 else 'P'}",
                            mark_iv=100 * (0.35 + 2.0 * (K / 80_000 - 1) ** 2), underlying_price=80_000.0)
                       for K in range(70_000, 90_001, 2_000)])
    extra = pd.DataFrame([dict(instrument_name=f"BTC-31OCT26-{K}-P", mark_iv=113.6, underlying_price=80_000.0)
                          for K in (60_000, 62_000, 64_000)])
    surf = VolSurface.from_deribit(pd.concat([df, extra]), NOW)
    assert surf.smiles[0].k.min() > -0.2


def _real_surface():
    return VolSurface.from_deribit(pd.read_csv(SNAP / "deribit_btc_options.csv"), NOW)


def test_real_snapshot_builds_reasonable_surface():
    surf = _real_surface()
    desc = surf.describe()
    assert len(desc) >= 3
    assert desc["atm_vol"].between(15, 80).all()
    # 下檔波動率比較高（加密貨幣短天期常見的 put skew）
    T = 58 / 8760
    assert surf.vol(76_000, T) > surf.vol(80_500, T)


def test_real_snapshot_gives_monotone_probabilities():
    """「收盤價高於 K」的機率一定要隨 K 遞減，否則代表曲面有蝴蝶套利（機率密度為負）。"""
    from pmopt.pricing import digital_call_smile

    surf = _real_surface()
    for hours in (30, 58, 70, 120):
        T = hours / 8760
        F = surf.forward(T)
        ps = [digital_call_smile(F, K, T, lambda x: surf.vol(x, T)) for K in range(60_000, 100_001, 250)]
        assert all(b <= a + 1e-7 for a, b in zip(ps, ps[1:])), f"T={hours}h 的機率不是單調遞減"
