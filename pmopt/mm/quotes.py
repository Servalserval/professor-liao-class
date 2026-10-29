"""把「要掛哪一邊」變成實際的訂單，並和現有掛單比對，決定撤哪些、掛哪些。

兩邊都只需要現金當保證金：
- 買方那一邊：買 Yes @ bid；手上有 No 庫存時改成賣 No @ (1 − bid)（直接減倉）
- 賣方那一邊：買 No @ (1 − ask)（等同賣 Yes）；手上有 Yes 庫存時改成賣 Yes @ ask
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import MMConfig
from .rules import Decision, MarketState

# 官方文件：tick size 對應的價格小數位數
_PRICE_DECIMALS = {0.1: 1, 0.01: 2, 0.005: 3, 0.0025: 4, 0.001: 3, 0.0001: 4}


def price_decimals(tick: float) -> int:
    for t, dcm in _PRICE_DECIMALS.items():
        if abs(t - tick) < 1e-12:
            return dcm
    return max(0, -int(math.floor(math.log10(tick))))


@dataclass(frozen=True)
class OrderIntent:
    condition_id: str
    token: str
    side: str        # "BUY" / "SELL"
    price: float
    size: float
    leg: str         # "bid" / "ask"（以 Yes 來看是哪一邊）


@dataclass
class Order:
    """交易所上的掛單（模擬與實盤共用）。"""
    id: str
    condition_id: str
    token: str
    side: str
    price: float
    size: float
    leg: str
    filled: float = 0.0
    created_at: float = 0.0
    expires_at: float | None = None

    @property
    def remaining(self) -> float:
        return max(self.size - self.filled, 0.0)


def _floor(x: float, tick: float) -> float:
    return math.floor(x / tick + 1e-9) * tick


def _ceil(x: float, tick: float) -> float:
    return math.ceil(x / tick - 1e-9) * tick


def target_quotes(st: MarketState, dec: Decision, cfg: MMConfig) -> list[OrderIntent]:
    """依規則判斷，算出這個市場想要掛的單（0–2 張）。"""
    if st.book is None or st.mid is None or not (dec.bid or dec.ask):
        return []
    info, book, mid, d = st.info, st.book, st.mid, st.d
    tick = book.tick
    dcm = price_decimals(tick)
    bid = _floor(mid - d, tick)
    ask = _ceil(mid + d, tick)
    if mid - bid >= info.v - 1e-9:      # 超出最大合格價差就拿不到分：往內一格
        bid += tick
    if ask - mid >= info.v - 1e-9:
        ask -= tick
    best_bid = max((p for p, _ in book.bids), default=None)
    best_ask = min((p for p, _ in book.asks), default=None)
    if best_ask is not None and bid >= best_ask - 1e-9:   # 只掛單不吃單
        bid = best_ask - tick
    if best_bid is not None and ask <= best_bid + 1e-9:
        ask = best_bid + tick
    bid, ask = round(bid, dcm), round(ask, dcm)
    if bid < tick or ask > 1 - tick or bid >= ask:
        return []
    if mid - bid >= info.v or ask - mid >= info.v:
        return []
    min_size = max(info.reward_min_size, book.min_order_size)
    n = math.floor(st.shares * 100) / 100
    if n < min_size:
        return []
    out = []
    if dec.bid:
        if st.no >= book.min_order_size:
            out.append(OrderIntent(info.condition_id, info.no_token, "SELL", round(1 - bid, dcm),
                                   math.floor(min(st.no, n) * 100) / 100, "bid"))
        else:
            out.append(OrderIntent(info.condition_id, info.yes_token, "BUY", bid, n, "bid"))
    if dec.ask:
        if st.yes >= book.min_order_size:
            out.append(OrderIntent(info.condition_id, info.yes_token, "SELL", ask,
                                   math.floor(min(st.yes, n) * 100) / 100, "ask"))
        else:
            out.append(OrderIntent(info.condition_id, info.no_token, "BUY", round(1 - ask, dcm), n, "ask"))
    return out


def diff_orders(open_orders: list[Order], desired: list[OrderIntent], d: float, now: float, cfg: MMConfig,
                tick: float = 0.0, mid: float | None = None, v: float | None = None,
                yes_token: str | None = None) -> tuple[list[str], list[OrderIntent]]:
    """保留還算接近目標的掛單，其他的撤掉重掛。回傳 (要撤的 id, 要掛的單)。

    撤單重掛會讓掛單重新計時（官方要求掛單存活一段時間才計分），所以容忍目標價移動一個 tick；
    但給了 mid 與 v 時，舊單一定要還在最大合格價差以內才保留。
    """
    cancel, place = [], []
    remaining = list(open_orders)
    tol = max(cfg.requote_frac * d, tick)
    for want in desired:
        match = next((o for o in remaining if o.token == want.token and o.side == want.side and o.leg == want.leg), None)
        if match is not None:
            remaining.remove(match)
            close_price = abs(match.price - want.price) < tol + 1e-9
            if close_price and mid is not None and v is not None and yes_token is not None:
                yes_price = match.price if match.token == yes_token else 1 - match.price
                close_price = abs(mid - yes_price) < v - 1e-9
            close_size = abs(match.remaining - want.size) <= 0.2 * want.size
            fresh = match.expires_at is None or match.expires_at - now > cfg.gtd_renew_minutes * 60
            if close_price and close_size and fresh:
                continue
            cancel.append(match.id)
        place.append(want)
    cancel += [o.id for o in remaining]
    return cancel, place
