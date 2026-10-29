"""主迴圈：更新資料 → 判斷規則 → 和現有掛單比對 → 撤單／下單 → 記錄。

    啟動：連交易所（實盤會先撤掉帳號上所有的單、讀餘額與部位）→ 選市場、配本金 → 回補最近的成交
    每 tick_seconds 秒：批次訂單簿、新成交 →（模擬）比對成交 → 自己的成交 → 風控
                     → 每個市場 decide() → target_quotes() → diff_orders() → 撤單、下單
    每 universe_refresh_min 分鐘：重新選市場、配本金、（實盤）對帳。
離開名單但還有庫存的市場只掛減倉那一邊。結束時一律全部撤單。
記錄在 log_dir/<模式>-<開始時間>/：events.jsonl（每一筆動作）、minutes.csv（每分鐘彙整）、
markets.csv 與 summary.json（結束時的結算）。
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import random
import time
from collections import Counter, deque
from dataclasses import asdict, replace
from pathlib import Path

from . import risk
from .config import MMConfig
from .exchange import LiveExchange, PaperExchange, _yes_level
from .feeds import PublicData, TradePoller
from .quotes import diff_orders, target_quotes
from .rules import Decision, MarketState, decide, without_own
from .universe import build_universe

log = logging.getLogger("pmopt.mm")


class Runner:
    def __init__(self, cfg: MMConfig, mode: str = "paper", data: PublicData | None = None, exchange=None,
                 log_dir: str | Path | None = None, backfill_hours: float = 2.0):
        assert mode in ("paper", "live")
        self.cfg, self.mode = cfg, mode
        self.data = data or PublicData()
        self.exchange = exchange
        self.states: dict[str, MarketState] = {}
        self.retired: dict[str, MarketState] = {}      # 已離開名單、但部位還在的市場
        self.backfill_hours = backfill_hours
        self.errors: deque = deque(maxlen=1000)        # 主迴圈自己遇到的錯誤（時間）
        self._started = False
        self._own: dict[str, list] = {}               # Yes token → 自己的掛單 [(leg, Yes 價格, 股數)]（實盤扣掉用）
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.dir = Path(log_dir or cfg.log_dir) / f"{mode}-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._minutes = open(self.dir / "minutes.csv", "a", newline="", encoding="utf-8")
        self._mw = None
        self._last_minute = None
        self._reasons: Counter = Counter()
        self.stop_reason: str | None = None
        (self.dir / "config.json").write_text(cfg.to_json(), encoding="utf-8")

    # ---------- 記錄 ----------
    def event(self, kind: str, **kw) -> None:
        self._events.write(json.dumps(dict(time=round(time.time(), 3), kind=kind, **kw), ensure_ascii=False, default=str) + "\n")
        self._events.flush()

    def everyone(self) -> dict[str, MarketState]:
        return {**self.retired, **self.states}

    def _minute_summary(self, now: float, open_orders: int, quoting: int, tick_s: float = 0.0) -> None:
        m = int(now // 60)
        if m == self._last_minute:
            return
        self._last_minute = m
        everyone = self.everyone()
        row = dict(ts=int(now), markets=len(self.states), quoting=quoting, open_orders=open_orders,
                   capital=round(sum(st.info.capital for st in self.states.values()), 2),
                   mtm_pnl=round(sum(st.value() for st in everyone.values()), 2),
                   est_reward=round(sum(st.reward_est for st in everyone.values()), 2),
                   rebate=round(sum(st.rebate for st in everyone.values()), 2),
                   inventory=round(sum(abs(st.inventory_value()) for st in everyone.values()), 2),
                   errors=len(self._recent_errors(now)), tick_s=round(tick_s, 2),
                   reasons=json.dumps(dict(self._reasons.most_common(8)), ensure_ascii=False))
        if self._mw is None:
            self._mw = csv.DictWriter(self._minutes, fieldnames=list(row))
            self._mw.writeheader()
        self._mw.writerow(row)
        self._minutes.flush()
        self._reasons.clear()
        log.info("[%s] 市場 %d、兩邊都掛 %d、掛單 %d 張、市值損益 %.2f、估計獎勵 %.2f、返佣 %.2f（這一輪 %.1f 秒）",
                 time.strftime("%H:%M", time.gmtime(now)), row["markets"], quoting, open_orders,
                 row["mtm_pnl"], row["est_reward"], row["rebate"], tick_s)

    def _recent_errors(self, now: float) -> list[float]:
        rejections = getattr(self.exchange, "rejections", [])
        return [t for t in [*self.data.errors, *self.errors, *(r[0] for r in rejections)] if now - t < 60]

    # ---------- 市場名單 ----------
    def refresh_universe(self, now: float, budget: float | None = None) -> None:
        cfg = replace(self.cfg, budget=budget) if budget is not None else self.cfg
        infos, plan, mult = build_universe(cfg, self.data, now, incumbents=frozenset(self.states),
                                           own=self._own if self.mode == "live" else None)
        plan.to_csv(self.dir / f"plan-{int(now)}.csv", index=False)
        self.event("universe", markets=len(infos), capital=round(sum(i.capital for i in infos), 2),
                   budget=cfg.budget, hurdle_mult=round(mult, 3))
        new = {i.condition_id: i for i in infos}
        for cid in [c for c in self.states if c not in new]:
            self.retired[cid] = self.states.pop(cid)
        for cid, info in new.items():
            if cid in self.states:
                self.states[cid].info = info
            elif cid in self.retired:
                self.states[cid] = self.retired.pop(cid)
                self.states[cid].info = info
            else:
                # 啟動時會回補 backfill_hours 的成交；之後才加入的市場沒有回補，成交量從現在開始算
                since = now - self.backfill_hours * 3600 if not self._started else now
                self.states[cid] = MarketState(info=info, cfg=self.cfg, first_seen=now, vol_since=since)
        # 沒有庫存的舊市場就不用再追蹤
        for cid in [c for c, st in self.retired.items() if abs(st.yes) < 1e-6 and abs(st.no) < 1e-6]:
            del self.retired[cid]
        if self.exchange is not None:
            self.exchange.markets = {c: s.info for c, s in self.everyone().items()}

    async def _load_positions(self) -> None:
        """實盤：把交易所上既有的部位放進狀態（現金記 0，損益從啟動那一刻算起）。"""
        pos = await self.exchange.positions()
        for (cid, token), size in pos.items():
            st = self.states.get(cid)
            if st is None:
                continue
            if token == st.info.yes_token:
                st.yes = size
            elif token == st.info.no_token:
                st.no = size
        if pos:
            self.event("positions_loaded", n=len(pos),
                       in_universe=sum(1 for (c, _) in pos if c in self.states))

    async def _reconcile(self) -> None:
        pos = await self.exchange.positions()
        diffs = []
        for cid, st in self.everyone().items():
            for token, local in ((st.info.yes_token, st.yes), (st.info.no_token, st.no)):
                remote = pos.get((cid, token), 0.0)
                if abs(remote - local) > 1.0:
                    diffs.append(dict(condition_id=cid, token=token, local=round(local, 2), exchange=round(remote, 2)))
        self.event("reconcile", diffs=diffs[:50], n_diffs=len(diffs))
        if diffs:
            log.warning("對帳：%d 個部位和交易所不一致（見 events.jsonl）", len(diffs))
        orders = await self.exchange.open_orders()
        # 掛得夠久的單才會開始計分：只抽樣掛超過 5 分鐘的
        old = [o.id for o in orders if time.time() - o.created_at > 300]
        random.shuffle(old)
        frac = await self.exchange.scoring_fraction(old)
        if frac is not None:
            self.event("scoring", sampled=min(len(old), 200), fraction=round(frac, 3))
            log.info("抽樣 %d 張掛單，%.0f%% 正在計分", min(len(old), 200), 100 * frac)

    # ---------- 主迴圈 ----------
    async def run(self, minutes: float | None = None, private_key: str | None = None, wallet: str | None = None,
                  relayer: tuple[str, str] | None = None) -> str:
        t_start = time.time()
        if self.exchange is None:
            self.exchange = PaperExchange(self.cfg, {}) if self.mode == "paper" else \
                LiveExchange(self.cfg, {}, private_key=private_key, wallet=wallet, relayer=relayer)
        await self.exchange.start()
        try:
            budget = None
            cash = await self.exchange.collateral()
            if cash is not None:
                self.event("collateral", balance=round(cash, 2))
                if cash < self.cfg.budget:
                    budget = 0.95 * cash
                    log.warning("抵押品（pUSD）餘額 %.0f 少於預算 %.0f，改用 %.0f", cash, self.cfg.budget, budget)
            await asyncio.to_thread(self.refresh_universe, t_start, budget)
            self._started = True
            if self.mode == "live":
                await self._load_positions()
            poller = TradePoller(self.data, {c: s.info.yes_token for c, s in self.everyone().items()},
                                 lookback_s=int(self.backfill_hours * 3600))
            self.event("start", mode=self.mode, markets=len(self.states))
            next_refresh = t_start + self.cfg.universe_refresh_min * 60
            while minutes is None or time.time() - t_start < minutes * 60:
                tick = time.time()
                try:
                    if tick >= next_refresh:
                        next_refresh = tick + self.cfg.universe_refresh_min * 60
                        # 官方：可下單量 = 餘額 − 掛單佔用，所以餘額本身已經包含掛著的單
                        cash = await self.exchange.collateral()
                        budget = None if cash is None else min(self.cfg.budget, 0.95 * cash)
                        await asyncio.to_thread(self.refresh_universe, tick, budget)
                        poller.set_markets({c: s.info.yes_token for c, s in self.everyone().items()})
                        if self.mode == "live":
                            await self._reconcile()
                    status = await self.step(poller)
                except Exception as e:      # 單一輪出錯不停機；錯誤太多由風控停機
                    self.errors.append(time.time())
                    self.event("error", error=repr(e)[:500])
                    log.warning("這一輪出錯：%r", e)
                    status = risk.check(self.cfg, self.everyone(), self._recent_errors(time.time()),
                                        self.exchange.heartbeat_ok())
                if status.kill:
                    self.stop_reason = status.reason
                    self.event("kill", reason=status.reason)
                    log.error("停機：%s", status.reason)
                    break
                await asyncio.sleep(max(0.0, self.cfg.tick_seconds - (time.time() - tick)))
            else:
                self.stop_reason = "時間到"
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.stop_reason = "手動停止"
            raise
        finally:
            try:
                await self.exchange.cancel_all()
                self.event("cancel_all", reason=self.stop_reason or "結束")
            except Exception as e:
                # 實盤：就算這裡失敗，停止送心跳 10 秒後交易所也會撤掉所有的單
                self.event("cancel_all_failed", error=repr(e)[:500])
                log.error("全部撤單失敗：%r（心跳停止後交易所會自動撤單）", e)
            finally:
                await self.exchange.close()
                self._write_summary(t_start)
                self._events.close()
                self._minutes.close()
        return self.stop_reason or "結束"

    def _quoted_capital(self) -> float:
        return sum(st.info.capital for st in self.states.values())

    async def step(self, poller: TradePoller) -> risk.RiskStatus:
        now = time.time()
        everyone = self.everyone()
        tokens = [s.info.yes_token for s in everyone.values()]
        books, trades = await asyncio.gather(asyncio.to_thread(self.data.books, tokens), asyncio.to_thread(poller.poll))
        if self.mode == "live":
            # 實盤：訂單簿裡有自己的單，扣掉才是別人的報價（模擬的單不在訂單簿裡，不用扣）
            books = {t: without_own(b, self._own.get(t, [])) for t, b in books.items()}
        for cid, st in everyone.items():
            b = books.get(st.info.yes_token)
            if b is not None:
                st.update_book(b, now)
            st.add_trades(trades.get(cid, []))
        if isinstance(self.exchange, PaperExchange):
            self.exchange.on_trades(trades)
        for f in await self.exchange.fills():
            st = everyone.get(f.condition_id)
            if st is not None:
                st.apply_fill(f.token, f.side, f.price, f.size, f.ts)
                if self.cfg.auto_merge and min(st.yes, st.no) >= 1 and await self.exchange.merge(f.condition_id):
                    q = st.merge()
                    self.event("merge", condition_id=f.condition_id, shares=round(q, 2))
            self.event("fill", **asdict(f))

        status = risk.check(self.cfg, everyone, self._recent_errors(now), self.exchange.heartbeat_ok(), now)
        if status.kill:
            return status
        open_orders = await self.exchange.open_orders()
        by_market: dict[str, list] = {}
        for o in open_orders:
            by_market.setdefault(o.condition_id, []).append(o)
        to_cancel, to_place, quoting = [], [], 0
        priority: dict[str, float] = {}
        for cid, st in everyone.items():
            dec = decide(st, now, self.cfg)
            if cid in self.retired:
                # 離開名單的市場：只掛減倉那一邊，不算獎勵
                net = st.yes - st.no
                dec = Decision(dec.bid and net < 0, dec.ask and net > 0, dec.reason or "已離開名單，只減倉")
            if dec.reason:
                self._reasons[dec.reason] += 1
            desired = target_quotes(st, dec, self.cfg)
            cancel, place = diff_orders(by_market.get(cid, []), desired, st.d, now, self.cfg,
                                        tick=st.book.tick if st.book else 0.0, mid=st.mid, v=st.info.v,
                                        yes_token=st.info.yes_token)
            to_cancel += cancel
            to_place += place
            priority[cid] = st.reward_per_min
            if cid in self.retired:
                continue
            if len(desired) == 2:
                quoting += 1
                st.reward_est += st.reward_per_min * self.cfg.tick_seconds / 60
            elif len(desired) == 1 and 0.10 <= (st.mid or 0) <= 0.90:
                st.reward_est += st.reward_per_min / 3 * self.cfg.tick_seconds / 60   # 單邊只有三分之一的分數
        for cid in by_market:
            if cid not in everyone:
                to_cancel += [o.id for o in by_market[cid]]
        if to_cancel:
            await self.exchange.cancel(to_cancel)
            self.event("cancel", n=len(to_cancel))
        if to_place and not status.pause_new:
            # 一次冒出太多單（例如暖機結束）：先掛獎勵高的，其他下一輪再掛
            to_place.sort(key=lambda it: priority.get(it.condition_id, 0.0), reverse=True)
            batch, deferred = to_place[:self.cfg.max_new_orders_per_tick], len(to_place) - self.cfg.max_new_orders_per_tick
            placed = await self.exchange.place(batch, now)
            self.event("place", n=len(batch), accepted=len(placed), deferred=max(deferred, 0))
        else:
            placed = []
            if to_place:
                self.event("skip_place", n=len(to_place), reason=status.reason)
        gone = set(to_cancel)
        live_orders = [o for o in open_orders if o.id not in gone] + placed
        n_live = len(live_orders)
        self._own = {}
        for o in live_orders:
            st = everyone.get(o.condition_id)
            if st is not None:
                leg, price = _yes_level(o, st.info)
                self._own.setdefault(st.info.yes_token, []).append((leg, price, o.remaining))
        self._minute_summary(now, n_live, quoting, time.time() - now)
        return status

    def _write_summary(self, t_start: float) -> None:
        everyone = self.everyone()
        hours = max((time.time() - t_start) / 3600, 1e-9)
        rows = [dict(condition_id=c, question=s.info.question[:80], category=s.info.category, capital=round(s.info.capital, 2),
                     mtm_pnl=round(s.value(), 2), est_reward=round(s.reward_est, 2), rebate=round(s.rebate, 2),
                     yes=round(s.yes, 2), no=round(s.no, 2)) for c, s in everyone.items()]
        with open(self.dir / "markets.csv", "w", newline="", encoding="utf-8") as fh:
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        total = dict(mode=self.mode, hours=round(hours, 2), markets=len(everyone),
                     capital=round(self._quoted_capital(), 2),
                     mtm_pnl=round(sum(r["mtm_pnl"] for r in rows), 2), est_reward=round(sum(r["est_reward"] for r in rows), 2),
                     rebate=round(sum(r["rebate"] for r in rows), 2), stop_reason=self.stop_reason)
        total["net_est"] = round(total["mtm_pnl"] + total["est_reward"] + total["rebate"], 2)
        (self.dir / "summary.json").write_text(json.dumps(total, ensure_ascii=False, indent=2), encoding="utf-8")
