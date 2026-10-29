"""樣本外檢驗：用前半週的價格波動挑市場，看後半週掛單的淨報酬。

獎勵收入沿用 reward_mm.csv（同一個訂單簿快照，假設獎勵與競爭整週不變）；
跳價成本分別用前半、後半週的 1 分鐘價格（5 分鐘中位數濾波）計算。
"""
import pickle

import numpy as np
import pandas as pd

raw = pickle.load(open("reward_mm_raw.pkl", "rb"))
mm = pd.read_csv("reward_mm.csv")
mm = mm[(mm["capital"] == 1000) & (mm["dist_c"] != 1.0) & (mm["best_spread_c"] <= mm["max_spread_c"])]
cid = pd.read_csv("rewards_all.csv").set_index("q")["condition_id"]
rows = []
for r in mm.itertuples(index=False):
    full_q = [q for q in cid.index if q.startswith(r.q)]
    if not full_q or cid[full_q[0]] not in raw:
        continue
    px = raw[cid[full_q[0]]]["px"]
    if len(px) < 1440 * 2:
        continue
    px = pd.Series(px).rolling(5, center=True, min_periods=1).median().to_numpy()
    d = r.dist_c / 100
    n = 1000 / (1 - 2 * d)
    half = len(px) // 2
    stats = {}
    for name, seg in (("first", px[:half]), ("second", px[half:])):
        dp = np.abs(np.diff(seg))
        days = (len(seg) - 1) / 1440
        stats[name] = dict(freq=(dp > d).sum() / days, loss=n * np.clip(dp - d, 0, None).sum() / days)
    rows.append(dict(category=r.category, q=r.q, reward=r.reward_day, freq1=stats["first"]["freq"],
                     loss1=stats["first"]["loss"], loss2=stats["second"]["loss"],
                     net2=r.reward_day - stats["second"]["loss"], days_to_end=r.days_to_end))
df = pd.DataFrame(rows)
print(f"{len(df)} 個市場（有至少 2 天價格資料、目前有雙邊報價）")
df["pick"] = pd.cut(df["freq1"], [-0.1, 0.5, 2, 10, 1e9], labels=["<0.5 次/天", "0.5–2", "2–10", ">10"])
g = df.groupby("pick", observed=True)
print(pd.DataFrame({
    "市場數": g.size(),
    "後半週淨報酬>0": g["net2"].apply(lambda x: (x > 0).mean()).round(2),
    "後半週淨報酬中位數": g["net2"].median().round(2),
    "後半週淨報酬平均": g["net2"].mean().round(2),
    "最差": g["net2"].min().round(1),
    "獎勵中位數": g["reward"].median().round(2),
}).to_string())
sel = df[df["freq1"] < 2]
print(f"\n規則「前半週每天 < 2 次」選出 {len(sel)} 個市場：後半週每天淨報酬合計 ${sel['net2'].sum():,.0f}，"
      f"其中正的 {int((sel['net2'] > 0).sum())} 個；最差的 5 個：")
print(sel.sort_values("net2").head(5)[["category", "q", "reward", "loss1", "loss2", "net2", "days_to_end"]].round(2).to_string(index=False))

# 已排定事件：以色列國會選舉在 10/27 投票，相關市場在樣本期間內開票
israel = sel["q"].str.contains("Knesset|Israel|Netanyahu|Eizenkot|mandate|Likud|Yashar|UTJ|Joint List", case=False)
rest = sel[~israel]
print(f"\n排除以色列選舉相關的 {int(israel.sum())} 個市場後：剩 {len(rest)} 個，後半週每天淨報酬合計 ${rest['net2'].sum():,.0f}，"
      f"平均 ${rest['net2'].mean():.2f}、中位數 ${rest['net2'].median():.2f}，正的 {int((rest['net2'] > 0).sum())} 個")
print("年化（每個市場 $1,000）：平均", round(rest["net2"].mean() * 365 / 1000, 2), "中位數", round(rest["net2"].median() * 365 / 1000, 2))
print(rest.sort_values("net2").head(5)[["category", "q", "reward", "loss2", "net2", "days_to_end"]].round(2).to_string(index=False))
print(rest.sort_values("net2").tail(5)[["category", "q", "reward", "loss2", "net2", "days_to_end"]].round(2).to_string(index=False))
