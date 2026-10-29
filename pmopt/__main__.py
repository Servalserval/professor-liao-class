"""命令列介面。

    python -m pmopt scan --snapshot data/snapshot_2026-10-29     # 離線示範（課堂用）
    python -m pmopt scan --live                                  # 即時抓資料掃一次
    python -m pmopt collect --minutes 120 --interval 60          # 每分鐘收集一次，存成 CSV
    python -m pmopt summarize data/live/scans.csv                # 彙整收集到的資料
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .polymarket import load_snapshot, parse_time
from .scanner import ScanConfig, fit_center, scan
from .surface import VolSurface

SHOW = ["market", "end_utc", "hours_left", "deribit_vol", "pm_iv", "fair", "bid", "ask", "best_edge", "signal"]


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


def cmd_scan(args) -> int:
    cfg = ScanConfig(asset=args.asset, usdt_usd=args.usdt_usd, taker_fee_coeff=args.fee_coeff, min_edge=args.min_edge)
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
    else:
        from .live import fetch_snapshot
        snap_live = fetch_snapshot(args.asset, args.slug or None, args.days, args.with_depth, verbose=True)
        now, surface, markets = snap_live.now, snap_live.surface, snap_live.markets
        print(f"即時資料：{now:%Y-%m-%d %H:%M:%S} UTC，{args.asset} 指數 {snap_live.index_price:,.2f}，"
              f"兩邊抓取時間差 {snap_live.sync_gap_sec:.1f} 秒")

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


def cmd_collect(args) -> int:
    from .live import fetch_snapshot

    cfg = ScanConfig(asset=args.asset, usdt_usd=args.usdt_usd, taker_fee_coeff=args.fee_coeff, min_edge=args.min_edge)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    n = 0
    while time.time() < deadline:
        started = time.time()
        try:
            snap = fetch_snapshot(args.asset, args.slug or None, args.days, args.with_depth)
            raw, centered, factor, _ = _scan_both(snap.markets, snap.surface, snap.now, cfg)
            if not raw.empty:
                raw.insert(0, "ts_utc", snap.now.isoformat())
                raw.insert(1, "index_price", snap.index_price)
                raw.insert(2, "sync_gap_sec", round(snap.sync_gap_sec, 2))
                raw["center_factor"] = factor
                if centered is not None:
                    raw["fair_centered"] = centered["fair"].to_numpy()
                    raw["best_edge_centered"] = centered["best_edge"].to_numpy()
                raw.to_csv(out, mode="a", header=not out.exists(), index=False)
            n += 1
            flagged = int((raw.get("signal", pd.Series(dtype=str)) != "").sum()) if not raw.empty else 0
            print(f"[{snap.now:%H:%M:%S}] 第 {n} 次：{len(raw)} 個市場，{flagged} 個超過門檻，"
                  f"時間差 {snap.sync_gap_sec:.1f}s")
        except Exception as e:  # 收集器要能撐過偶發的網路錯誤
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] 這一輪失敗：{e}", file=sys.stderr)
        time.sleep(max(0.0, args.interval - (time.time() - started)))
    print(f"收集完成，共 {n} 輪，資料在 {out}")
    return 0


def cmd_summarize(args) -> int:
    from .report import summarize_collected
    summary = summarize_collected(Path(args.csv), Path(args.out), args.min_edge)
    print(summary.to_string())
    print(f"\n圖表與表格已存到 {args.out}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pmopt", description="用期權隱含機率檢查 Polymarket 加密貨幣價格盤")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--asset", default="BTC", choices=["BTC", "ETH"])
        sp.add_argument("--usdt-usd", type=float, default=1.0, help="1 USDT 值多少美元（預設 1.0）")
        sp.add_argument("--fee-coeff", type=float, default=0.0, help="taker 手續費模型係數：fee = c·p·(1-p)")
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

    sp = sub.add_parser("collect", help="定時收集，存成 CSV")
    common(sp)
    sp.add_argument("--interval", type=float, default=60, help="每幾秒收一次")
    sp.add_argument("--minutes", type=float, default=60, help="總共收幾分鐘")
    sp.add_argument("--out", default="data/live/scans.csv")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("summarize", help="彙整 collect 收到的資料")
    sp.add_argument("csv")
    sp.add_argument("--out", default="results/summary")
    sp.add_argument("--min-edge", type=float, default=0.01)
    sp.set_defaults(func=cmd_summarize)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
