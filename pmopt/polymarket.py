"""Polymarket 公開 API（讀資料不需要 API key）。

- Gamma API：市場清單、題目、結算時間、最佳買賣價
  GET https://gamma-api.polymarket.com/events?slug=<event-slug>
- CLOB API：完整訂單簿（含每一檔的掛單量）
  GET https://clob.polymarket.com/book?token_id=<token-id>

Polymarket 的 BTC 價格盤有固定的網址格式，例如：
- bitcoin-above-on-october-31-2026              （每天一場：指定時間收盤價高於 K？）
- what-price-will-bitcoin-hit-in-october-2026   （每月一場：當月會不會碰到某價位？）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ET = ZoneInfo("America/New_York")
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]


def candidate_event_slugs(now: datetime, days_ahead: int = 7, assets: tuple[str, ...] = ("bitcoin",)) -> list[str]:
    """依照 Polymarket 的命名慣例，猜出接下來幾天可能存在的事件網址。

    Polymarket 用美東時間（ET）定義日期，所以先把 now 換成 ET。
    同一種事件有時有年份後綴、有時沒有，兩種都試。
    """
    today = now.astimezone(ET).date()
    slugs: list[str] = []
    for asset in assets:
        for d in range(days_ahead + 1):
            day = today + timedelta(days=d)
            base = f"{asset}-above-on-{_MONTH_NAMES[day.month - 1]}-{day.day}"
            slugs += [f"{base}-{day.year}", base]
        for offset in (0, 1):
            m = (today.month - 1 + offset) % 12
            y = today.year + (today.month - 1 + offset) // 12
            slugs.append(f"what-price-will-{asset}-hit-in-{_MONTH_NAMES[m]}-{y}")
    return slugs


def _parse_token_ids(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(x) for x in raw]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize_market(m: dict, event_slug: str) -> dict:
    tokens = _parse_token_ids(m.get("clobTokenIds"))
    return dict(
        event_slug=event_slug,
        question=m.get("question", ""),
        end_date=m.get("endDate"),
        best_bid=_f(m.get("bestBid")),
        best_ask=_f(m.get("bestAsk")),
        closed=bool(m.get("closed", False)),
        token_yes=tokens[0] if tokens else None,
    )


def fetch_event_markets(slug: str, session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    r = s.get(f"{GAMMA}/events", params={"slug": slug}, timeout=15)
    r.raise_for_status()
    rows = []
    for ev in r.json():
        for m in ev.get("markets", []):
            rows.append(normalize_market(m, slug))
    return rows


def fetch_markets(slugs: list[str], session: requests.Session | None = None, verbose: bool = False) -> pd.DataFrame:
    s = session or requests.Session()
    rows, seen = [], set()
    for slug in slugs:
        try:
            got = fetch_event_markets(slug, s)
        except requests.RequestException as e:
            if verbose:
                print(f"  [polymarket] {slug}: {e}")
            continue
        for row in got:
            key = (row["question"], row["end_date"])
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if verbose and got:
            print(f"  [polymarket] {slug}: {len(got)} 個市場")
    return pd.DataFrame(rows, columns=["event_slug", "question", "end_date", "best_bid", "best_ask", "closed", "token_yes"])


def fetch_book_top(token_id: str, session: requests.Session | None = None) -> dict:
    """從 CLOB 訂單簿拿最佳買價/賣價和該價位的掛單量（股數）。"""
    s = session or requests.Session()
    r = s.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    book = r.json()
    bids = [(float(b["price"]), float(b["size"])) for b in book.get("bids", [])]
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    best_bid = max(bids, default=(None, None), key=lambda x: x[0])
    best_ask = min(asks, default=(None, None), key=lambda x: x[0])
    return dict(best_bid=best_bid[0], bid_size=best_bid[1], best_ask=best_ask[0], ask_size=best_ask[1])


def load_snapshot(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["closed"] = df["closed"].astype(str).str.lower().eq("true")
    return df


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
