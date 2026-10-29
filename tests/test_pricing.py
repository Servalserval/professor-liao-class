import math

import numpy as np
import pytest

from pmopt.pricing import (
    bs_call,
    digital_call_flat,
    digital_call_smile,
    digital_call_smile_analytic,
    implied_vol_digital,
    implied_vol_from_prob,
    one_touch,
)


def test_digital_is_minus_derivative_of_call():
    F, K, T, s = 80_000.0, 82_000.0, 3 / 365, 0.45
    h = 1.0
    fd = -(bs_call(F, K + h, T, s) - bs_call(F, K - h, T, s)) / (2 * h)
    assert digital_call_flat(F, K, T, s) == pytest.approx(fd, abs=1e-6)


def test_smile_digital_matches_analytic_formula():
    F, K, T = 80_000.0, 84_000.0, 2.5 / 365
    a, b = 0.10, 3e-6  # σ(K) = a + bK，往右上升的微笑
    vol = lambda k: a + b * k  # noqa: E731
    fd = digital_call_smile(F, K, T, vol)
    an = digital_call_smile_analytic(F, K, T, vol(K), b)
    assert fd == pytest.approx(an, abs=2e-5)
    # 微笑往右上升時，數位買權應該比「平坦波動率」版本便宜
    assert fd < digital_call_flat(F, K, T, vol(K))


def test_digital_monotone_in_strike():
    F, T, s = 80_000.0, 5 / 365, 0.5
    ps = [digital_call_flat(F, K, T, s) for K in range(60_000, 100_001, 2_000)]
    assert all(x >= y for x, y in zip(ps, ps[1:]))
    assert ps[0] > 0.99 and ps[-1] < 0.01


def _mc_touch(S, B, T, sigma, n_paths=40_000, n_steps=400, seed=7):
    """用 Monte Carlo + 布朗橋修正估計觸價機率（零漂移的 forward 測度）。"""
    rng = np.random.default_rng(seed)
    dt = T / n_steps
    x = np.full(n_paths, math.log(S))
    hit = np.zeros(n_paths, dtype=bool)
    b = math.log(B)
    up = B > S
    for _ in range(n_steps):
        x_new = x - 0.5 * sigma**2 * dt + sigma * math.sqrt(dt) * rng.standard_normal(n_paths)
        crossed = (x_new >= b) if up else (x_new <= b)
        # 布朗橋：兩個離散點之間「偷偷穿越再回來」的機率
        d1, d2 = (b - x), (b - x_new)
        p_bridge = np.exp(-2 * np.clip(d1 * d2, 0, None) / (sigma**2 * dt))
        sneaky = rng.random(n_paths) < p_bridge
        hit |= crossed | sneaky
        x = x_new
    return hit.mean()


@pytest.mark.parametrize("B", [84_000.0, 77_500.0])
def test_one_touch_matches_monte_carlo(B):
    S, T, s = 80_000.0, 3 / 365, 0.40
    mc = _mc_touch(S, B, T, s)
    assert one_touch(S, B, T, s) == pytest.approx(mc, abs=0.01)


def test_one_touch_is_about_twice_the_digital():
    S, T, s = 80_000.0, 2 / 365, 0.35
    B = 82_000.0
    touch = one_touch(S, B, T, s)
    terminal = digital_call_flat(S, B, T, s)
    assert touch == pytest.approx(2 * terminal, rel=0.05)


def test_one_touch_edges():
    assert one_touch(80_000, 80_000, 1 / 365, 0.4) == 1.0
    assert one_touch(80_000, 90_000, 0.0, 0.4) == 0.0
    near, far = one_touch(80_000, 81_000, 1 / 365, 0.4), one_touch(80_000, 85_000, 1 / 365, 0.4)
    assert near > far


@pytest.mark.parametrize("K", [74_000.0, 86_000.0])
@pytest.mark.parametrize("true_vol", [0.25, 0.6, 1.2])
def test_implied_vol_roundtrip(K, true_vol):
    F, T = 80_000.0, 3 / 365
    p = digital_call_flat(F, K, T, true_vol)
    assert implied_vol_digital(p, F, K, T) == pytest.approx(true_vol, rel=1e-4)


def test_implied_vol_none_when_unreachable():
    # 價外數位買權的機率有上限（σ 太大反而下降），要求 0.9 一定找不到解
    F, K, T = 80_000.0, 86_000.0, 3 / 365
    assert implied_vol_digital(0.9, F, K, T) is None


def test_touch_implied_vol_roundtrip():
    S, B, T = 80_000.0, 87_500.0, 3 / 365
    p = one_touch(S, B, T, 0.45)
    assert implied_vol_from_prob(p, lambda s: one_touch(S, B, T, s)) == pytest.approx(0.45, rel=1e-4)
