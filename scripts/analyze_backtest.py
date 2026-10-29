"""回測的補充分析：冷門結果的事件數與精確信賴區間、BTC 走勢、模型波動率敏感度、前後半段。

    python scripts/analyze_backtest.py results/backtest_2025-10_2026-10/history.csv

冷門結果（例如機率 0.3% 的事件）在一千多筆樣本裡只會發生幾次，群組標準誤在這種情況下不可靠，
所以這裡改用二項分布的精確信賴區間（Clopper–Pearson），並直接比較「報價暗示的次數」和「實際次數」。
"""
import sys

import numpy as np
import pandas as pd
from scipy.stats import beta

sys.path.insert(0, ".")
from pmopt import history as H  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "results/backtest_2025-10_2026-10/history.csv"
df = H.usable(H.add_model(pd.read_csv(path)))
pd.set_option("display.width", 220)


def cp(k, n, a=0.05):
    lo = 0.0 if k == 0 else beta.ppf(a / 2, k, n - k + 1)
    hi = 1.0 if k == n else beta.ppf(1 - a / 2, k + 1, n - k)
    return lo, hi


print("=== 1) 冷門那一邊（價格 q = min(p, 1−p) ≤ 門檻）：報價 vs 實際發生次數 ===")
rows = []
for h, g in df.groupby("horizon_h"):
    q = np.minimum(g["pm_price"], 1 - g["pm_price"])
    longshot_hit = np.where(g["pm_price"] <= 0.5, g["outcome"], 1 - g["outcome"])
    for cap in (0.01, 0.03, 0.10):
        m = q <= cap
        n, k = int(m.sum()), int(longshot_hit[m].sum())
        lo, hi = cp(k, n)
        rows.append(dict(horizon_h=h, q_max=cap, n=n, n_days=g.loc[m, "day"].nunique(), avg_price=round(q[m].mean(), 4),
                         events=k, expected_by_price=round(q[m].sum(), 1), realized=round(k / n, 4),
                         ci95=f"[{lo:.4f}, {hi:.4f}]"))
print(pd.DataFrame(rows).to_string(index=False))

print("\n=== 2) BTC 在樣本期間的走勢 ===")
sp = df[df["horizon_h"] == 24].groupby("day")["spot"].first()
print(f"{sp.index.min()} {sp.iloc[0]:,.0f} → {sp.index.max()} {sp.iloc[-1]:,.0f}（{(sp.iloc[-1] / sp.iloc[0] - 1) * 100:+.1f}%）")
mid = df[df["pm_price"].between(0.25, 0.75)]
print("中間價位（25%–75%）的平均 (價格 − 結果)：", round((mid["pm_price"] - mid["outcome"]).mean(), 4),
      "；模型 (機率 − 結果)：", round((mid["model_prob"] - mid["outcome"]).mean(), 4))
half = sp.index[len(sp) // 2]
for name, part in (("前半段", mid[mid["day"] < half]), ("後半段", mid[mid["day"] >= half])):
    s = df[df["horizon_h"] == 24].groupby("day")["spot"].first()
    s = s[(s.index < half)] if name == "前半段" else s[(s.index >= half)]
    print(f"  {name}：BTC {(s.iloc[-1] / s.iloc[0] - 1) * 100:+.1f}%，中間價位 (價格 − 結果) = {(part['pm_price'] - part['outcome']).mean():+.4f}")

print("\n=== 3) 模型波動率 = DVOL × 倍數：哪個倍數最準？（Brier，越小越好）===")
out = []
for scale in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2):
    m = H.add_model(df.drop(columns=["model_prob"]), vol_scale=scale)
    for h, g in m.groupby("horizon_h"):
        out.append(dict(scale=scale, horizon_h=h, brier_model=((g["model_prob"] - g["outcome"]) ** 2).mean()))
t = pd.DataFrame(out).pivot(index="scale", columns="horizon_h", values="brier_model").round(5)
pm = df.groupby("horizon_h").apply(lambda g: ((g["pm_price"] - g["outcome"]) ** 2).mean()).round(5)
t.loc["Polymarket"] = pm
print(t.to_string())

print("\n=== 4) 前後半段分開看 Brier 差（> 0 代表 Polymarket 較差）===")
for name, part in (("前半段", df[df["day"] < half]), ("後半段", df[df["day"] >= half])):
    sc = H.scores(part)
    print(name, sc[["horizon_h", "n", "n_days", "brier_pm", "brier_model", "brier_diff", "brier_diff_se"]].to_string(index=False))
