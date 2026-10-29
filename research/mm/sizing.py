"""每個市場該放多少本金？先在回測樣本驗證，再套用到全站。

每個市場每天的預期淨額（本金 C）：
    獎勵(C) = R × 掛單比例 × sC / (sC + K)        ← 分到的比例會飽和
    成本(C) = h × C                               ← 預期成交虧損 + 資金成本，跟本金成正比
最適本金 C* = argmax 獎勵(C) − 成本(C)，限制在 [最低股數需要的本金, 我的分數不超過競爭者]。
"""
import numpy as np
import pandas as pd

COST_OF_CAPITAL = 0.04 / 365      # 年化 4% 的資金成本
DIST = 0.7                        # 掛在 0.7 × 最大合格價差


def best_capital(R, cov, s, K, h, c_min, c_max):
    """在 [c_min, c_max] 上找 R·cov·sC/(sC+K) − hC 的最大值（對數格點，夠準）。"""
    if c_max < c_min:
        return 0.0, 0.0
    grid = np.unique(np.concatenate([np.geomspace(max(c_min, 1), max(c_max, c_min + 1), 200), [c_min]]))
    val = R * cov * s * grid / (s * grid + K) - h * grid if K > 0 else R * cov - h * grid
    i = int(np.argmax(val))
    return (grid[i], val[i]) if val[i] > 0 else (0.0, 0.0)


def plan(df, h_by_cat, cov_by_cat, cap_share=1.0, c_cap=50_000):
    rows = []
    for r in df.itertuples(index=False):
        d = DIST * r.v
        s = (1 - DIST) ** 2 / max(1 - 2 * d, 0.05)          # 每 $1 的分數
        c_min = max(r.min_size, 1) * (1 - 2 * d)              # 兩邊各掛最低股數需要的本金
        c_max = min(c_cap, cap_share * r.comp / s) if r.comp > 0 else max(c_min, 1)
        h = h_by_cat.get(r.category, h_by_cat["_"]) + COST_OF_CAPITAL
        cov = cov_by_cat.get(r.category, cov_by_cat["_"])
        c, net = best_capital(r.rate, cov, s, r.comp, h, c_min, max(c_max, c_min))
        rows.append(dict(capital=c, exp_net=net, reward=(r.rate * cov * s * c / (s * c + r.comp) if c > 0 and r.comp > 0 else (r.rate * cov if c > 0 else 0))))
    return pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def category(q, slug):
    t = f"{q} {slug}".lower()
    if any(k in t for k in (" vs", "o/u", "spread:", "win on 20", "nfl", "nba", "mlb", "nhl", "ufc", "premier", "laliga", "serie",
                            "bundesliga", "champions", "world series", "worlds 20", "lol", "cs2", "dota", "atp", "wta")):
        return "運動／電競"
    if any(k in t for k in ("election", "senate", "house", "governor", "president", "prime minister", "mandate", "balance of power",
                            "mayor", "parliament", "party", "nominee", "trump", "minister", "seats")):
        return "政治"
    if any(k in t for k in ("fed ", "interest rate", "cpi", "inflation", "gdp", "unemployment", "jobs", "recession", "bps")):
        return "總經"
    if any(k in t for k in ("bitcoin", "btc", "ethereum", "eth ", "solana", "crypto", "xrp")):
        return "加密貨幣"
    return "其他"


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    # ---------- 1) 在回測樣本驗證：用一半估參數、另一半看結果 ----------
    f = pd.read_csv("mm_combo_F.csv")            # 規則 F、每個市場 $1,000 的逐半週結果
    mm = pd.read_csv("reward_mm.csv")
    mm = mm[(mm["capital"] == 1000) & (mm["dist_c"] != 1.0)][["q", "rate", "max_spread_c", "comp_score", "min_size"]]
    mm = mm.rename(columns={"comp_score": "comp"}).assign(v=lambda x: x["max_spread_c"] / 100)
    d = DIST * mm["v"]
    mm["s"] = (1 - DIST) ** 2 / (1 - 2 * d)
    res = []
    for train, test in (("first", "second"), ("second", "first")):
        tr = f[f["half"] == train].merge(mm, on="q")
        te = f[f["half"] == test].merge(mm, on="q")
        # 掛單比例：從回測的 $1,000 獎勵反推（獎勵 = R × 比例 × 1000s/(1000s+K)）
        tr["cov_hat"] = (tr["reward"] / (tr["rate"] * 1000 * tr["s"] / (1000 * tr["s"] + tr["comp"]))).clip(0, 1)
        te["cov_hat"] = (te["reward"] / (te["rate"] * 1000 * te["s"] / (1000 * te["s"] + te["comp"]))).clip(0, 1)
        a_tr = (tr["pnl"] + tr["rebate"]) / 1000
        h_cat = {"_": -a_tr.clip(upper=0).mean()}
        for cat, g in tr.assign(a=a_tr).groupby("category"):
            h_cat[cat] = -g["a"].clip(upper=0).mean() if len(g) >= 5 else h_cat["_"]
        cov_cat = {"_": tr["cov_hat"].mean(), **tr.groupby("category")["cov_hat"].mean().to_dict()}
        p = plan(tr[["q", "category", "rate", "v", "comp", "min_size"]].drop_duplicates("q"), h_cat, cov_cat)
        p = p.merge(te[["q", "reward", "pnl", "rebate", "cov_hat"]].rename(columns={"reward": "r1k", "pnl": "p1k", "rebate": "b1k", "cov_hat": "cov_te"}), on="q")
        s_ = (1 - DIST) ** 2 / (1 - 2 * DIST * p["v"])
        p["test_reward"] = np.where(p["capital"] > 0, p["rate"] * p["cov_te"] * s_ * p["capital"] / (s_ * p["capital"] + p["comp"]), 0)
        p["test_net"] = p["test_reward"] + (p["p1k"] + p["b1k"]) * p["capital"] / 1000
        uni = (p["r1k"] + p["p1k"] + p["b1k"])
        res.append(dict(驗證="用" + ("前" if train == "first" else "後") + "半估、" + ("後" if test == "second" else "前") + "半驗證",
                        固定1k_本金=1000 * len(p), 固定1k_每天淨額=round(uni.sum(), 1), 固定1k_年化=f"{uni.sum() * 365 / (1000 * len(p)):.0%}",
                        最適_本金=round(p["capital"].sum()), 最適_投入市場=int((p["capital"] > 0).sum()),
                        最適_每天淨額=round(p["test_net"].sum(), 1), 最適_年化=f"{p['test_net'].sum() * 365 / max(p['capital'].sum(), 1):.0%}",
                        最適_最差=round(p["test_net"].min(), 1)))
    print("=== 1) 回測樣本（86 個市場、規則 F）：固定 $1,000 vs 最適本金 ===")
    print(pd.DataFrame(res).to_string(index=False))

    # ---------- 2) 套用到全站可用市場 ----------
    u = pd.read_csv("universe.csv")
    u = u[u["ok"]].copy()
    u["category"] = [category(q, str(sl)) for q, sl in zip(u["q"], u["slug"])]
    a_all = (f["pnl"] + f["rebate"]) / 1000
    f2 = f.assign(a=a_all)
    base_h = {"_": -a_all.clip(upper=0).mean(), **{c: -g["a"].clip(upper=0).mean() for c, g in f2.groupby("category") if len(g) >= 10}}
    cov_all = f.assign(cov_hat=f["coverage"]).groupby("category")["cov_hat"].mean().to_dict()
    cov_all["_"] = f["coverage"].mean()
    print("\n門檻 h（每 $1 每天，不含資金成本）：", {k: f"{v * 100:.3f}%" for k, v in base_h.items()})
    print("掛單比例：", {k: round(v, 2) for k, v in cov_all.items()})
    out = []
    for label, mult in (("樂觀（門檻 × 0.5）", 0.5), ("基準", 1.0), ("保守（門檻 × 2）", 2.0), ("很保守（門檻 × 4）", 4.0)):
        h = {k: v * mult for k, v in base_h.items()}
        p = plan(u, h, cov_all)
        inv = p[p["capital"] > 0]
        out.append(dict(情境=label, 投入市場=len(inv), 本金=round(inv["capital"].sum()), 每天獎勵=round(inv["reward"].sum()),
                        每天預期淨額=round(inv["exp_net"].sum()), 年化=f"{inv['exp_net'].sum() * 365 / max(inv['capital'].sum(), 1):.0%}",
                        單一市場最大本金=round(inv["capital"].max()), 本金中位數=round(inv["capital"].median())))
        if mult == 1.0:
            p.to_csv("universe_plan.csv", index=False)
    print("\n=== 2) 全站 8,316 個可用市場，用最適本金（依競爭、獎勵、門檻） ===")
    print(pd.DataFrame(out).to_string(index=False))
    p = pd.read_csv("universe_plan.csv")
    inv = p[p["capital"] > 0]
    print("\n基準情境，依類別：")
    print(inv.groupby("category").agg(市場=("q", "size"), 本金=("capital", "sum"), 獎勵=("reward", "sum"), 淨額=("exp_net", "sum")).round(0).assign(
        年化=lambda x: (x["淨額"] * 365 / x["本金"]).map(lambda v: f"{v:.0%}")).to_string())
    print("\n依每日獎金池大小：")
    inv = inv.assign(rb=pd.cut(inv["rate"], [0, 5, 20, 100, 500, 1e9], labels=["<$5", "$5–20", "$20–100", "$100–500", "≥$500"]))
    print(inv.groupby("rb", observed=True).agg(市場=("q", "size"), 本金=("capital", "sum"), 淨額=("exp_net", "sum")).round(0).assign(
        年化=lambda x: (x["淨額"] * 365 / x["本金"]).map(lambda v: f"{v:.0%}")).to_string())
    print("\n本金最多的 10 個市場：")
    print(inv.sort_values("capital", ascending=False).head(10)[["q", "category", "rate", "comp", "capital", "reward", "exp_net"]].round(1).to_string(index=False))
