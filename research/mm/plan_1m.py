"""投入 $1,000,000：用 F1 規則的數據，在限制下配置到各市場，並做壓力測試。

限制：單一市場 ≤ $20,000、單一事件 ≤ $30,000、剩不到 1 天到期的不放（很快就會進入凍結）。
進場門檻的倍數用二分法調整，直到總本金 ≈ $1,000,000。
"""
import numpy as np
import pandas as pd

exec(open("sizing.py").read().split('\nif __name__ == "__main__":')[0])

BUDGET, MARKET_CAP, EVENT_CAP = 1_000_000, 20_000, 30_000

f = pd.read_csv("mm_combo_F1.csv")
a_all = (f["pnl"] + f["rebate"]) / 1000
# 每個類別的虧損率往全體平均收縮：樣本少的類別（例如只有 5 個運動市場）不能太相信
SHRINK = 20   # 等同把全體平均當成 20 個市場的樣本
h_all = -a_all.clip(upper=0).mean()
base_h = {"_": h_all}
for c, g in f.assign(a=a_all).groupby("category"):
    n = g["q"].nunique()
    base_h[c] = (n * -g["a"].clip(upper=0).mean() + SHRINK * h_all) / (n + SHRINK)
cov = {**f.groupby("category")["coverage"].mean().to_dict(), "_": f["coverage"].mean()}

u = pd.read_csv("universe.csv")
u = u[u["ok"] & (u["days_left"] >= 1)].copy()
u["category"] = [category(q, str(sl)) for q, sl in zip(u["q"], u["slug"])]
u["midterm"] = u["q"].str.contains("Senate|House|governor|Governor|Balance of Power|midterm|Midterm") & (u["days_left"] < 10)


def allocate(mult: float, universe: pd.DataFrame) -> pd.DataFrame:
    p = plan(universe, {k: v * mult for k, v in base_h.items()}, cov, c_cap=MARKET_CAP)
    p = p[p["capital"] > 0].copy()
    # 單一事件上限：超過就把該事件所有市場等比例縮小（獎勵用同一條飽和曲線重算）
    ev = p.groupby("slug")["capital"].transform("sum")
    scale = np.minimum(1.0, EVENT_CAP / ev)
    s_ = (1 - DIST) ** 2 / (1 - 2 * DIST * p["v"])
    c_new = p["capital"] * scale
    cv = p["category"].map(lambda c: cov.get(c, cov["_"]))
    p["reward"] = np.where(p["comp"] > 0, p["rate"] * cv * s_ * c_new / (s_ * c_new + p["comp"]), p["rate"] * cv)
    p["capital"] = c_new
    p["h"] = p["category"].map(lambda c: base_h.get(c, base_h["_"]))
    return p


def solve(universe):
    lo, hi = 0.2, 50.0
    for _ in range(40):
        mid = (lo * hi) ** 0.5
        if allocate(mid, universe)["capital"].sum() > BUDGET:
            lo = mid
        else:
            hi = mid
    return hi, allocate(hi, universe)


def report(p, label):
    C = p["capital"].sum()
    print(f"\n=== {label}：{len(p)} 個市場、本金 ${C:,.0f} ===")
    rows = []
    for real in (1, 2, 3, 4):
        net = p["reward"].sum() - (p["h"] * real * p["capital"]).sum() - C * COST_OF_CAPITAL
        rows.append(dict(實際虧損率=f"估計的 {real} 倍", 每天獎勵=round(p["reward"].sum()), 每天淨額=round(net),
                         每月淨額=round(net * 30), 年化=f"{net * 365 / C:.0%}"))
    print(pd.DataFrame(rows).to_string(index=False))
    g = p.groupby("category").agg(市場=("q", "size"), 本金=("capital", "sum"), 每天獎勵=("reward", "sum"))
    g["本金占比"] = (g["本金"] / C).map("{:.0%}".format)
    print(g.round(0).to_string())
    print("本金：中位數 ${:,.0f}，最大 ${:,.0f}；最大單一事件 ${:,.0f}".format(
        p["capital"].median(), p["capital"].max(), p.groupby("slug")["capital"].sum().max()))
    return p


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    print("門檻 h（每 $1 每天）：", {k: f"{v * 100:.3f}%" for k, v in base_h.items()})
    print("掛單比例：", {k: round(v, 2) for k, v in cov.items()})
    m1, p1 = solve(u)
    print(f"\n門檻倍數 = {m1:.2f}")
    report(p1, "本週（含期中選舉）")
    p1.to_csv("plan_1m.csv", index=False)
    core = u[~u["midterm"]]
    m2, p2 = solve(core)
    print(f"\n門檻倍數 = {m2:.2f}")
    report(p2, "排除期中選舉（11/3 之後的常態）")
    p2.to_csv("plan_1m_core.csv", index=False)
