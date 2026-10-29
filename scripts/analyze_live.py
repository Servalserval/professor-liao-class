"""分析 collect 收到的同步資料：中心價格差、無模型違反、近似套利是否持續、穩健 edge。

    python scripts/analyze_live.py data/live          # 資料夾裡要有 scans.csv 和 scans_pairs.csv
"""
import sys
from pathlib import Path

import pandas as pd

d = Path(sys.argv[1] if len(sys.argv) > 1 else "data/live")
scans = pd.read_csv(d / "scans.csv")
pairs = pd.read_csv(d / "scans_pairs.csv")
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

rounds = pairs.groupby("ts_utc").agg(
    index_price=("index_price", "first"), center_above=("center_above", "first"), center_touch=("center_touch", "first"))
rounds["gap_pct"] = (rounds["center_touch"] / rounds["center_above"] - 1) * 100
mf = pairs[pairs["model_free"]]
rounds["n_mf_violations"] = mf[mf["slack"] > 0].groupby("ts_utc").size().reindex(rounds.index, fill_value=0)
rounds["max_mf_slack"] = mf.groupby("ts_utc")["slack"].max()
near = pairs[(~pairs["model_free"]) & (pairs["relation"] == "觸價 vs 到期")]
rounds["n_near_pos_ev"] = near[(near["slack"] > 0) & (near["ev"] > 0)].groupby("ts_utc").size().reindex(rounds.index, fill_value=0)
print(f"輪數 {len(rounds)}，{rounds.index.min()} ~ {rounds.index.max()}")
print(f"BTC 指數 {rounds['index_price'].min():,.0f} ~ {rounds['index_price'].max():,.0f}")
print("\n兩組市場的中心價格差（觸價盤 / 到期盤 − 1，%）：")
print(rounds["gap_pct"].describe().round(3).to_string())
print("\n期權 vs 到期盤中心（%）：", ((scans.groupby("ts_utc")["center_factor"].first() - 1) * 100).describe().round(3).to_dict())
print("抓取時間差（秒）：", scans.groupby("ts_utc")["sync_gap_sec"].first().describe().round(2).to_dict())
print("\n每輪的無模型違反數：", rounds["n_mf_violations"].value_counts().sort_index().to_dict())

print("\n=== 無模型違反：哪些組合、多常、多大 ===")
v = mf.assign(viol=mf["slack"] > 0).groupby(["relation", "pair"]).agg(
    rounds=("slack", "size"), share_violated=("viol", "mean"), max_slack=("slack", "max"), median_slack=("slack", "median"))
print(v[v["share_violated"] > 0].sort_values("share_violated", ascending=False).round(4).to_string())

print("\n=== 近似套利（觸價 vs 到期、有缺口）：slack > 0 的比例最高的組合 ===")
n = near.assign(pos=near["slack"] > 0, pos_ev=(near["slack"] > 0) & (near["ev"] > 0)).groupby("pair").agg(
    rounds=("slack", "size"), share_slack_pos=("pos", "mean"), share_pos_ev=("pos_ev", "mean"),
    median_slack=("slack", "median"), max_slack=("slack", "max"), median_p_gap=("p_gap", "median"), median_ev=("ev", "median"))
print(n.sort_values(["share_pos_ev", "median_slack"], ascending=False).head(12).round(4).to_string())

print("\n=== 期權 vs Polymarket（中心對齊後）：依題型與期權機率分組的平均 (PM 中價 − fair) ===")
s = scans.dropna(subset=["bid", "ask", "fair_centered"]).copy()
s["mid"] = (s["bid"] + s["ask"]) / 2
s["diff"] = s["mid"] - s["fair_centered"]
s["fair_bucket"] = pd.cut(s["fair_centered"], [0, 0.02, 0.1, 0.3, 0.7, 0.9, 0.98, 1.0], include_lowest=True)
s["kind2"] = s["kind"].where(s["kind"] != "above", "above " + s["end_utc"].str[:5])
print(s.pivot_table(index="fair_bucket", columns="kind2", values="diff", aggfunc="mean", observed=True).round(4).to_string())

if "best_edge_robust" in scans.columns:
    print("\n=== 扣手續費後的 edge：一般 vs 穩健（觸價盤要超出兩種近似的區間）===")
    s2 = scans.copy()
    s2["kind2"] = s2["kind"].where(s2["kind"] != "above", "above " + s2["end_utc"].str[:5])
    g = s2.groupby("kind2")
    print(pd.DataFrame({
        "觀測": g.size(),
        "edge≥1¢ 比例": g["best_edge"].apply(lambda x: (x >= 0.01).mean()).round(3),
        "穩健 edge≥1¢ 比例": g["best_edge_robust"].apply(lambda x: (x >= 0.01).mean()).round(3),
        "穩健 edge 最大": g["best_edge_robust"].max().round(4),
    }).to_string())
    top = s2[s2["best_edge_robust"] >= 0.01].groupby(["market", "end_utc"]).agg(
        rounds=("best_edge_robust", "size"), median_robust=("best_edge_robust", "median"),
        median_fair=("fair", "median"), median_bid=("bid", "median"), median_ask=("ask", "median"))
    print("\n穩健 edge ≥ 1¢ 的市場（出現輪數）：")
    print(top.sort_values("rounds", ascending=False).head(15).to_string())
