"""同一組規則，哪些參數比較好？——敏感度、隨機搜尋、交叉驗證。

評估都拿掉獎勵最高的 5 個以色列選舉市場（它們會主導總額）。
目標：後半週（或前半週）81 個市場每天淨額合計；同時看虧損市場合計、最差市場、掛單時間。
"""
import random
from dataclasses import asdict, replace
from multiprocessing import Pool

import numpy as np
import pandas as pd

exec(open("mm_backtest.py").read().split('if __name__ == "__main__":')[0])

MARKETS = load()
_s = min(mk.t0 for mk in MARKETS)
_e = max(mk.t0 + 60 * len(mk.p) for mk in MARKETS)
SPLIT = (_s + _e) // 2
_b = evaluate(MARKETS, Rules(), SPLIT)
TOP5 = set(_b.groupby("q")["reward"].sum().nlargest(5).index)

F0 = Rules(dist_frac=0.7, stop_loss=25, cooldown=60, vol_window=60, vol_mult=2.0, volu_window=10, volu_mult=5, trail_hours=24)
SPACE = dict(
    dist_frac=[0.5, 0.6, 0.7, 0.8, 0.9],
    stop_loss=[None, 5, 10, 15, 25, 50],
    cooldown=[0, 15, 30, 60, 120, 240],
    vol_window=[0, 15, 30, 60, 120],
    vol_mult=[1.0, 1.5, 2.0, 3.0],
    volu_window=[0, 5, 10, 30],
    volu_mult=[3, 5, 10, 20],
    trail_hours=[0, 6, 12, 24, 48],
)


def score(rules: Rules) -> dict:
    df = evaluate(MARKETS, rules, SPLIT)
    df = df[~df["q"].isin(TOP5)]
    out = {}
    for half, k in (("first", "前"), ("second", "後")):
        g = df[df["half"] == half]
        out[f"{k}_淨額"] = g["net"].sum()
        out[f"{k}_虧損合計"] = g["net"][g["net"] < 0].sum()
        out[f"{k}_最差"] = g["net"].min()
        out[f"{k}_賺錢"] = int((g["net"] > 0).sum())
        out[f"{k}_掛單"] = g["coverage"].mean()
    return out


def run_one(r: Rules) -> dict:
    return dict(asdict(r), **score(r))


def fmt(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if c.endswith("掛單"):
            df[c] = (df[c] * 100).round(0).astype(int).astype(str) + "%"
        elif df[c].dtype.kind == "f" and ("淨額" in c or "虧損" in c or "最差" in c):
            df[c] = df[c].round(0).astype(int)
    return df


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    # ---------- 1) 一次只改一個參數 ----------
    cands = [("F0（目前設定）", F0)]
    for name, vals in SPACE.items():
        for v in vals:
            if getattr(F0, name) == v:
                continue
            if name == "vol_mult" and F0.vol_window == 0:
                continue
            cands.append((f"{name}={v}", replace(F0, **{name: v})))
    with Pool(4) as pool:
        res = pool.map(run_one, [c for _, c in cands])
    sens = pd.DataFrame(res)
    sens.insert(0, "變動", [n for n, _ in cands])
    cols = ["變動", "前_淨額", "後_淨額", "前_虧損合計", "後_虧損合計", "前_最差", "後_最差", "前_掛單", "後_掛單"]
    print("=== 1) 一次只改一個參數（其他維持 F0），81 個市場每天淨額合計 ===")
    print(fmt(sens[cols]).to_string(index=False))
    sens.to_csv("param_sensitivity.csv", index=False)

    # ---------- 2) 隨機搜尋 ----------
    random.seed(11)
    combos = []
    keys = list(SPACE)
    while len(combos) < 300:
        c = {k: random.choice(SPACE[k]) for k in keys}
        if c["vol_window"] == 0:
            c["vol_mult"] = 0.0
        if c["volu_window"] == 0:
            c["volu_mult"] = 0.0
        r = Rules(**c)
        if r not in combos:
            combos.append(r)
    with Pool(4) as pool:
        res = pool.map(run_one, combos)
    rs = pd.DataFrame(res)
    rs.to_csv("param_random.csv", index=False)
    rho = rs[["前_淨額", "後_淨額"]].corr(method="spearman").iloc[0, 1]
    print(f"\n=== 2) 隨機 300 組參數：前半週與後半週淨額的等級相關 = {rho:.2f} ===")
    print("（接近 1：前半週好的組合後半週也好；接近 0：挑參數主要是在擬合雜訊）")
    q = rs[["前_淨額", "後_淨額"]].quantile([0.1, 0.5, 0.9]).round(0)
    print("淨額分位數：\n", q.to_string())

    # ---------- 3) 交叉驗證：用一半挑最佳，另一半看結果 ----------
    f0 = score(F0)
    rows = []
    for train, test in (("前", "後"), ("後", "前")):
        best = rs.sort_values(f"{train}_淨額", ascending=False).iloc[0]
        top10 = rs.sort_values(f"{train}_淨額", ascending=False).head(10)
        rows.append(dict(方向=f"{train}半挑 → {test}半驗證", 挑到的最佳_樣本內=round(best[f"{train}_淨額"]), 最佳_樣本外=round(best[f"{test}_淨額"]),
                         前10名_樣本外平均=round(top10[f"{test}_淨額"].mean()), 前10名_樣本外最差=round(top10[f"{test}_淨額"].min()),
                         F0_樣本外=round(f0[f"{test}_淨額"]), 全部300組_樣本外中位數=round(rs[f"{test}_淨額"].median())))
    print("\n=== 3) 交叉驗證 ===")
    print(pd.DataFrame(rows).to_string(index=False))

    # ---------- 4) 兩個半週都在前 25% 的組合：參數長什麼樣子？ ----------
    both = rs[(rs["前_淨額"] >= rs["前_淨額"].quantile(0.75)) & (rs["後_淨額"] >= rs["後_淨額"].quantile(0.75))]
    print(f"\n=== 4) 兩個半週都在前 25% 的組合：{len(both)} / 300 組 ===")
    for k in keys:
        cnt_all = rs[k].astype(str).value_counts(normalize=True)
        cnt_top = both[k].astype(str).value_counts(normalize=True)
        line = "、".join(f"{v}: {cnt_top.get(v, 0):.0%}（全體 {cnt_all.get(v, 0):.0%}）" for v in rs[k].astype(str).value_counts().sort_index().index)
        print(f"  {k:12s} {line}")
    both = both.assign(兩半合計=both["前_淨額"] + both["後_淨額"]).sort_values("兩半合計", ascending=False)
    show = ["dist_frac", "stop_loss", "cooldown", "vol_window", "vol_mult", "volu_window", "volu_mult", "trail_hours",
            "前_淨額", "後_淨額", "前_虧損合計", "後_虧損合計", "前_最差", "後_最差", "前_掛單", "後_掛單"]
    print("\n兩個半週都好的前 10 組：")
    print(fmt(both[show].head(10)).to_string(index=False))
