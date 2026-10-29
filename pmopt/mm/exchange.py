"""交易所介面：模擬（PaperExchange）與實盤（LiveExchange，官方 polymarket-client SDK）。

兩者介面相同，主迴圈不需要知道現在是哪一種：
    open_orders()、place(intents, now)、cancel(ids)、cancel_all()、fills()、
    collateral()、positions()、merge(condition_id)
實盤的安全設計：
- 所有單都是 post-only（只掛單不吃單）＋ GTD（gtd_minutes 後自動失效，預設 60 分鐘）；
- 官方心跳：每 5 秒送一次，程式停止回應 10 秒，交易所會撤掉這個 API 金鑰的所有單；
- 私鑰只從環境變數讀，不寫進任何檔案或記錄。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

from .config import MMConfig
from .quotes import Order, OrderIntent
from .rules import MarketInfo, Trade

log = logging.getLogger("pmopt.mm")


@dataclass(frozen=True)
class Fill:
    order_id: str
    condition_id: str
    token: str
    side: str
    price: float
    size: float
    ts: float


def _yes_level(order: Order, info: MarketInfo) -> tuple[str, float]:
    """把一張單換成「以 Yes 來看」的掛單：回傳 ('bid' 或 'ask', Yes 價格)。"""
    on_yes = order.token == info.yes_token
    if order.side == "BUY":
        return ("bid", order.price) if on_yes else ("ask", 1 - order.price)
    return ("ask", order.price) if on_yes else ("bid", 1 - order.price)


class PaperExchange:
    """用真實的吃單成交模擬自己的單有沒有被吃到（不送出任何訂單）。

    成交規則（偏保守）：吃單價格穿過我的價位 → 成交 min(剩餘, 該筆股數)；
    剛好等於我的價位 → 只成交 paper_fill_at_level 的比例（排隊不一定輪到我）。
    """

    def __init__(self, cfg: MMConfig, markets: dict[str, MarketInfo]):
        self.cfg = cfg
        self.markets = markets
        self.orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._fills: list[Fill] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def heartbeat_ok(self) -> bool:
        return True

    async def open_orders(self) -> list[Order]:
        now = time.time()
        for oid in [k for k, o in self.orders.items() if o.expires_at is not None and o.expires_at <= now]:
            del self.orders[oid]
        return list(self.orders.values())

    async def place(self, intents: list[OrderIntent], now: float) -> list[Order]:
        out = []
        for it in intents:
            o = Order(id=f"paper-{next(self._ids)}", condition_id=it.condition_id, token=it.token, side=it.side,
                      price=it.price, size=it.size, leg=it.leg, created_at=now,
                      expires_at=now + self.cfg.gtd_minutes * 60 - 60)   # 官方：GTD 提早一分鐘失效
            self.orders[o.id] = o
            out.append(o)
        return out

    async def cancel(self, ids: list[str]) -> None:
        for i in ids:
            self.orders.pop(i, None)

    async def cancel_all(self) -> None:
        self.orders.clear()

    async def collateral(self) -> float | None:
        return None                      # 模擬沒有餘額限制

    async def positions(self) -> dict[tuple[str, str], float]:
        return {}

    async def merge(self, condition_id: str) -> bool:
        return True                      # 模擬：直接當作 merge 成功

    async def scoring_fraction(self, order_ids: list[str]) -> float | None:
        return None

    def on_trades(self, trades_by_market: dict[str, list[Trade]]) -> None:
        for cid, trades in trades_by_market.items():
            info = self.markets.get(cid)
            if info is None:
                continue
            mine = [o for o in self.orders.values() if o.condition_id == cid]
            for t in sorted(trades, key=lambda x: x.ts):
                for o in mine:
                    if o.remaining <= 0 or t.ts < o.created_at:
                        continue
                    leg, level = _yes_level(o, info)
                    if leg == "bid" and t.direction < 0 and t.price <= level + 1e-9:
                        through = t.price < level - 1e-9
                    elif leg == "ask" and t.direction > 0 and t.price >= level - 1e-9:
                        through = t.price > level + 1e-9
                    else:
                        continue
                    qty = min(o.remaining, t.shares * (1.0 if through else self.cfg.paper_fill_at_level))
                    if qty <= 0:
                        continue
                    o.filled += qty
                    self._fills.append(Fill(o.id, cid, o.token, o.side, o.price, qty, t.ts))
            for o in mine:
                if o.remaining <= 1e-9:
                    self.orders.pop(o.id, None)

    async def fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out


class LiveExchange:
    """官方 SDK（polymarket-client）的實盤介面。需要 pip install polymarket-client。

    client 參數只給測試用（注入假的 SDK）；實盤時由 start() 用私鑰建立。
    """

    BATCH = 15          # post_orders 一次最多 15 張
    ORDERS_PER_S = 30.0 # 低於官方的 40 張／秒
    HEARTBEAT_S = 5.0

    def __init__(self, cfg: MMConfig, markets: dict[str, MarketInfo], private_key: str | None = None,
                 wallet: str | None = None, client=None, relayer: tuple[str, str] | None = None):
        self.cfg = cfg
        self.markets = markets
        self._private_key = private_key
        self._relayer = relayer          # (Relayer API key, 地址)：免 gas 的 merge 需要；只下單可以不給
        self.wallet = wallet
        self.client = client
        self._known: dict[str, Order] = {}
        self._seen_fills: set = set()
        self._last_fill_ts = int(time.time()) - 60
        self._hb_task: asyncio.Task | None = None
        self._hb_id = ""
        self._hb_last_ok = 0.0
        self.rejections: list[tuple[float, str, str]] = []

    async def start(self) -> None:
        if self.client is None:
            from polymarket import AsyncSecureClient, RelayerApiKey   # 只有實盤才需要 SDK
            extra = {"api_key": RelayerApiKey(key=self._relayer[0], address=self._relayer[1])} if self._relayer else {}
            self.client = await AsyncSecureClient.create(private_key=self._private_key, wallet=self.wallet, **extra)
        self._private_key = self._relayer = None       # 建立完就不再保留私鑰
        self.wallet = str(getattr(self.client, "wallet", "") or self.wallet or "")
        if not self.wallet:
            raise RuntimeError("讀不到錢包地址，無法辨認自己的成交")
        if await self.client.get_closed_only_mode():
            raise RuntimeError("帳號在 closed-only 模式，不能開新部位")
        await self.client.cancel_all()                 # 從乾淨的狀態開始：先撤掉這個帳號上所有的單
        self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self) -> None:
        # 注意：官方心跳沒有「解除」的方法；停止送心跳後，交易所會在 10 秒後撤掉剩下的單
        if self._hb_task:
            self._hb_task.cancel()
        if self.client is not None and hasattr(self.client, "close"):
            await self.client.close()

    def heartbeat_ok(self) -> bool:
        return time.time() - self._hb_last_ok < 3 * self.HEARTBEAT_S

    async def _heartbeat_loop(self) -> None:
        from polymarket import OrderHeartbeatMismatchError
        while True:
            try:
                try:
                    hb = await self.client.send_order_heartbeat(heartbeat_id=self._hb_id)
                except OrderHeartbeatMismatchError as e:
                    hb = await self.client.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
                self._hb_id = hb.heartbeat_id
                self._hb_last_ok = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 心跳失敗：主迴圈看到 heartbeat_ok() 為 False 會停止下新單
                log.warning("心跳失敗：%s", e)
            await asyncio.sleep(self.HEARTBEAT_S)

    def _leg_of(self, cid: str, token: str, side: str) -> str:
        info = self.markets.get(cid)
        on_yes = info is not None and token == info.yes_token
        return ("bid" if on_yes else "ask") if side == "BUY" else ("ask" if on_yes else "bid")

    async def open_orders(self) -> list[Order]:
        out = []
        async for page in self.client.list_open_orders():
            for o in page.items:
                oid = str(o.id)
                known = self._known.get(oid)
                cid = str(o.condition_id)
                out.append(Order(
                    id=oid, condition_id=cid, token=str(o.asset_id), side=str(o.side),
                    price=float(o.price), size=float(o.original_size), filled=float(o.size_matched),
                    leg=known.leg if known else self._leg_of(cid, str(o.asset_id), str(o.side)),
                    created_at=o.created_at.timestamp(),
                    expires_at=o.expires_at.timestamp() - 60 if o.expires_at else None,
                ))
        return out

    async def place(self, intents: list[OrderIntent], now: float) -> list[Order]:
        expiration = int(now + self.cfg.gtd_minutes * 60)
        signed = []
        for it in intents:
            try:
                so = await self.client.create_limit_order(asset_id=it.token, price=str(it.price), size=str(it.size),
                                                          side=it.side, post_only=self.cfg.post_only, expiration=expiration)
            except Exception as e:      # 例如價格不符合 tick：這張跳過，其他照常
                self.rejections.append((now, "sign", str(e)))
                continue
            signed.append((it, so))
        out = []
        for i in range(0, len(signed), self.BATCH):
            chunk = signed[i:i + self.BATCH]
            if i:
                await asyncio.sleep(self.BATCH / self.ORDERS_PER_S)
            try:
                responses = await self.client.post_orders([so for _, so in chunk])
            except Exception as e:
                self.rejections.append((now, "post", str(e)))
                log.warning("送單失敗：%s", e)
                continue
            for (it, _), r in zip(chunk, responses):
                if getattr(r, "ok", False):
                    o = Order(id=str(r.order_id), condition_id=it.condition_id, token=it.token, side=it.side,
                              price=it.price, size=it.size, leg=it.leg, created_at=now, expires_at=expiration - 60)
                    self._known[o.id] = o
                    out.append(o)
                else:
                    self.rejections.append((now, str(getattr(r, "code", "")), str(getattr(r, "message", ""))))
                    log.info("下單被拒：%s %s", getattr(r, "code", ""), getattr(r, "message", ""))
        return out

    async def cancel(self, ids: list[str]) -> None:
        for i in range(0, len(ids), 3000):
            res = await self.client.cancel_orders(order_ids=ids[i:i + 3000])
            for oid in getattr(res, "canceled", ()):
                self._known.pop(str(oid), None)
            for oid, why in (getattr(res, "not_canceled", None) or {}).items():
                log.info("撤單失敗 %s：%s", oid, why)

    async def cancel_all(self) -> None:
        await self.client.cancel_all()
        self._known.clear()

    async def collateral(self) -> float | None:
        """抵押品（pUSD）餘額，美元。注意這是總餘額：掛單佔用的部分沒有扣掉。"""
        ba = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        return ba.balance / 1e6

    async def positions(self) -> dict[tuple[str, str], float]:
        """交易所記錄的部位：(condition_id, token) → 股數。啟動時載入、之後只用來對帳。"""
        out = {}
        async for page in self.client.list_positions():
            for p in page.items:
                size = float(p.current_size)
                if size > 0:
                    out[(str(p.condition_id), str(p.asset_id))] = size
        return out

    async def merge(self, condition_id: str) -> bool:
        """把成對的 Yes + No 換回抵押品（鏈上交易，最多等 60 秒）。"""
        try:
            handle = await self.client.merge_positions(condition_id=condition_id, amount="max")
            await asyncio.wait_for(handle.wait(), timeout=60)
            return True
        except Exception as e:
            log.warning("merge 失敗 %s：%s", condition_id, e)
            return False

    async def scoring_fraction(self, order_ids: list[str]) -> float | None:
        """抽樣檢查掛單有沒有在拿獎勵分數（掛太遠、太小、或掛得不夠久都不算）。"""
        if not order_ids:
            return None
        res = await self.client.get_orders_scoring(order_ids=order_ids[:200])
        return sum(bool(v) for v in res.values()) / len(res) if res else None

    async def fills(self) -> list[Fill]:
        """自己當 maker 的成交（從帳戶成交紀錄裡找 maker_orders 中屬於自己的那幾筆）。"""
        out = []
        newest = self._last_fill_ts
        me = (self.wallet or "").lower()
        async for page in self.client.list_account_trades(after=str(self._last_fill_ts - 30)):
            for t in page.items:
                ts = t.matched_at.timestamp()
                newest = max(newest, int(ts))
                if str(getattr(t, "status", "")).upper() == "FAILED":   # 鏈上失敗的成交不算（其他差異靠對帳發現）
                    continue
                for mo in t.maker_orders:
                    key = (str(t.id), str(mo.order_id))
                    if key in self._seen_fills or (me and str(mo.maker_address).lower() != me):
                        continue
                    self._seen_fills.add(key)
                    out.append(Fill(str(mo.order_id), str(t.condition_id), str(mo.asset_id), str(mo.side),
                                    float(mo.price), float(mo.matched_amount), ts))
        self._last_fill_ts = newest
        return out
