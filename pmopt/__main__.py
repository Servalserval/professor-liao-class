"""命令列介面。

    python -m pmopt scan --snapshot data/snapshot_2026-10-29     # 離線示範（課堂用）
    python -m pmopt scan --live                                  # 即時抓資料掃一次
    python -m pmopt check --snapshot data/snapshot_2026-10-29    # Polymarket 自己的兩組市場一致嗎？
    python -m pmopt collect --minutes 120 --interval 60          # 每分鐘收集一次，存成 CSV
    python -m pmopt summarize data/live/scans.csv                # 彙整收集到的資料
    python -m pmopt backtest --start 2026-09-01 --end 2026-10-28 # 已結算的每日盤：誰比較準？
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .consistency import evaluate_near_arbs, find_pairs, public_columns
from .polymarket import load_snapshot, parse_time
from .scanner import ScanConfig, fit_center, scan
from .surface import VolSurface

SHOW = ["market", "end_utc", "hours_left", "deribit_vol", "pm_iv", "fair", "fair_alt", "bid", "ask", "best_edge",
        "best_edge_robust", "signal"]


def _print_table(df: pd.DataFrame, title: str) -> None:
    print(f"\n=== {title} ===")
    if df.empty:
        print("（沒有可比較的市場）")
        return
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.max_rows", 200):
        print(df[SHOW].to_string(index=False))


def _scan_both(markets, surface, now, cfg):
    raw = scan(markets, surface, now, cfg)
    factor = fit_center(markets, surface, now, cfg)
    if factor is None:
        return raw, None, None, None
    centered_surface = surface.shifted(factor)
    centered = scan(markets, centered_surface, now, cfg)
    return raw, centered, factor, centered_surface


def _config(args, markets: pd.DataFrame | None = None, announce: bool = False) -> ScanConfig:
    """--fee-coeff 沒給時，用市場自己的 feeSchedule（即時資料才有）；離線快照沒有這個欄位就是 0。"""
    fee, source = args.fee_coeff, "--fee-coeff"
    if fee is None:
        rates = markets["fee_rate"].dropna() if markets is not None and "fee_rate" in markets else pd.Series(dtype=float)
        fee, source = (float(rates.median()), "市場的 feeSchedule") if not rates.empty else (0.0, "預設，資料裡沒有手續費資訊")
    if announce:
        print(f"taker 手續費：fee = {fee:g} × p × (1 − p)／股（{source}）")
    return ScanConfig(asset=args.asset, usdt_usd=args.usdt_usd, taker_fee_coeff=fee, min_edge=args.min_edge)


def _load_inputs(args):
    """依 --snapshot 或 --live 載入 (估值時間, 波動率曲面, Polymarket 市場)。"""
    if args.snapshot:
        snap = Path(args.snapshot)
        meta = json.loads((snap / "meta.json").read_text(encoding="utf-8"))
        now = parse_time(meta["valuation_time_utc"])
        chain = pd.read_csv(snap / f"deribit_{args.asset.lower()}_options.csv")
        surface = VolSurface.from_deribit(chain, now)
        markets = load_snapshot(snap / "polymarket_markets.csv")
        print(f"離線快照：{snap}（估值時間 {now:%Y-%m-%d %H:%M:%S} UTC）")
        for c in meta.get("caveats", []):
            print(f"  ⚠ {c}")
        return now, surface, markets
    from .live import fetch_snapshot
    snap_live = fetch_snapshot(args.asset, args.slug or None, args.days, args.with_depth, verbose=True)
    print(f"即時資料：{snap_live.now:%Y-%m-%d %H:%M:%S} UTC，{args.asset} 指數 {snap_live.index_price:,.2f}，"
          f"兩邊抓取時間差 {snap_live.sync_gap_sec:.1f} 秒")
    return snap_live.now, snap_live.surface, snap_live.markets


def cmd_scan(args) -> int:
    now, surface, markets = _load_inputs(args)
    cfg = _config(args, markets, announce=True)

    print("\n期權波動率曲面（每個到期日）：")
    print(surface.describe().to_string(index=False))

    raw, centered, factor, centered_surface = _scan_both(markets, surface, now, cfg)
    _print_table(raw, "原始比較：fair = 期權推算的公允機率；edge = 每股可捕捉的錯價（美元）")
    if factor is not None:
        shift = surface.spot * (factor - 1)
        print(f"\nPolymarket 到期價格盤隱含的中心價格，比期權 forward 高 {shift:+,.0f} 美元（{(factor-1)*100:+.2f}%）。")
        print("如果這是兩份資料不同步造成的，就該先對齊中心再比較：")
        _print_table(centered, "中心價格對齊後")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        raw.to_csv(out / "scan_raw.csv", index=False)
        if centered is not None:
            centered.to_csv(out / "scan_centered.csv", index=False)
        if args.plot and centered is not None:
            from .report import plot_snapshot
            above = markets[markets["question"].str.contains("above", case=False)]
            end = parse_time(above["end_date"].iloc[0]) if not above.empty else now
            for p in plot_snapshot(raw, centered, surface, centered_surface, now, end, out):
                print(f"圖已存到 {p}")
        print(f"結果已存到 {out}/")
    return 0


def _pairs_with_model(markets, surface, now, cfg, factor):
    """找出所有組合；近似套利用「期權的微笑形狀 + Polymarket 自己的中心價格」來估計缺口機率。"""
    pairs = find_pairs(markets, now, cfg)
    model_surface = surface.shifted(factor) if factor else surface
    return pairs, evaluate_near_arbs(pairs, model_surface), model_surface


def _fmt_gap(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["gap"] = [f"{lo:,.0f}–{hi:,.0f}" if pd.notna(lo) else "" for lo, hi in zip(df["gap_lo"], df["gap_hi"])]
    return df


def cmd_check(args) -> int:
    now, surface, markets = _load_inputs(args)
    cfg = _config(args, markets, announce=True)
    factor = fit_center(markets, surface, now, cfg)
    touch_factor = fit_center(markets, surface, now, cfg, band=(0.01, 0.99), kinds=("touch_up", "touch_down"))
    pairs, near, model_surface = _pairs_with_model(markets, surface, now, cfg, factor)
    opts = ("display.width", 200, "display.max_columns", 20)

    # 先看兩組市場「以為」現在 BTC 在哪裡：用期權的微笑形狀，各自反推中心價格
    print(f"\n中心價格：期權 {surface.spot:,.0f}", end="")
    for name, f in (("到期盤", factor), ("觸價盤", touch_factor)):
        if f:
            print(f"；{name} {surface.spot * f:,.0f}（{(f - 1) * 100:+.2f}%）", end="")
    print()
    if factor and touch_factor and abs(touch_factor / factor - 1) > 0.0025:
        print(f"⚠ 兩組 Polymarket 市場隱含的中心價格差了 {(touch_factor / factor - 1) * 100:+.2f}%。"
              "如果兩組報價不是同一時間抓的，下面的近似套利可能只是時間差造成的。")

    mf = pairs[pairs["model_free"]]
    print("\n=== ① 無模型的關係：這些兩腳組合不管發生什麼都至少付 $1，成本 < $1 就是套利 ===")
    viol = mf[mf["slack"] > 0]
    with pd.option_context(*opts):
        if viol.empty:
            print(f"共檢查 {len(mf)} 組，沒有任何一組的成本低於 $1。最接近的 5 組：")
            print(mf.head(5)[["relation", "pair", "p1", "p2", "cost_fee", "slack"]].to_string(index=False))
        else:
            print(viol[["relation", "pair", "p1", "p2", "cost_fee", "slack"]].to_string(index=False))

    print("\n=== ② 近似套利：觸價盤 + 到期盤，履約價沒對齊，只有掉進「缺口」才兩腳都歸零 ===")
    print(f"模型：單一波動率的對數常態，現貨用 Polymarket 到期盤隱含的中心價格 {model_surface.spot:,.0f}，"
          f"波動率取期權曲面在觸價價位的值。")
    show = _fmt_gap(near[(~near["model_free"]) & (near["slack"] > 0)]).sort_values("ev", ascending=False)
    with pd.option_context(*opts):
        if show.empty:
            print("（沒有成本低於 $1 的近似套利組合）")
        else:
            print(show.head(args.top)[["pair", "p1", "p2", "cost_fee", "slack", "gap", "sigma", "p_gap", "ev"]]
                  .to_string(index=False))
    print("\n欄位：cost_fee = 兩腳成本（含手續費模型）；slack = 1 − cost_fee；gap = 缺口；"
          "p_gap = 掉進缺口、全部虧掉的機率；ev = 每組的期望報酬（美元）。")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        public_columns(pairs).to_csv(out / "pairs_all.csv", index=False)
        public_columns(_fmt_gap(near)).to_csv(out / "pairs_touch_vs_terminal.csv", index=False)
        if args.plot and not show.empty:
            from .report import plot_pair_payoff
            print(f"圖已存到 {plot_pair_payoff(show.iloc[0].to_dict(), out)}")
        print(f"結果已存到 {out}/")
    return 0


def cmd_collect(args) -> int:
    from .live import fetch_snapshot

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    n = 0
    while time.time() < deadline:
        started = time.time()
        try:
            snap = fetch_snapshot(args.asset, args.slug or None, args.days, args.with_depth)
            cfg = _config(args, snap.markets)
            raw, centered, factor, _ = _scan_both(snap.markets, snap.surface, snap.now, cfg)
            if not raw.empty:
                raw.insert(0, "ts_utc", snap.now.isoformat())
                raw.insert(1, "index_price", snap.index_price)
                raw.insert(2, "sync_gap_sec", round(snap.sync_gap_sec, 2))
                raw["center_factor"] = factor
                if centered is not None:
                    raw["fair_centered"] = centered["fair"].to_numpy()
                    raw["best_edge_centered"] = centered["best_edge"].to_numpy()
                    raw["best_edge_robust_centered"] = centered["best_edge_robust"].to_numpy()
                raw.to_csv(out, mode="a", header=not out.exists(), index=False)
            # Polymarket 內部一致性：觸價 vs 到期的組合全記，單調性只記接近邊界的
            pairs, near, _ = _pairs_with_model(snap.markets, snap.surface, snap.now, cfg, factor)
            mono = pairs[(pairs["relation"] != "觸價 vs 到期") & (pairs["slack"] > -0.005)]
            log = public_columns(pd.concat([near, mono], ignore_index=True))
            if not log.empty:
                touch_factor = fit_center(snap.markets, snap.surface, snap.now, cfg, band=(0.01, 0.99),
                                          kinds=("touch_up", "touch_down"))
                log.insert(0, "ts_utc", snap.now.isoformat())
                log.insert(1, "index_price", snap.index_price)
                log.insert(2, "center_above", factor)
                log.insert(3, "center_touch", touch_factor)
                pairs_out = _pairs_path(out)
                log.to_csv(pairs_out, mode="a", header=not pairs_out.exists(), index=False)
            n += 1
            flagged = int((raw.get("signal", pd.Series(dtype=str)) != "").sum()) if not raw.empty else 0
            print(f"[{snap.now:%H:%M:%S}] 第 {n} 次：{len(raw)} 個市場，{flagged} 個超過門檻，"
                  f"時間差 {snap.sync_gap_sec:.1f}s")
        except Exception as e:  # 收集器要能撐過偶發的網路錯誤
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] 這一輪失敗：{e}", file=sys.stderr)
        time.sleep(max(0.0, args.interval - (time.time() - started)))
    print(f"收集完成，共 {n} 輪，資料在 {out}")
    return 0


def cmd_backtest(args) -> int:
    from . import history as H
    from .report import plot_reliability

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.from_csv:
        df = H.add_model(pd.read_csv(args.from_csv), args.vol_scale)
    else:
        from datetime import date
        horizons = tuple(float(x) for x in args.horizons.split(","))
        f = H.Fetcher(cache_dir=Path(args.cache) if args.cache else None, verbose=args.verbose)
        df = H.collect_history(date.fromisoformat(args.start), date.fromisoformat(args.end), horizons, args.asset, f)
        if df.empty:
            print("沒有收集到任何已結算的市場。請加 --verbose 檢查 API 回應。")
            return 1
        df = H.add_model(df, args.vol_scale)
        df.to_csv(out / "history.csv", index=False)
        print(f"原始資料已存到 {out / 'history.csv'}（之後可用 --from-csv 重新分析，不必再連網）")

    for col, candle in (("rule_check", "11:59 開盤、12:00 收盤"), ("rule_check_alt", "12:00 開盤")):
        if col in df and df[col].notna().any():
            mk = df[df[col].notna()].drop_duplicates(["day", "strike"])
            bad = int((mk[col].astype(str) == "False").sum())
            print(f"結算驗證（{candle}的 K 線）：{len(mk)} 個市場中，自己算的結果和 Polymarket 結算不一致的有 {bad} 個。")

    u = H.usable(df, args.max_age_min)
    print(f"可用樣本：{len(u)} 筆（{u['day'].nunique()} 天）；價格超過 {args.max_age_min:.0f} 分鐘沒更新的已排除。")
    if u.empty:
        return 1
    sc = H.scores(u)
    print("\n=== 準確度（越小越準）：brier_diff > 0 代表 Polymarket 比模型差 ===")
    print(sc.to_string(index=False))
    for h, g in u.groupby("horizon_h"):
        by_pm = H.calibration_table(g, "pm_price")
        by_model = H.calibration_table(g, "model_prob")
        print(f"\n=== 結算前 {h:g} 小時：依 Polymarket 價格分組 ===")
        print(by_pm.to_string(index=False))
        by_pm.to_csv(out / f"calibration_pm_{h:g}h.csv", index=False)
        by_model.to_csv(out / f"calibration_model_{h:g}h.csv", index=False)
        p = plot_reliability(by_pm, by_model, out / f"{h:g}h", f"結算前 {h:g} 小時，{len(g)} 筆、{g['day'].nunique()} 天")
        print(f"圖已存到 {p}")
    st = H.strategy(u, args.threshold, args.half_spread, args.fee_coeff)
    print(f"\n=== 簡單策略：價差超過 {args.threshold} 就交易，每股扣 {args.half_spread} 的半價差"
          f"與 {args.fee_coeff:g}·p·(1−p) 的手續費 ===")
    print(st.to_string(index=False) if not st.empty else "（沒有觸發任何交易）")
    sc.to_csv(out / "scores.csv", index=False)
    st.to_csv(out / "strategy.csv", index=False)
    return 0


def _pairs_path(scans_csv: Path) -> Path:
    """collect 把組合記錄存在 scans.csv 旁邊的 scans_pairs.csv。"""
    return scans_csv.with_name(scans_csv.stem + "_pairs.csv")


def cmd_summarize(args) -> int:
    from .report import summarize_collected, summarize_pairs
    summary = summarize_collected(Path(args.csv), Path(args.out), args.min_edge)
    print(summary.to_string())
    pairs_csv = _pairs_path(Path(args.csv))
    if pairs_csv.exists():
        ps = summarize_pairs(pairs_csv, Path(args.out))
        print("\nPolymarket 內部一致性（成本低於 $1 的時間比例最高的組合）：")
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(ps.head(15).to_string())
    print(f"\n圖表與表格已存到 {args.out}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pmopt", description="用期權隱含機率檢查 Polymarket 加密貨幣價格盤")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--asset", default="BTC", choices=["BTC", "ETH"])
        sp.add_argument("--usdt-usd", type=float, default=1.0, help="1 USDT 值多少美元（預設 1.0）")
        sp.add_argument("--fee-coeff", type=float, default=None,
                        help="taker 手續費係數 c：fee = c·p·(1-p)。不給就用市場的 feeSchedule（離線快照為 0）")
        sp.add_argument("--min-edge", type=float, default=0.01, help="標示訊號的 edge 門檻（美元／股）")
        sp.add_argument("--slug", action="append", help="指定 Polymarket 事件 slug（可重複）；不給就自動猜")
        sp.add_argument("--days", type=int, default=7, help="自動猜 slug 時往後看幾天")
        sp.add_argument("--with-depth", action="store_true", help="改用 CLOB 訂單簿的最佳價與掛單量")

    sp = sub.add_parser("scan", help="掃描一次")
    common(sp)
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--snapshot", help="離線快照資料夾")
    src.add_argument("--live", action="store_true", help="即時抓資料")
    sp.add_argument("--out", help="把結果 CSV（和圖）存到這個資料夾")
    sp.add_argument("--plot", action="store_true", help="同時畫圖（需要 --out）")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("check", help="Polymarket 內部一致性：觸價盤 vs 到期盤、單調性")
    common(sp)
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--snapshot", help="離線快照資料夾")
    src.add_argument("--live", action="store_true", help="即時抓資料")
    sp.add_argument("--out", help="把結果 CSV 存到這個資料夾")
    sp.add_argument("--top", type=int, default=8, help="近似套利顯示幾組")
    sp.add_argument("--plot", action="store_true", help="畫出期望值最高那一組的報酬圖（需要 --out）")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("collect", help="定時收集，存成 CSV")
    common(sp)
    sp.add_argument("--interval", type=float, default=60, help="每幾秒收一次")
    sp.add_argument("--minutes", type=float, default=60, help="總共收幾分鐘")
    sp.add_argument("--out", default="data/live/scans.csv")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("backtest", help="歷史回測：已結算的每日盤，Polymarket 和模型誰比較準")
    sp.add_argument("--asset", default="BTC", choices=["BTC", "ETH"])
    sp.add_argument("--start", help="開始日期（美東時間），例如 2026-09-01")
    sp.add_argument("--end", help="結束日期（含）")
    sp.add_argument("--horizons", default="24,6", help="結算前幾小時的價格，逗號分隔")
    sp.add_argument("--cache", default="data/cache", help="API 回應的快取資料夾（空字串 = 不快取）")
    sp.add_argument("--from-csv", help="不連網，直接分析之前存下的 history.csv")
    sp.add_argument("--out", default="results/backtest")
    sp.add_argument("--vol-scale", type=float, default=1.0, help="模型波動率 = DVOL × 這個倍數")
    sp.add_argument("--threshold", type=float, default=0.05, help="策略的交易門檻（機率差）")
    sp.add_argument("--half-spread", type=float, default=0.01, help="每股扣多少當作買賣價差成本")
    sp.add_argument("--fee-coeff", type=float, default=0.07, help="taker 手續費係數（加密貨幣盤官方為 0.07）")
    sp.add_argument("--max-age-min", type=float, default=180, help="價格最多可以多舊（分鐘）")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("summarize", help="彙整 collect 收到的資料")
    sp.add_argument("csv")
    sp.add_argument("--out", default="results/summary")
    sp.add_argument("--min-edge", type=float, default=0.01)
    sp.set_defaults(func=cmd_summarize)

    args = p.parse_args(argv)
    if args.cmd == "backtest" and not args.from_csv and not (args.start and args.end):
        p.error("backtest 需要 --start 和 --end，或是 --from-csv")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
