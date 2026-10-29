"""掛單系統的命令列：

    python -m pmopt mm-plan --budget 350000                 # 現在會選哪些市場、各配多少（不下單）
    python -m pmopt mm-paper --minutes 60                   # 模擬：用即時公開資料跑，不下任何單
    python -m pmopt mm-report data/mm_logs/paper-...        # 彙整一次執行，對照上線檢查表
    python -m pmopt mm-live --budget 10000 --confirm-live   # 實盤（只在自己的電腦上、用自己的錢包）
    python -m pmopt mm-earnings --days 7                    # 實盤：查官方記錄的實際獎勵與返佣

實盤需要 pip install "pmopt[live]"，以及環境變數 POLYMARKET_PRIVATE_KEY（和選填的 POLYMARKET_WALLET_ADDRESS）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import pandas as pd

from .config import MMConfig


def _cfg(args) -> MMConfig:
    over = dict(budget=args.budget, max_markets=args.max_markets)
    if getattr(args, "exclude", None):
        over["exclude_categories"] = args.exclude
    if getattr(args, "log_dir", None):
        over["log_dir"] = args.log_dir
    return MMConfig.load(args.config, **over)


def _logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_plan(args) -> int:
    from .feeds import PublicData
    from .universe import build_universe
    cfg = _cfg(args)
    t0 = time.time()
    infos, plan, mult = build_universe(cfg, PublicData(), t0)
    if plan.empty:
        print("沒有符合條件的市場")
        return 1
    plan = plan.copy()
    plan["exp_cost"] = plan["exp_reward"] - plan["exp_net"]
    by = plan.groupby("category").agg(markets=("capital", "size"), capital=("capital", "sum"),
                                      reward_per_day=("exp_reward", "sum"), net_per_day=("exp_net", "sum"))
    by.loc["合計"] = by.sum()
    by["apr"] = (by["net_per_day"] * 365 / by["capital"]).map("{:.0%}".format)
    print(f"預算 {cfg.budget:,.0f}、門檻倍數 {mult:.2f}（設定 {cfg.hurdle_mult}），{time.time() - t0:.0f} 秒")
    with pd.option_context("display.width", 200, "display.float_format", "{:,.2f}".format):
        print(by.to_string())
        print("\n前 15 個市場：")
        print(plan.head(15)[["question", "category", "rate", "capital", "exp_reward", "exp_net"]].to_string(index=False))
    print("\n註：預期淨額已扣掉「門檻倍數 × 回測的成交虧損」和資金成本，是保守估計；真實虧損要靠模擬與小額實盤確認。")
    if args.out:
        plan.to_csv(args.out, index=False)
        print(f"配置表存到 {args.out}")
    return 0


def _run(cfg: MMConfig, mode: str, minutes, **kw) -> int:
    from .runner import Runner
    runner = Runner(cfg, mode=mode, backfill_hours=kw.pop("backfill_hours", 2.0))
    print(f"記錄寫在 {runner.dir}")
    try:
        reason = asyncio.run(runner.run(minutes=minutes, **kw))
    except KeyboardInterrupt:
        reason = "手動停止"
    print(f"結束：{reason}。彙整：python -m pmopt mm-report {runner.dir}")
    return 0


def cmd_paper(args) -> int:
    _logging(args.verbose)
    cfg = _cfg(args)
    return _run(cfg, "paper", args.minutes, backfill_hours=args.backfill_hours)


def _secrets() -> tuple[str, str | None, tuple[str, str] | None]:
    """私鑰只從環境變數讀（不要寫在指令、設定檔或記錄裡）。"""
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        sys.exit("需要環境變數 POLYMARKET_PRIVATE_KEY（不要寫在指令或設定檔裡）")
    rk, ra = os.environ.get("POLYMARKET_RELAYER_API_KEY"), os.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS")
    return key, os.environ.get("POLYMARKET_WALLET_ADDRESS") or None, ((rk, ra) if rk and ra else None)


def cmd_live(args) -> int:
    if not args.confirm_live:
        sys.exit("實盤會用真的錢下單。確定的話加上 --confirm-live")
    if args.budget is None:
        sys.exit("實盤必須明確給 --budget（建議從 10000 開始）")
    try:
        import polymarket  # noqa: F401
    except ImportError:
        sys.exit('實盤需要官方 SDK：pip install "pmopt[live]"（或 pip install polymarket-client）')
    _logging(args.verbose)
    cfg = _cfg(args)
    key, wallet, relayer = _secrets()
    if cfg.auto_merge and relayer is None:
        sys.exit("auto_merge 需要 Relayer API key（POLYMARKET_RELAYER_API_KEY 和 POLYMARKET_RELAYER_API_KEY_ADDRESS）")
    print(f"實盤：預算 {cfg.budget:,.0f} 美元、單一市場上限 {cfg.market_cap:,.0f}、單日總虧損上限 "
          f"{cfg.max_daily_loss_frac * cfg.budget:,.0f}（{cfg.max_daily_loss_frac:.1%}）")
    print("啟動時會先撤掉這個帳號上所有的掛單；請用專門的錢包跑，不要和手動交易混用。")
    if not args.yes:
        typed = input(f"輸入預算數字 {int(cfg.budget)} 確認開始：").strip()
        if typed != str(int(cfg.budget)):
            sys.exit("沒有確認，結束")
    return _run(cfg, "live", args.minutes, private_key=key, wallet=wallet, relayer=relayer,
                backfill_hours=args.backfill_hours)


def cmd_report(args) -> int:
    from .report import print_report, summarize
    print_report(summarize(args.run_dir), args.stage, args.actual_reward)
    return 0


def cmd_earnings(args) -> int:
    """官方記錄的實際獎勵與返佣（需要私鑰，只讀不下單）。"""
    try:
        from polymarket import AsyncSecureClient
    except ImportError:
        sys.exit('需要官方 SDK：pip install "pmopt[live]"')
    key, wallet, _ = _secrets()

    async def go():
        client = await AsyncSecureClient.create(private_key=key, wallet=wallet)
        try:
            me = str(client.wallet)
            rows = []
            for i in range(args.days):
                day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400 * (i + 1)))
                total = await client.get_total_earnings_for_user_for_day(date=day, sponsored=True)
                rebates = await client.get_current_rebates(date=day, maker_address=me)
                rows.append(dict(date=day, rewards=float(sum(t.earnings for t in total)),
                                 rebates=float(sum(r.rebated_fees_usdc for r in rebates))))
            return pd.DataFrame(rows)
        finally:
            if hasattr(client, "close"):
                await client.close()

    df = asyncio.run(go())
    df.loc["合計"] = ["", df["rewards"].sum(), df["rebates"].sum()]
    print(df.to_string())
    return 0


def add_parsers(sub) -> None:
    def common(sp):
        sp.add_argument("--config", help="JSON 設定檔（覆蓋 MMConfig 的預設值）")
        sp.add_argument("--budget", type=float, default=None, help="總本金上限（美元）")
        sp.add_argument("--max-markets", type=int, default=None, help="最多掛幾個市場（分階段上線用）")
        sp.add_argument("--exclude", action="append", help="排除的類別，例如 --exclude 其他")

    sp = sub.add_parser("mm-plan", help="掛單：現在會選哪些市場、各配多少本金（不下單）")
    common(sp)
    sp.add_argument("--out", help="配置表存成 CSV")
    sp.set_defaults(func=cmd_plan)

    for name, func, helptext in (("mm-paper", cmd_paper, "掛單：模擬（即時公開資料，不下單）"),
                                 ("mm-live", cmd_live, "掛單：實盤（需要私鑰與 --confirm-live）")):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.add_argument("--minutes", type=float, default=None, help="跑幾分鐘（不給就一直跑，Ctrl-C 停止並撤單）")
        sp.add_argument("--backfill-hours", type=float, default=2.0, help="啟動時回補幾小時的公開成交")
        sp.add_argument("--log-dir", help="記錄資料夾（預設 data/mm_logs）")
        sp.add_argument("--verbose", action="store_true")
        if name == "mm-live":
            sp.add_argument("--confirm-live", action="store_true", help="確認要用真的錢下單")
            sp.add_argument("--yes", action="store_true", help="不要再互動確認一次（例如用 systemd 跑）")
        sp.set_defaults(func=func)

    sp = sub.add_parser("mm-report", help="掛單：彙整一次執行的記錄，對照上線檢查表")
    sp.add_argument("run_dir")
    sp.add_argument("--stage", choices=["paper", "live-10k", "live-100k"], help="對照哪一階段的通過條件")
    sp.add_argument("--actual-reward", type=float, default=None, help="同期間官方記錄的實際獎勵（mm-earnings 查）")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("mm-earnings", help="掛單：查官方記錄的實際獎勵與返佣（需要私鑰，只讀）")
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(func=cmd_earnings)
