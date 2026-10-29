"""即時資料：一次抓好 Deribit 期權鏈 + 指數 + Polymarket 市場，並記錄兩邊的時間差。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import requests

from . import deribit, polymarket
from .surface import VolSurface


@dataclass
class LiveSnapshot:
    now: datetime
    surface: VolSurface
    markets: pd.DataFrame
    index_price: float
    sync_gap_sec: float  # Polymarket 與 Deribit 抓取時間的差距（秒），越小越好


def fetch_snapshot(asset: str = "BTC", slugs: list[str] | None = None, days_ahead: int = 7,
                   with_depth: bool = False, verbose: bool = False) -> LiveSnapshot:
    s = requests.Session()
    s.headers["User-Agent"] = "pmopt-classroom-demo/0.1"

    t0 = time.time()
    if slugs is None:
        name = {"BTC": "bitcoin", "ETH": "ethereum"}[asset]
        slugs = polymarket.candidate_event_slugs(datetime.now(timezone.utc), days_ahead, (name,))
    markets = polymarket.fetch_markets(slugs, s, verbose=verbose)
    if with_depth and not markets.empty:
        tops = []
        for tok in markets["token_yes"]:
            try:
                tops.append(polymarket.fetch_book_top(tok, s) if tok else {})
            except requests.RequestException:
                tops.append({})
        top = pd.DataFrame(tops, index=markets.index)
        for c in ("best_bid", "best_ask"):
            if c in top:
                markets[c] = top[c].combine_first(markets[c])
        for c in ("bid_size", "ask_size"):
            markets[c] = top[c] if c in top else None
    t_pm = time.time()

    chain, asof = deribit.fetch_option_summaries(asset, s)
    index_price = deribit.fetch_index_price(asset, s)
    t_der = time.time()

    now = datetime.now(timezone.utc)
    surface = VolSurface.from_deribit(chain, now, spot=index_price)
    gap = abs(((t_pm + t0) / 2) - t_der)
    return LiveSnapshot(now=now, surface=surface, markets=markets, index_price=index_price, sync_gap_sec=gap)
