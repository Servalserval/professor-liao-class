"""公開資料（不需要帳號）：獎勵市場清單、批次訂單簿、Gamma 市場細節、多市場成交輪詢。

速率限制（官方文件）：POST /books 500 次／10 秒；data-api /v2/trades 300 次／10 秒、
每次最多 20 個市場；gamma /markets 300 次／10 秒。這裡的用量遠低於上限。
token id 一律當字串處理（轉成浮點數會失真）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

from .rules import Book, Trade

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


class PublicData:
    """workers > 1 時，訂單簿與成交會用多個執行緒同時抓（每個執行緒各自一個連線）。"""

    def __init__(self, session: requests.Session | None = None, timeout: float = 20.0, workers: int = 12):
        self._fixed = session
        self._local = threading.local()
        self.timeout = timeout
        self.workers = workers
        self.errors: deque = deque(maxlen=10_000)     # 最近的錯誤時間，給風控用

    @property
    def s(self) -> requests.Session:
        if self._fixed is not None:
            return self._fixed
        if not hasattr(self._local, "s"):
            self._local.s = requests.Session()
            self._local.s.headers["User-Agent"] = "pmopt-mm/0.1"
        return self._local.s

    def map(self, fn, items: list) -> list:
        if self.workers <= 1 or len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=min(self.workers, len(items))) as ex:
            return list(ex.map(fn, items))

    def _get(self, url, **params):
        try:
            r = self.s.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            self.errors.append(time.time())
            raise

    def reward_markets(self, max_pages: int = 400) -> list[dict]:
        """所有有獎勵的市場（依每日獎金由高到低，獎金變成 0 就停）。"""
        rows, cursor = [], ""
        for _ in range(max_pages):
            params = {"order_by": "rate_per_day", "position": "DESC", "page_size": 500}
            if cursor:
                params["next_cursor"] = cursor
            d = self._get(f"{CLOB}/rewards/markets/multi", **params)
            page = d.get("data") or []
            rates = [sum(float(c.get("rate_per_day") or 0) for c in (m.get("rewards_config") or [])) for m in page]
            rows += [m for m, r in zip(page, rates) if r > 0]
            cursor = d.get("next_cursor")
            if not cursor or cursor == "LTE=" or not rates or max(rates) == 0:
                break
        return rows

    def books(self, tokens: list[str]) -> dict[str, Book]:
        def fetch(chunk):
            try:
                r = self.s.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=self.timeout)
                r.raise_for_status()
                return time.time(), r.json()
            except (requests.RequestException, ValueError):
                self.errors.append(time.time())
                return time.time(), []

        out = {}
        for now, rows in self.map(fetch, [tokens[i:i + 500] for i in range(0, len(tokens), 500)]):
            for b in rows:
                out[str(b["asset_id"])] = Book(
                    ts=now,
                    bids=tuple((float(x["price"]), float(x["size"])) for x in b.get("bids", [])),
                    asks=tuple((float(x["price"]), float(x["size"])) for x in b.get("asks", [])),
                    tick=float(b.get("tick_size") or 0.01),
                    min_order_size=float(b.get("min_order_size") or 5),
                )
        return out

    def gamma_markets(self, condition_ids: list[str]) -> dict[str, dict]:
        """比賽開始時間、截止日、手續費設定、事件 slug。"""
        out = {}
        for i in range(0, len(condition_ids), 50):
            chunk = condition_ids[i:i + 50]
            try:
                r = self.s.get(f"{GAMMA}/markets", params=[("condition_ids", c) for c in chunk] + [("limit", 100)],
                               timeout=self.timeout)
                r.raise_for_status()
            except requests.RequestException:
                self.errors.append(time.time())
                continue
            for m in r.json():
                out[m["conditionId"]] = m
        return out


class TradePoller:
    """多市場成交輪詢：每次最多 20 個市場，只抓上次之後的新成交，轉成 Yes 的價格與方向。"""

    OVERLAP_S = 120     # 往回多抓一段（成交索引可能延遲），重複的用交易 hash 去重

    def __init__(self, data: PublicData, yes_token_of: dict[str, str], lookback_s: int = 120):
        self.data = data
        self.yes_token_of = dict(yes_token_of)      # condition_id → Yes token
        self.last_ts = int(time.time()) - lookback_s
        self.seen: deque = deque(maxlen=200_000)
        self._seen_set: set = set()
        self._first = True

    def set_markets(self, yes_token_of: dict[str, str]) -> None:
        self.yes_token_of = dict(yes_token_of)

    def _fetch(self, chunk: list[str], start: int, max_pages: int, limit: int) -> list[dict]:
        """由新到舊一頁一頁抓，抓到比 start 舊的成交就停。

        注意：官方文件寫明 start／end 只對 user 查詢有效，用 condition 查詢時會被忽略，所以要自己判斷何時停。
        """
        rows, cursor = [], None
        for _ in range(max_pages):
            params = {"condition": ",".join(chunk), "limit": limit}
            if cursor:
                params["cursor"] = cursor
            try:
                j = self.data._get(f"{DATA}/v2/trades", **params)
            except (requests.RequestException, ValueError):
                break
            page = j.get("data") or []
            rows += [t for t in page if int(t["timestamp"]) >= start]
            cursor = (j.get("pagination") or {}).get("next_cursor")
            if not cursor or not page or min(int(t["timestamp"]) for t in page) < start:
                break
        return rows

    def poll(self, max_pages: int = 20) -> dict[str, list[Trade]]:
        start = self.last_ts - self.OVERLAP_S
        newest = self.last_ts
        out: dict[str, list[Trade]] = {}
        cids = list(self.yes_token_of)
        chunks = [cids[i:i + 20] for i in range(0, len(cids), 20)]
        mapper = getattr(self.data, "map", lambda fn, xs: [fn(x) for x in xs])
        # 第一次（回補）用大頁；之後每 10 秒只有少量新成交，小頁比較快
        limit = 1000 if self._first else 200
        self._first = False
        for rows in mapper(lambda c: self._fetch(c, start, max_pages, limit), chunks):
            for t in rows:
                key = (t.get("transaction_hash"), t.get("token_id"), t.get("size"), t.get("price"), t.get("timestamp"))
                if key in self._seen_set:
                    continue
                if len(self.seen) == self.seen.maxlen:
                    self._seen_set.discard(self.seen[0])
                self.seen.append(key)
                self._seen_set.add(key)
                cid = t["condition_id"]
                yes = self.yes_token_of.get(cid)
                if yes is None:
                    continue
                p, ts = float(t["price"]), int(t["timestamp"])
                if str(t["token_id"]) == yes:
                    price, direction = p, (1 if t["side"] == "BUY" else -1)
                else:
                    price, direction = 1 - p, (-1 if t["side"] == "BUY" else 1)
                out.setdefault(cid, []).append(Trade(ts, price, direction, float(t["size"])))
                newest = max(newest, ts)
        self.last_ts = newest
        return out
