"""Deribit 公開 API（不需要 API key）。

文件：https://docs.deribit.com/
- public/get_book_summary_by_currency：一次拿到某幣種所有期權的報價摘要（含 mark_iv）
- public/get_index_price：現貨指數
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests

BASE = "https://www.deribit.com/api/v2/public"
COLUMNS = ["instrument_name", "mark_iv", "underlying_price", "bid_price", "ask_price", "mark_price"]


def fetch_option_summaries(currency: str = "BTC", session: requests.Session | None = None) -> tuple[pd.DataFrame, datetime]:
    """回傳 (期權報價表, 資料時間)。"""
    s = session or requests.Session()
    r = s.get(f"{BASE}/get_book_summary_by_currency", params={"currency": currency, "kind": "option"}, timeout=20)
    r.raise_for_status()
    result = r.json()["result"]
    df = pd.DataFrame(result)
    ts_ms = int(df["creation_timestamp"].max()) if "creation_timestamp" in df else None
    asof = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) if ts_ms else datetime.now(timezone.utc)
    return df[[c for c in COLUMNS if c in df.columns]], asof


def fetch_index_price(currency: str = "BTC", session: requests.Session | None = None) -> float:
    s = session or requests.Session()
    r = s.get(f"{BASE}/get_index_price", params={"index_name": f"{currency.lower()}_usd"}, timeout=10)
    r.raise_for_status()
    return float(r.json()["result"]["index_price"])


def load_snapshot(path) -> pd.DataFrame:
    return pd.read_csv(path)
