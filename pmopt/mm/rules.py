"""每個市場的即時狀態，以及「現在該不該掛、掛哪一邊」的判斷。

規則的語意和回測（docs/market-making.md）一致，而且只用「當下以前」的資料：
- 影子帳：每分鐘假設一直掛在 mid ± d，用 markout_min 分鐘後的中價結算假想成交，結算前不計入。
- 冷卻：1 分鐘中價移動 > d（跳價）或自己被吃之後，暫停 cooldown_min 分鐘。
- 成交量：前 W 分鐘成交量 > volu_mult × 前 24 小時的平均。
- 單日停損：當天（UTC）市值變化 < −stop_loss_frac × 本金，停到午夜。
所有時間都是 Unix 秒（UTC）。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import MMConfig

DAY_MIN = 1440


@dataclass(frozen=True)
class Trade:
    """一筆吃單成交，換成 Yes 的價格與吃單方方向（+1 = 買 Yes，−1 = 賣 Yes）。"""
    ts: int
    price: float
    direction: int
    shares: float


@dataclass(frozen=True)
class Book:
    """Yes 那一本訂單簿（已包含鏡像的 No 單）。"""
    ts: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    tick: float = 0.01
    min_order_size: float = 5.0


@dataclass
class MarketInfo:
    """選市場時決定、一小時內不變的資料。"""
    condition_id: str
    question: str
    slug: str
    category: str
    yes_token: str
    no_token: str
    rate: float              # 每日獎金池
    v: float                 # 最大合格價差（價格單位，例如 0.045）
    reward_min_size: float   # 計分的最低股數
    comp: float              # 競爭者的獎勵分數
    capital: float           # 配給這個市場的本金
    end_ts: float | None = None
    game_start_ts: float | None = None
    fee_rate: float = 0.0
    neg_risk: bool = False


@dataclass
class Decision:
    bid: bool
    ask: bool
    reason: str | None = None   # 不掛（或只掛一邊）的原因


def minute_of(ts: float) -> int:
    return int(ts // 60)


def without_own(book: Book, own: list[tuple[str, float, float]]) -> Book:
    """實盤時訂單簿裡有自己的單：扣掉之後才是「別人」的報價（算中價、價差、競爭者分數都要用這個）。

    own：[(leg, Yes 價格, 剩餘股數)]，leg 是 'bid' 或 'ask'。
    """
    if not own:
        return book
    bids, asks = dict(book.bids), dict(book.asks)
    for leg, price, size in own:
        side = bids if leg == "bid" else asks
        key = next((p for p in side if abs(p - price) < 1e-9), None)
        if key is not None:
            side[key] -= size
    keep = lambda d: tuple((p, q) for p, q in d.items() if q > 1e-9)
    return Book(book.ts, keep(bids), keep(asks), book.tick, book.min_order_size)


@dataclass
class MarketState:
    info: MarketInfo
    cfg: MMConfig
    first_seen: float
    vol_since: float | None = None                      # 成交量資料從什麼時候開始有（回補的起點）
    mids: dict = field(default_factory=dict)            # 分鐘 → 那一分鐘最後的中價
    vol: dict = field(default_factory=dict)             # 分鐘 → 成交股數
    min_sell: dict = field(default_factory=dict)        # 分鐘 → 吃單賣出的最低價
    max_buy: dict = field(default_factory=dict)         # 分鐘 → 吃單買入的最高價
    pending: dict = field(default_factory=dict)         # 分鐘 → 假想掛單 (bid, ask, 股數)
    shadow: deque = field(default_factory=deque)        # (分鐘, 已結算的假想淨額)
    book: Book | None = None
    mid: float | None = None
    last_jump_min: int = -10**9
    last_fill_min: int = -10**9
    yes: float = 0.0                                    # 持有的 Yes 股數
    no: float = 0.0
    cash: float = 0.0                                   # 累積現金流（買入為負）
    rebate: float = 0.0                                 # 估計的手續費返佣
    reward_est: float = 0.0                             # 估計的流動性獎勵（掛單時累積）
    day: int | None = None
    day_start_value: float = 0.0
    stopped_day: int | None = None

    # ---------- 參數 ----------
    @property
    def d(self) -> float:
        return self.cfg.dist_frac * self.info.v

    @property
    def shares(self) -> float:
        """兩邊各掛的股數：本金 ≈ 股數 × (1 − 2d)。"""
        return self.info.capital / max(1 - 2 * self.d, 0.05)

    @property
    def reward_per_min(self) -> float:
        score = (1 - self.cfg.dist_frac) ** 2 * self.shares
        comp = self.info.comp
        return self.info.rate / DAY_MIN * (score / (score + comp) if comp > 0 else 1.0)

    # ---------- 更新 ----------
    def update_book(self, book: Book, now: float) -> None:
        self.book = book
        big_b = [p for p, s in book.bids if s >= self.info.reward_min_size]
        big_a = [p for p, s in book.asks if s >= self.info.reward_min_size]
        if big_b and big_a:
            mid = (max(big_b) + min(big_a)) / 2
        elif book.bids and book.asks:
            mid = (max(p for p, _ in book.bids) + min(p for p, _ in book.asks)) / 2
        else:
            return
        m = minute_of(now)
        prev = self._mid_before(m)
        if prev is not None and abs(mid - prev) > self.d:
            self.last_jump_min = m
        self.mid = mid
        if m not in self.mids:
            # 新的一分鐘：登記這一分鐘的假想掛單（影子帳）
            self.pending[m] = (mid - self.d, mid + self.d, self.shares)
        self.mids[m] = mid
        self._roll_day(now)
        self._settle_shadow(m)
        self._trim(m)

    def add_trades(self, trades: list[Trade]) -> None:
        for t in trades:
            m = minute_of(t.ts)
            self.vol[m] = self.vol.get(m, 0.0) + t.shares
            if t.direction < 0:
                self.min_sell[m] = min(self.min_sell.get(m, math.inf), t.price)
            else:
                self.max_buy[m] = max(self.max_buy.get(m, -math.inf), t.price)

    def apply_fill(self, token: str, side: str, price: float, size: float, ts: float) -> None:
        """自己的單被吃：更新部位與現金。"""
        sign = 1 if side == "BUY" else -1
        if token == self.info.yes_token:
            self.yes += sign * size
        else:
            self.no += sign * size
        self.cash -= sign * price * size
        self.rebate += 0.2 * self.info.fee_rate * price * (1 - price) * size
        self.last_fill_min = minute_of(ts)

    def merge(self) -> float:
        """同時持有 Yes 與 No：每一對可以換回 $1。回傳 merge 的股數。"""
        q = min(self.yes, self.no)
        if q > 0:
            self.yes -= q
            self.no -= q
            self.cash += q
        return q

    # ---------- 衍生量 ----------
    def value(self) -> float:
        """市值：現金 + 部位（用中價估）。"""
        mid = self.mid if self.mid is not None else 0.5
        return self.cash + self.yes * mid + self.no * (1 - mid)

    def day_pnl(self) -> float:
        return self.value() - self.day_start_value

    def inventory_value(self) -> float:
        """單邊庫存的價值（Yes 與 No 可以互相抵銷）。正 = 偏多 Yes。"""
        mid = self.mid if self.mid is not None else 0.5
        net = self.yes - self.no
        return net * (mid if net > 0 else (1 - mid))

    def shadow_sum(self, now: float) -> float:
        cutoff = minute_of(now) - self.cfg.trail_hours * 60
        return sum(v for m, v in self.shadow if m >= cutoff)

    # ---------- 內部 ----------
    def _mid_before(self, m: int) -> float | None:
        for k in (m - 1, m - 2, m - 3):
            if k in self.mids:
                return self.mids[k]
        return None

    def _roll_day(self, now: float) -> None:
        d = int(now // 86400)
        if self.day != d:
            self.day = d
            self.day_start_value = self.value()

    def _settle_shadow(self, m: int) -> None:
        """第 j 分鐘的假想掛單，在 j + markout 分鐘的中價出現後結算。"""
        h = self.cfg.markout_min
        for j in sorted(k for k in self.pending if k + h <= m):
            later = self.mids.get(j + h, self.mid)
            bid, ask, n = self.pending.pop(j)
            pnl = 0.0
            if self.min_sell.get(j, math.inf) <= bid + 1e-9:
                pnl += n * (later - bid - self.cfg.unwind_cost)
            if self.max_buy.get(j, -math.inf) >= ask - 1e-9:
                pnl += n * (ask - later - self.cfg.unwind_cost)
            self.shadow.append((j, pnl + self.reward_per_min))

    def _trim(self, m: int) -> None:
        keep = m - DAY_MIN - 60
        for dct in (self.mids, self.vol, self.min_sell, self.max_buy):
            for k in [k for k in dct if k < keep]:
                del dct[k]
        cutoff = m - self.cfg.trail_hours * 60 - 1
        while self.shadow and self.shadow[0][0] < cutoff:
            self.shadow.popleft()


def _freeze_reason(info: MarketInfo, now: float, windows: list[dict]) -> str | None:
    for w in windows:
        start = datetime.fromisoformat(w["start"].replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(w["end"].replace("Z", "+00:00")).timestamp()
        if start <= now < end:
            text = f"{info.question} {info.slug}"
            if any(k in text for k in w.get("keywords", [])) or w.get("slugs") and info.slug in w["slugs"]:
                return f"事件凍結：{w['name']}"
    return None


def decide(st: MarketState, now: float, cfg: MMConfig) -> Decision:
    """依序檢查每一條規則；第一條擋下來的就是原因。"""
    info, m = st.info, minute_of(now)
    if now - st.first_seen < cfg.warmup_min * 60:
        return Decision(False, False, "暖機中")
    if st.book is None or st.mid is None or now - st.book.ts > cfg.stale_data_seconds:
        return Decision(False, False, "訂單簿過期")
    if info.end_ts is not None and now >= info.end_ts - cfg.stop_before_end_hours * 3600:
        return Decision(False, False, "已過（或接近）截止日")
    if info.game_start_ts is not None and now >= info.game_start_ts - cfg.sports_stop_before_min * 60:
        return Decision(False, False, "比賽即將開始")
    reason = _freeze_reason(info, now, cfg.freeze_windows)
    if reason:
        return Decision(False, False, reason)
    if st.stopped_day == st.day:
        return Decision(False, False, "單日停損")
    if st.day_pnl() < -cfg.stop_loss_frac * info.capital:
        st.stopped_day = st.day
        return Decision(False, False, "單日停損")
    if m < max(st.last_jump_min, st.last_fill_min + 1) + cfg.cooldown_min:
        return Decision(False, False, "跳價／被吃後冷卻")
    if cfg.vol_window_min:
        window = [st.mids[k] for k in range(m - cfg.vol_window_min, m + 1) if k in st.mids]
        if window and max(window) - min(window) > cfg.vol_mult * st.d:
            return Decision(False, False, "波動暫停")
    if cfg.volu_window_min:
        recent = sum(v for k, v in st.vol.items() if m - cfg.volu_window_min <= k < m)
        day_total = sum(v for k, v in st.vol.items() if m - DAY_MIN <= k < m)
        # 剛啟動時沒有一整天的資料：用實際有資料的分鐘數算平均
        since = minute_of(st.vol_since if st.vol_since is not None else st.first_seen)
        span = max(min(DAY_MIN, m - since), cfg.volu_window_min)
        baseline = day_total / span * cfg.volu_window_min
        if baseline > 0 and recent > cfg.volu_mult * baseline:
            return Decision(False, False, "成交量暫停")
    if cfg.trail_hours and st.shadow and st.shadow_sum(now) < 0:
        return Decision(False, False, "影子帳虧損")
    lo, hi = cfg.mid_band
    if not lo <= st.mid <= hi:
        return Decision(False, False, "中價超出範圍")
    inv = st.inventory_value()
    if inv > cfg.max_inventory_frac * info.capital:
        return Decision(False, True, "Yes 庫存過多，只掛減倉")
    if -inv > cfg.max_inventory_frac * info.capital:
        return Decision(True, False, "No 庫存過多，只掛減倉")
    return Decision(True, True, None)
