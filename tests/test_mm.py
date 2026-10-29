"""掛單系統（pmopt/mm）的測試：規則、報價、配置、模擬成交、實盤介面（假的 SDK）、主迴圈。"""

import asyncio
import json
import sys
import time
import types
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pmopt.mm.config import MMConfig
from pmopt.mm.exchange import LiveExchange, PaperExchange
from pmopt.mm.feeds import TradePoller
from pmopt.mm.quotes import Order, OrderIntent, diff_orders, target_quotes
from pmopt.mm.report import gate, summarize
from pmopt.mm.rules import Book, Decision, MarketInfo, MarketState, Trade, decide, minute_of
from pmopt.mm.runner import Runner
from pmopt.mm.universe import Candidate, allocate, best_capital, candidates, competitor_score, solve_budget

T0 = 1_800_000_000          # 剛好是整分鐘
MIN = 60


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cfg_(**kw):
    base = dict(warmup_min=0, freeze_windows=[])
    base.update(kw)
    return MMConfig(**base)


def info_(**kw):
    base = dict(condition_id="c1", question="Will X happen?", slug="x", category="其他", yes_token="Y", no_token="N",
                rate=100.0, v=0.045, reward_min_size=50.0, comp=1000.0, capital=1000.0)
    base.update(kw)
    return MarketInfo(**base)


def book_(bid=0.49, ask=0.51, size=500.0, ts=T0, tick=0.01, extra_bids=(), extra_asks=()):
    return Book(ts=ts, bids=((bid, size),) + tuple(extra_bids), asks=((ask, size),) + tuple(extra_asks),
                tick=tick, min_order_size=5.0)


def state_(cfg=None, info=None, first_seen=T0 - 3600, vol_since=T0 - 3600):
    cfg = cfg or cfg_()
    return MarketState(info=info or info_(), cfg=cfg, first_seen=first_seen, vol_since=vol_since)


# ---------------------------------------------------------------- 規則
def test_all_clear_quotes_both_sides():
    st = state_()
    st.update_book(book_(), T0)
    assert decide(st, T0, st.cfg) == Decision(True, True, None)


@pytest.mark.parametrize("setup, reason", [
    (dict(cfg=dict(warmup_min=30), first_seen=T0), "暖機中"),
    (dict(info=dict(end_ts=T0 - 1)), "已過（或接近）截止日"),
    (dict(info=dict(game_start_ts=T0 + 10 * MIN)), "比賽即將開始"),
])
def test_blocking_rules(setup, reason):
    st = state_(cfg=cfg_(**setup.get("cfg", {})), info=info_(**setup.get("info", {})),
                first_seen=setup.get("first_seen", T0 - 3600))
    st.update_book(book_(), T0)
    assert decide(st, T0, st.cfg).reason == reason


def test_stale_book():
    st = state_()
    st.update_book(book_(ts=T0), T0)
    assert decide(st, T0 + 120, st.cfg).reason == "訂單簿過期"


def test_freeze_window_matches_keywords_only():
    cfg = cfg_(freeze_windows=[dict(name="開票", start=iso(T0 - 3600), end=iso(T0 + 3600), keywords=["Senate"])])
    st = state_(cfg=cfg, info=info_(question="Which party wins the Senate?"))
    st.update_book(book_(), T0)
    assert decide(st, T0, cfg).reason == "事件凍結：開票"
    other = state_(cfg=cfg, info=info_(question="Will it rain in Paris?"))
    other.update_book(book_(), T0)
    assert decide(other, T0, cfg).reason is None


def test_jump_cooldown_then_resume():
    st = state_()
    st.update_book(book_(0.49, 0.51), T0 - 2 * MIN)
    st.update_book(book_(0.54, 0.56, ts=T0 - MIN), T0 - MIN)      # 中價 0.50 → 0.55，超過 d = 0.0315
    assert st.last_jump_min == minute_of(T0 - MIN)
    st.update_book(book_(0.54, 0.56, ts=T0), T0)
    assert decide(st, T0, st.cfg).reason == "跳價／被吃後冷卻"
    later = T0 + 31 * MIN
    st.update_book(book_(0.54, 0.56, ts=later), later)
    assert decide(st, later, st.cfg).reason is None


def test_fill_cooldown():
    st = state_()
    st.update_book(book_(), T0)
    st.apply_fill("Y", "BUY", 0.47, 100, T0 - MIN)
    assert decide(st, T0, st.cfg).reason == "跳價／被吃後冷卻"


def _heavy_recent_volume(st):
    for k in range(60):                     # 前 50 分鐘每分鐘 10 股，最近 10 分鐘每分鐘 400 股
        ts = T0 - 3600 + k * MIN
        st.add_trades([Trade(ts, 0.5, 1, 400.0 if ts >= T0 - 10 * MIN else 10.0)])


def test_volume_pause():
    st = state_()
    _heavy_recent_volume(st)
    st.update_book(book_(), T0)
    assert decide(st, T0, st.cfg).reason == "成交量暫停"


def test_volume_pause_not_triggered_without_history():
    """剛啟動、沒有回補：基準用實際有資料的時間算，不會因為只看到最近幾分鐘就誤判。"""
    st = state_(first_seen=T0, vol_since=T0)
    _heavy_recent_volume(st)
    st.update_book(book_(), T0)
    assert decide(st, T0, st.cfg).reason is None


def test_shadow_book_has_no_lookahead_and_pauses_after_loss():
    st = state_()
    j = T0 - 15 * MIN
    st.update_book(book_(0.49, 0.51, ts=j), j)                      # 第 j 分鐘假想掛在 0.4685 / 0.5315
    st.add_trades([Trade(j + 5, 0.46, -1, 10.0)])                   # 有人賣到我的 bid 以下
    st.update_book(book_(0.39, 0.41, ts=j + 9 * MIN), j + 9 * MIN)
    assert len(st.shadow) == 0                                      # 還沒到 10 分鐘，不能結算
    st.update_book(book_(0.39, 0.41, ts=j + 10 * MIN), j + 10 * MIN)
    assert len(st.shadow) == 1 and st.shadow[0][1] < 0
    st.update_book(book_(0.39, 0.41, ts=T0), T0)
    assert decide(st, T0, st.cfg).reason == "影子帳虧損"


def test_daily_stop_loss_sticks_for_the_day():
    st = state_()
    st.update_book(book_(), T0)                                     # 當天起點：市值 0
    st.yes, st.cash = 1000.0, -500.0
    st.update_book(book_(0.44, 0.46, ts=T0 + MIN), T0 + MIN)        # 跌到 0.45：虧 50 > 本金 2.5%
    assert decide(st, T0 + MIN, st.cfg).reason == "單日停損"
    st.yes, st.cash = 0.0, 0.0
    later = T0 + 40 * MIN
    st.update_book(book_(0.44, 0.46, ts=later), later)
    assert decide(st, later, st.cfg).reason == "單日停損"           # 同一天不恢復


def test_inventory_limits_to_reducing_side():
    st = state_()
    st.update_book(book_(), T0)
    st.yes, st.cash = 1200.0, -600.0
    assert decide(st, T0, st.cfg) == Decision(False, True, "Yes 庫存過多，只掛減倉")
    st.yes, st.no, st.cash = 0.0, 1200.0, -600.0
    assert decide(st, T0, st.cfg) == Decision(True, False, "No 庫存過多，只掛減倉")


def test_mid_out_of_band():
    st = state_()
    st.update_book(book_(0.01, 0.03), T0)
    assert decide(st, T0, st.cfg).reason == "中價超出範圍"


def test_merge_and_value():
    st = state_()
    st.update_book(book_(), T0)
    st.apply_fill("Y", "BUY", 0.46, 100, T0)
    st.apply_fill("N", "BUY", 0.46, 60, T0)
    assert st.merge() == 60
    assert st.yes == pytest.approx(40) and st.no == 0
    assert st.value() == pytest.approx(-0.46 * 160 + 60 + 40 * 0.5)


# ---------------------------------------------------------------- 報價
def _quoting_state(cfg=None, info=None, book=None):
    st = state_(cfg=cfg, info=info)
    st.update_book(book or book_(), T0)
    return st


def test_target_quotes_basic():
    st = _quoting_state()
    q = target_quotes(st, Decision(True, True), st.cfg)
    n = int(st.shares * 100) / 100
    assert q == [OrderIntent("c1", "Y", "BUY", 0.46, n, "bid"), OrderIntent("c1", "N", "BUY", 0.46, n, "ask")]


def test_target_quotes_stay_inside_max_spread():
    cfg = cfg_(dist_frac=1.0)                     # 掛在 v 上會拿不到分：往內一格
    st = _quoting_state(cfg=cfg)
    q = target_quotes(st, Decision(True, True), cfg)
    assert [x.price for x in q] == [0.46, 0.46]


def test_target_quotes_post_only_safety():
    # 小量的賣單在 0.48（小於計分門檻，不影響中價 0.51），我的 bid 不能碰到它：退到 0.47
    cfg = cfg_(dist_frac=0.5)
    st = _quoting_state(cfg=cfg, info=info_(v=0.06), book=book_(0.45, 0.57, extra_asks=((0.48, 1.0),)))
    assert st.mid == pytest.approx(0.51)
    q = target_quotes(st, Decision(True, False), cfg)
    assert [x.price for x in q] == [0.47]
    # 退一格之後超出最大合格價差：乾脆不掛
    st = _quoting_state(book=book_(0.45, 0.57, extra_asks=((0.47, 1.0),)))
    assert target_quotes(st, Decision(True, False), st.cfg) == []


def test_target_quotes_fine_tick():
    st = _quoting_state(book=book_(0.499, 0.501, tick=0.001))
    q = target_quotes(st, Decision(True, True), st.cfg)
    assert [x.price for x in q] == [0.468, 0.468]


def test_target_quotes_sell_inventory_first():
    st = _quoting_state()
    st.no, st.yes = 200.0, 300.0
    q = target_quotes(st, Decision(True, True), st.cfg)
    assert q[0] == OrderIntent("c1", "N", "SELL", 0.54, 200.0, "bid")
    assert q[1] == OrderIntent("c1", "Y", "SELL", 0.54, 300.0, "ask")


def test_target_quotes_too_small_or_paused():
    st = _quoting_state(info=info_(capital=10.0))
    assert target_quotes(st, Decision(True, True), st.cfg) == []
    st = _quoting_state()
    assert target_quotes(st, Decision(False, False, "x"), st.cfg) == []


def _order(oid, intent, expires_at=T0 + 1200):
    return Order(oid, intent.condition_id, intent.token, intent.side, intent.price, intent.size, intent.leg,
                 created_at=T0 - 60, expires_at=expires_at)


def test_diff_orders():
    cfg = cfg_()
    d = 0.0315
    want = [OrderIntent("c1", "Y", "BUY", 0.46, 1000, "bid"), OrderIntent("c1", "N", "BUY", 0.46, 1000, "ask")]
    same = [_order("a", want[0]), _order("b", want[1])]
    assert diff_orders(same, want, d, T0, cfg) == ([], [])
    moved = [_order("a", replace(want[0], price=0.45)), _order("b", want[1])]
    assert diff_orders(moved, want, d, T0, cfg) == (["a"], [want[0]])
    small_move = [_order("a", replace(want[0], price=0.465)), _order("b", want[1])]
    assert diff_orders(small_move, want, d, T0, cfg) == ([], [])
    expiring = [_order("a", want[0], expires_at=T0 + 60), _order("b", want[1])]
    assert diff_orders(expiring, want, d, T0, cfg) == (["a"], [want[0]])
    extra = same + [_order("c", replace(want[0], token="N", side="SELL"))]
    assert diff_orders(extra, want, d, T0, cfg) == (["c"], [])
    assert diff_orders(same, [], d, T0, cfg) == (["a", "b"], [])


def test_diff_orders_tolerates_one_tick_but_not_leaving_the_band():
    cfg = cfg_()
    d, v = 0.0315, 0.045
    want = [OrderIntent("c1", "Y", "BUY", 0.46, 1000, "bid"), OrderIntent("c1", "N", "BUY", 0.46, 1000, "ask")]
    kw = dict(tick=0.01, mid=0.50, v=v, yes_token="Y")
    inside = [_order("a", replace(want[0], price=0.47)), _order("b", replace(want[1], price=0.47))]
    assert diff_orders(inside, want, d, T0, cfg, **kw) == ([], [])        # 差一格、還在 4.5¢ 以內：不動
    outside = [_order("a", replace(want[0], price=0.45)), _order("b", replace(want[1], price=0.45))]
    assert diff_orders(outside, want, d, T0, cfg, **kw) == (["a", "b"], want)   # 差一格但離中價 5¢：不計分，重掛
    two_ticks = [_order("a", replace(want[0], price=0.48)), _order("b", want[1])]
    assert diff_orders(two_ticks, want, d, T0, cfg, **kw) == (["a"], [want[0]])


# ---------------------------------------------------------------- 選市場、配本金
def test_competitor_score():
    b = Book(T0, bids=((0.49, 100.0), (0.40, 100.0)), asks=((0.51, 100.0),))
    s = ((0.045 - 0.01) / 0.045) ** 2
    assert competitor_score(b, 0.5, 0.045) == pytest.approx(100 * s)
    one_sided = Book(T0, bids=((0.49, 300.0),), asks=((0.70, 100.0),))
    assert competitor_score(one_sided, 0.5, 0.045) == pytest.approx(300 * s / 3)
    assert competitor_score(one_sided, 0.95, 0.045) == 0.0


def test_best_capital():
    cap, net, reward = best_capital(100, 1.0, 1.0, 1000, 0.001, 10, 20000)
    assert abs(cap - 9000) / 9000 < 0.06               # 解析解：(C + K)² = R·K / h
    assert net == pytest.approx(reward - 0.001 * cap)
    assert best_capital(100, 1.0, 1.0, 1000, 1.0, 10, 20000) == (0.0, 0.0, 0.0)


def _cand(cid, slug, rate=5000.0, comp=2000.0, cat="其他"):
    return Candidate(cid, f"Q {cid}", slug, cat, f"Y{cid}", f"N{cid}", rate, 0.045, 50.0, None, 0.5, 0.02, comp)


def test_allocate_market_and_event_caps():
    cfg = cfg_()
    df = allocate([_cand("a", "e1"), _cand("b", "e1"), _cand("c", "e2")], cfg, cfg.hurdle_mult)
    cap = df.set_index("condition_id")["capital"]
    assert cap["c"] == pytest.approx(cfg.market_cap)
    assert cap["a"] + cap["b"] == pytest.approx(cfg.event_cap)
    assert (df["exp_net"] - (df["exp_reward"] - df["h"] * df["capital"])).abs().max() < 1e-9


def test_solve_budget_raises_hurdle():
    cfg = cfg_(budget=50_000)
    cands = [_cand(str(i), f"e{i}", rate=500 + 100 * i) for i in range(30)]
    df, mult = solve_budget(cands, cfg)
    assert df["capital"].sum() <= cfg.budget + 1e-6
    assert mult > cfg.hurdle_mult


def _row(cid, spread_bps=4.5, end="2027-06-01T00:00:00Z", rate=200.0, q="Will it rain?", slug="rain"):
    return dict(condition_id=cid, question=q, event_slug=slug, end_date=end,
                tokens=[dict(token_id=f"Y{cid}", outcome="Yes"), dict(token_id=f"N{cid}", outcome="No")],
                rewards_max_spread=spread_bps, rewards_min_size=50, rewards_config=[dict(rate_per_day=rate)])


def test_candidates_eligibility():
    now = T0
    rows = [_row("ok"), _row("wide"), _row("soon", end=iso(now + 3600)), _row("edge")]
    books = {"Yok": book_(0.49, 0.51), "Ywide": book_(0.40, 0.60), "Ysoon": book_(0.49, 0.51), "Yedge": book_(0.005, 0.02)}
    got = [c.condition_id for c in candidates(rows, books, cfg_(), now)]
    assert got == ["ok"]


def test_candidates_dedupe():
    rows = [_row("ok"), _row("ok")]
    assert len(candidates(rows, {"Yok": book_(0.49, 0.51)}, cfg_(), T0)) == 1


# ---------------------------------------------------------------- 成交輪詢
class _PagedFeed:
    """由新到舊、每頁 10 筆；不理會 start（和真的 API 一樣）。"""

    def __init__(self):
        self.calls = 0
        self.rows = [dict(condition_id="c1", token_id="Y" if ts % 2 else "N", side="BUY", price="0.30", size="5",
                          timestamp=ts, transaction_hash=f"0x{ts}") for ts in range(200, 100, -1)]

    def _get(self, url, **params):
        self.calls += 1
        i = int(params.get("cursor") or 0)
        return {"data": self.rows[i:i + 10], "pagination": {"next_cursor": str(i + 10)}}


def test_trade_poller_stops_at_old_trades_and_converts_to_yes():
    feed = _PagedFeed()
    poller = TradePoller(feed, {"c1": "Y"})
    poller.last_ts = 175 + TradePoller.OVERLAP_S           # start = 175：要抓第 1–3 頁
    got = poller.poll()["c1"]
    assert feed.calls == 3
    assert sorted(t.ts for t in got) == list(range(175, 201))
    yes = next(t for t in got if t.ts == 199)
    no = next(t for t in got if t.ts == 200)
    assert (yes.price, yes.direction) == (0.30, 1)         # 買 Yes @ 0.30
    assert (no.price, no.direction) == (0.70, -1)          # 買 No @ 0.30 = 賣 Yes @ 0.70
    assert poller.poll() == {}                             # 同樣的成交不重複


# ---------------------------------------------------------------- 模擬成交
def test_paper_fills():
    cfg = cfg_()
    info = info_()
    ex = PaperExchange(cfg, {"c1": info})
    now = time.time()
    bid, ask = asyncio.run(ex.place([OrderIntent("c1", "Y", "BUY", 0.46, 100, "bid"),
                                     OrderIntent("c1", "N", "BUY", 0.46, 100, "ask")], now))
    t = int(now) + 1
    ex.on_trades({"c1": [Trade(int(now) - 5, 0.40, -1, 50),     # 下單之前的成交不算
                         Trade(t, 0.45, -1, 30),                 # 穿過 bid：全部 30
                         Trade(t, 0.46, -1, 40),                 # 剛好在 bid：一半 20
                         Trade(t, 0.47, -1, 1000),               # 沒到 bid
                         Trade(t, 0.54, 1, 500)]})               # 剛好在 ask：min(100, 250)
    fills = asyncio.run(ex.fills())
    assert [(f.order_id, f.size) for f in fills] == [(bid.id, 30), (bid.id, 20), (ask.id, 100)]
    assert fills[-1].token == "N" and fills[-1].side == "BUY" and fills[-1].price == 0.46
    left = asyncio.run(ex.open_orders())
    assert [o.id for o in left] == [bid.id] and left[0].remaining == pytest.approx(50)


# ---------------------------------------------------------------- 實盤介面（假的 SDK）
class _Mismatch(Exception):
    def __init__(self, heartbeat_id):
        super().__init__("mismatch")
        self.heartbeat_id = heartbeat_id


@pytest.fixture
def fake_sdk(monkeypatch):
    mod = types.ModuleType("polymarket")
    mod.OrderHeartbeatMismatchError = _Mismatch
    monkeypatch.setitem(sys.modules, "polymarket", mod)
    return mod


async def _pages(*pages):
    for items in pages:
        yield SimpleNamespace(items=items)


class FakeClient:
    wallet = "0xABC"

    def __init__(self, closed_only=False, reject_price=None, positions=()):
        self.closed_only, self.reject_price = closed_only, reject_price
        self.created, self.posted, self.cancelled = [], [], []
        self.cancel_all_calls, self.heartbeats = 0, []
        self.open: dict[str, dict] = {}
        self.trades, self._positions = [], list(positions)

    async def get_closed_only_mode(self):
        return self.closed_only

    async def cancel_all(self):
        self.cancel_all_calls += 1
        self.open.clear()

    async def send_order_heartbeat(self, heartbeat_id):
        self.heartbeats.append(heartbeat_id)
        if heartbeat_id == "":
            raise _Mismatch("srv-1")
        return SimpleNamespace(heartbeat_id=heartbeat_id)

    async def create_limit_order(self, **kw):
        self.created.append(kw)
        return kw

    async def post_orders(self, signed):
        assert 1 <= len(signed) <= 15
        self.posted.append(len(signed))
        out = []
        for so in signed:
            if so["price"] == self.reject_price:
                out.append(SimpleNamespace(ok=False, code="INVALID_PRICE", message="bad"))
                continue
            oid = f"o{len(self.open) + len(self.cancelled) + 1}"
            self.open[oid] = so
            out.append(SimpleNamespace(ok=True, order_id=oid))
        return out

    async def cancel_orders(self, order_ids):
        self.cancelled += list(order_ids)
        for i in order_ids:
            self.open.pop(i, None)
        return SimpleNamespace(canceled=list(order_ids), not_canceled={})

    def list_open_orders(self):
        now = datetime.now(timezone.utc)
        return _pages([SimpleNamespace(id=k, condition_id="c1", asset_id=v["asset_id"], side=v["side"], price=v["price"],
                                       original_size=v["size"], size_matched="0", created_at=now,
                                       expires_at=datetime.fromtimestamp(v["expiration"], timezone.utc))
                       for k, v in self.open.items()])

    def list_account_trades(self, after):
        return _pages(self.trades)

    async def get_balance_allowance(self, asset_type):
        assert asset_type == "COLLATERAL"
        return SimpleNamespace(balance=5_000 * 10**6, allowances={})

    def list_positions(self):
        return _pages([SimpleNamespace(condition_id=c, asset_id=a, current_size=s) for c, a, s in self._positions])

    async def get_orders_scoring(self, order_ids):
        return {i: True for i in order_ids}


def test_live_exchange_orders_and_heartbeat(fake_sdk):
    cfg = cfg_()
    client = FakeClient(reject_price="0.47")
    ex = LiveExchange(cfg, {"c1": info_()}, private_key="secret", client=client)
    ex.ORDERS_PER_S = 1e6

    async def go():
        await ex.start()
        assert ex._private_key is None and ex.wallet == "0xABC" and client.cancel_all_calls == 1
        await asyncio.sleep(0.05)
        assert ex.heartbeat_ok() and client.heartbeats[:2] == ["", "srv-1"]
        now = time.time()
        intents = [OrderIntent("c1", "Y", "BUY", 0.46, 100 + i, "bid") for i in range(20)]
        intents.append(OrderIntent("c1", "Y", "BUY", 0.47, 100, "bid"))
        placed = await ex.place(intents, now)
        orders = await ex.open_orders()
        await ex.cancel([o.id for o in orders[:3]])
        await ex.close()
        return now, placed, orders

    now, placed, orders = asyncio.run(go())
    assert client.posted == [15, 6]
    assert all(c["post_only"] is True and isinstance(c["expiration"], int) and c["expiration"] >= now + 180
               for c in client.created)
    assert len(placed) == 20 and len(ex.rejections) == 1 and ex.rejections[0][1] == "INVALID_PRICE"
    assert {o.leg for o in orders} == {"bid"} and orders[0].price == 0.46
    assert len(client.cancelled) == 3


def test_live_exchange_refuses_closed_only(fake_sdk):
    ex = LiveExchange(cfg_(), {}, client=FakeClient(closed_only=True))
    with pytest.raises(RuntimeError):
        asyncio.run(ex.start())


def test_live_exchange_fills_only_mine_once(fake_sdk):
    client = FakeClient()
    ex = LiveExchange(cfg_(), {"c1": info_()}, client=client)
    ex.wallet = "0xABC"
    mo = lambda oid, addr: SimpleNamespace(order_id=oid, asset_id="Y", maker_address=addr, side="BUY",
                                           price="0.46", matched_amount="25")
    client.trades = [SimpleNamespace(id="t1", condition_id="c1", matched_at=datetime.now(timezone.utc),
                                     maker_orders=[mo("mine", "0xabc"), mo("theirs", "0xdef")])]
    first = asyncio.run(ex.fills())
    again = asyncio.run(ex.fills())
    assert [(f.order_id, f.size, f.price) for f in first] == [("mine", 25.0, 0.46)]
    assert again == []


# ---------------------------------------------------------------- 主迴圈
class FakeData:
    def __init__(self):
        self.rows = [_row("c1"), _row("c2", q="Will it snow?", slug="snow")]
        self._books = {"Yc1": book_(0.49, 0.51, size=2000), "Yc2": book_(0.49, 0.51, size=2000)}
        self.trades = []
        self.errors = deque()

    def reward_markets(self):
        return self.rows

    def books(self, tokens):
        return {t: replace(self._books[t], ts=time.time()) for t in tokens if t in self._books}

    def gamma_markets(self, cids):
        return {c: {} for c in cids}

    def _get(self, url, **params):
        # 和真的 API 一樣：condition 查詢不理會 start，由新到舊回傳
        assert "start" not in params
        cids = params["condition"].split(",")
        rows = sorted((t for t in self.trades if t["condition_id"] in cids), key=lambda t: -t["timestamp"])
        return {"data": rows[:params["limit"]]}


def _paper_runner(tmp_path, **cfg_kw):
    cfg = cfg_(log_dir=str(tmp_path), tick_seconds=0.01, **cfg_kw)
    data = FakeData()
    r = Runner(cfg, "paper", data=data)
    r.exchange = PaperExchange(cfg, {})
    r.refresh_universe(time.time())
    r._started = True
    poller = TradePoller(data, {c: s.info.yes_token for c, s in r.states.items()}, lookback_s=60)
    return r, data, poller


def test_runner_quotes_then_pulls_after_fill(tmp_path):
    r, data, poller = _paper_runner(tmp_path)
    assert set(r.states) == {"c1", "c2"}
    status = asyncio.run(r.step(poller))
    assert not status.kill
    assert sorted(o.condition_id for o in r.exchange.orders.values()) == ["c1", "c1", "c2", "c2"]
    bid = next(o for o in r.exchange.orders.values() if o.condition_id == "c1" and o.leg == "bid")
    data.trades.append(dict(condition_id="c1", token_id="Yc1", side="SELL", price="0.40", size="100",
                            timestamp=int(time.time()) + 1, transaction_hash="0x1"))
    asyncio.run(r.step(poller))
    st = r.states["c1"]
    assert st.yes == pytest.approx(100) and st.cash == pytest.approx(-100 * bid.price)
    assert sorted(o.condition_id for o in r.exchange.orders.values()) == ["c2", "c2"]   # c1 冷卻中：撤單
    events = [json.loads(x) for x in (r.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "fill" and e["condition_id"] == "c1" for e in events)


def test_runner_kill_switch_cancels_everything(tmp_path):
    cfg = cfg_(log_dir=str(tmp_path), tick_seconds=0.01, max_daily_loss_frac=-1.0)   # 一開始就觸發
    r = Runner(cfg, "paper", data=FakeData())
    reason = asyncio.run(r.run(minutes=0.05))
    assert "全域單日虧損" in reason
    assert r.exchange.orders == {}
    assert json.loads((r.dir / "summary.json").read_text(encoding="utf-8"))["stop_reason"] == reason


def test_runner_paper_run_and_report(tmp_path):
    cfg = cfg_(log_dir=str(tmp_path), tick_seconds=0.01)
    r = Runner(cfg, "paper", data=FakeData())
    assert asyncio.run(r.run(minutes=0.01)) == "時間到"
    assert r.exchange.orders == {}
    res = summarize(r.dir)
    assert res["markets"] == 2 and res["placements"] >= 4
    assert all(st.reward_est > 0 for st in r.states.values())


def test_runner_live_with_fake_sdk(tmp_path, fake_sdk):
    cfg = cfg_(log_dir=str(tmp_path), tick_seconds=0.01)
    client = FakeClient(positions=[("c1", "Yc1", 100.0), ("other", "Z", 5.0)])
    ex = LiveExchange(cfg, {}, client=client)
    ex.ORDERS_PER_S = 1e6
    r = Runner(cfg, "live", data=FakeData(), exchange=ex)
    assert asyncio.run(r.run(minutes=0.02)) == "時間到"
    events = [json.loads(x) for x in (r.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    uni = next(e for e in events if e["kind"] == "universe")
    assert uni["budget"] == pytest.approx(0.95 * 5000)                 # 餘額不足預算：用 95% 的餘額
    assert r.states["c1"].yes == pytest.approx(100)                    # 載入既有部位
    assert client.cancel_all_calls == 2                                # 啟動時、結束時
    assert client.created and all(c["post_only"] for c in client.created)
    # 持有 Yes：ask 那一邊改成賣 Yes 減倉
    assert any(c["asset_id"] == "Yc1" and c["side"] == "SELL" for c in client.created)


# ---------------------------------------------------------------- 設定與上線檢查
def test_config_load(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(dict(budget=1000, mid_band=[0.1, 0.9])), encoding="utf-8")
    cfg = MMConfig.load(p, max_markets=5)
    assert cfg.budget == 1000 and cfg.mid_band == (0.1, 0.9) and cfg.max_markets == 5
    p.write_text(json.dumps(dict(budgte=1000)), encoding="utf-8")
    with pytest.raises(ValueError):
        MMConfig.load(p)


def test_gate():
    good = dict(hours=24 * 8, net_est=500.0, mtm_pnl=-100.0, est_reward=600.0, kills=[], quoting_frac=0.8)
    assert all(ok for _, ok, _ in gate(good, "paper"))
    bad = dict(good, mtm_pnl=-500.0, net_est=100.0)
    assert not all(ok for _, ok, _ in gate(bad, "paper"))
    live = dict(good, hours=24 * 15, reconcile_max_diffs=0, scoring_frac=0.9)
    assert all(ok for _, ok, _ in gate(live, "live-10k", actual_reward=400.0))
    assert not all(ok for _, ok, _ in gate(live, "live-10k", actual_reward=100.0))


def test_mm_check_is_read_only(fake_sdk, monkeypatch, capsys):
    from pmopt.mm import cli
    client = FakeClient(positions=[("c1", "Yc1", 100.0)])

    class _Client:
        @staticmethod
        async def create(private_key, wallet=None):
            assert private_key == "k"
            return client

    fake_sdk.AsyncSecureClient = _Client
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "k")
    assert cli.cmd_check(None) == 0
    out = capsys.readouterr().out
    assert "0xABC" in out and "5,000.00" in out and "持有部位：1 個" in out
    assert client.cancel_all_calls == 0 and client.created == [] and client.cancelled == []
