"""文件用的最後數字：基準、F0、F1、F1 拿掉波動暫停（實盤預設），排除獎勵最高的 5 個以色列市場。"""
exec(open("param_search.py").read().split("SPACE = dict(")[0])

def show(name, r):
    df = evaluate(MARKETS, r, SPLIT)
    df = df[~df["q"].isin(TOP5)]
    out = [name]
    for h in ("first", "second"):
        g = df[df["half"] == h]
        out.append(f"{h}: 淨額 {g['net'].sum():7.0f}  獎勵 {g['reward'].sum():6.0f}  成交 {g['pnl'].sum():7.0f}  "
                   f"賺錢 {int((g['net'] > 0).sum())}/{len(g)}  最差 {g['net'].min():6.1f}  掛單 {g['coverage'].mean():.0%}")
    print(" | ".join(out), flush=True)
    return df

print(len(MARKETS), "markets; TOP5:", sorted(TOP5))
show("基準（一直掛）", Rules())
show("F0", F0)
F1 = replace(F0, cooldown=30, trail_hours=6)
show("F1", F1)
d = show("F1 − 波動暫停（實盤預設）", replace(F1, vol_window=0))
d.to_csv("mm_final_live_default.csv", index=False)
show("F1 − 波動暫停 − 單日停損", replace(F1, vol_window=0, stop_loss=None))
