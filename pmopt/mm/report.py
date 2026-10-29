"""彙整一次執行的記錄（模擬或實盤），並對照分階段上線的通過條件。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

# 每一階段要跑多久、要符合哪些條件才進下一階段（見 docs/market-making.md「上線步驟」）
STAGES = {
    "paper": dict(min_days=7, next="實盤 $10k"),
    "live-10k": dict(min_days=14, next="實盤 $100k"),
    "live-100k": dict(min_days=30, next="實盤 $300–350k（$1M 的建議部位）"),
}


def load_run(run_dir: str | Path) -> dict:
    d = Path(run_dir)
    out = dict(dir=d)
    out["summary"] = json.loads((d / "summary.json").read_text(encoding="utf-8")) if (d / "summary.json").exists() else {}
    out["minutes"] = pd.read_csv(d / "minutes.csv") if (d / "minutes.csv").exists() and (d / "minutes.csv").stat().st_size else pd.DataFrame()
    events = []
    if (d / "events.jsonl").exists():
        with open(d / "events.jsonl", encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
    out["events"] = events
    return out


def summarize(run_dir: str | Path) -> dict:
    run = load_run(run_dir)
    m, ev = run["minutes"], run["events"]
    kinds = Counter(e["kind"] for e in ev)
    res: dict = dict(dir=str(run["dir"]))
    if m.empty:
        res["note"] = "沒有分鐘記錄（執行時間太短？）"
        return res
    hours = (m["ts"].iloc[-1] - m["ts"].iloc[0]) / 3600 if len(m) > 1 else 0.0
    # 啟動後的暖機時間不算：從第一次有市場兩邊都掛的那一分鐘開始算掛單比例與每日數字
    active = m[m.index >= (m["quoting"] > 0).idxmax()] if (m["quoting"] > 0).any() else m
    active_hours = (active["ts"].iloc[-1] - active["ts"].iloc[0]) / 3600 if len(active) > 1 else 0.0
    last = m.iloc[-1]
    capital = float(m["capital"].mean())
    net = float(last["mtm_pnl"] + last["est_reward"] + last["rebate"])
    res.update(hours=round(hours, 2), active_hours=round(active_hours, 2), avg_capital=round(capital, 0),
               markets=int(last["markets"]),
               quoting_frac=round(float((active["quoting"] / active["markets"].clip(lower=1)).mean()), 3),
               mtm_pnl=round(float(last["mtm_pnl"]), 2), est_reward=round(float(last["est_reward"]), 2),
               rebate=round(float(last["rebate"]), 2), net_est=round(net, 2),
               max_inventory=round(float(m["inventory"].max()), 2),
               fills=kinds.get("fill", 0), placements=sum(e.get("n", 0) for e in ev if e["kind"] == "place"),
               cancels=sum(e.get("n", 0) for e in ev if e["kind"] == "cancel"),
               errors=kinds.get("error", 0), kills=[e.get("reason") for e in ev if e["kind"] == "kill"])
    if active_hours > 0:
        per_day = net / active_hours * 24
        res["net_per_day"] = round(per_day, 2)
        res["apr"] = round(per_day * 365 / capital, 4) if capital > 0 else None
        res["fill_loss_per_day"] = round(float(last["mtm_pnl"]) / active_hours * 24, 2)
        res["reward_per_day"] = round(float(last["est_reward"]) / active_hours * 24, 2)
        n_quoting = max(float(active["quoting"].mean()), 1.0)
        placements = sum(e.get("n", 0) for e in ev if e["kind"] == "place" and e["time"] >= active["ts"].iloc[0] + 600)
        span_h = max((active["ts"].iloc[-1] - active["ts"].iloc[0] - 600) / 3600, 1e-9)
        res["orders_per_market_hour"] = round(placements / n_quoting / span_h, 2)   # 不含一開始掛單的前 10 分鐘
    reasons: Counter = Counter()
    for r in m["reasons"].dropna():
        reasons.update(json.loads(r))
    res["pause_reasons"] = dict(reasons.most_common(10))
    scoring = [e["fraction"] for e in ev if e["kind"] == "scoring"]
    if scoring:
        res["scoring_frac"] = round(sum(scoring) / len(scoring), 3)
    diffs = [e.get("n_diffs", 0) for e in ev if e["kind"] == "reconcile"]
    if diffs:
        res["reconcile_max_diffs"] = max(diffs)
    return res


def gate(res: dict, stage: str, actual_reward: float | None = None) -> list[tuple[str, bool, str]]:
    """分階段上線的檢查表：回傳 (條件, 是否通過, 說明)。"""
    spec = STAGES[stage]
    days = res.get("hours", 0) / 24
    checks = [
        (f"至少跑 {spec['min_days']} 天", days >= spec["min_days"], f"{days:.1f} 天"),
        ("估計淨額 > 0（成交損益 + 獎勵 + 返佣）", res.get("net_est", -1) > 0, f"{res.get('net_est', 0):,.2f}"),
        ("成交虧損 < 估計獎勵的 60%",
         -min(res.get("mtm_pnl", 0), 0) < 0.6 * max(res.get("est_reward", 0), 1e-9),
         f"{res.get('mtm_pnl', 0):,.2f} vs {res.get('est_reward', 0):,.2f}"),
        ("沒有被風控停機", not res.get("kills"), "、".join(map(str, res.get("kills") or [])) or "無"),
        ("掛單時間比例 ≥ 60%", res.get("quoting_frac", 0) >= 0.6, f"{res.get('quoting_frac', 0):.0%}"),
    ]
    if stage != "paper":
        checks.append(("部位對帳一致", res.get("reconcile_max_diffs", 0) == 0, str(res.get("reconcile_max_diffs", "沒有對帳記錄"))))
        checks.append(("抽樣掛單計分比例 ≥ 80%", res.get("scoring_frac", 0) >= 0.8, f"{res.get('scoring_frac', 0):.0%}"))
        if actual_reward is not None:
            ok = actual_reward >= 0.5 * res.get("est_reward", 0)
            checks.append(("實際獎勵 ≥ 估計的 50%", ok, f"{actual_reward:,.2f} vs {res.get('est_reward', 0):,.2f}"))
        else:
            checks.append(("實際獎勵 ≥ 估計的 50%", False, "用 mm-earnings 查實際獎勵後以 --actual-reward 帶入"))
    return checks


def print_report(res: dict, stage: str | None = None, actual_reward: float | None = None) -> None:
    labels = dict(hours="執行時數", active_hours="暖機後時數", avg_capital="平均本金", markets="市場數", quoting_frac="兩邊都掛的時間比例",
                  mtm_pnl="成交損益（市值）", est_reward="估計獎勵", rebate="估計返佣", net_est="估計淨額",
                  net_per_day="每日淨額", apr="年化（估計淨額／本金）", fill_loss_per_day="每日成交損益",
                  reward_per_day="每日估計獎勵", max_inventory="最大庫存", fills="成交筆數", placements="下單張數",
                  cancels="撤單張數", orders_per_market_hour="每市場每小時下單", errors="錯誤次數", scoring_frac="抽樣計分比例", reconcile_max_diffs="對帳不一致")
    print(f"記錄：{res['dir']}")
    if "note" in res:
        print(res["note"])
        return
    for k, label in labels.items():
        if k in res:
            v = res[k]
            if k in ("apr", "quoting_frac", "scoring_frac") and v is not None:
                v = f"{v:.1%}"
            elif isinstance(v, float):
                v = f"{v:,.2f}"
            print(f"  {label:<14} {v}")
    if res.get("pause_reasons"):
        print("  沒掛單的原因（每一輪 × 市場的次數）：")
        for r, n in res["pause_reasons"].items():
            print(f"    {r:<16} {n:,}")
    if res.get("kills"):
        print(f"  停機原因：{res['kills']}")
    print("  注意：模擬的成交是用公開成交推估、獎勵是用靜態的競爭者分數估的；實盤以官方的實際獎勵為準。")
    if stage:
        print(f"\n上線檢查（{stage} → {STAGES[stage]['next']}）：")
        checks = gate(res, stage, actual_reward)
        for name, ok, detail in checks:
            print(f"  [{'通過' if ok else '未過'}] {name}：{detail}")
        print("  → " + ("全部通過，可以進下一階段" if all(ok for _, ok, _ in checks) else "還不能進下一階段"))
