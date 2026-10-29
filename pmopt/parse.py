"""把 Polymarket 的題目文字解析成結構化的規格。

目前支援的題型（Bitcoin / Ethereum 皆可）：
- above      : "Will the price of Bitcoin be above $80,000 on October 31?"
- below      : "Will the price of Bitcoin be below $80,000 on October 31?"
- range      : "Will the price of Bitcoin be between $80,000 and $82,000 on October 31?"
- touch_up   : "Will Bitcoin reach $87,500 in October?"
- touch_down : "Will Bitcoin dip to $77,500 in October?"

時間不從題目解析，而是用 API 給的 endDate（題目文字的日期格式太多種，容易出錯）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ASSETS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH"}
_NUM = r"\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([kK])?"
_ASSET = r"(bitcoin|btc|ethereum|eth)"

_PATTERNS = [
    ("range", re.compile(rf"price of {_ASSET} be between {_NUM} and {_NUM}", re.I)),
    ("above", re.compile(rf"price of {_ASSET} be (?:above|greater than|higher than|over) {_NUM}", re.I)),
    ("below", re.compile(rf"price of {_ASSET} be (?:below|less than|lower than|under) {_NUM}", re.I)),
    ("touch_up", re.compile(rf"will {_ASSET} (?:reach|hit) {_NUM}", re.I)),
    ("touch_down", re.compile(rf"will {_ASSET} (?:dip to|fall to|drop to) {_NUM}", re.I)),
]


@dataclass(frozen=True)
class MarketSpec:
    kind: str           # above / below / range / touch_up / touch_down
    asset: str          # BTC / ETH
    strike: float       # 履約價或觸價價位；range 時是下界
    strike_hi: float | None = None  # range 的上界


def _to_number(digits: str, k_suffix: str | None) -> float:
    x = float(digits.replace(",", ""))
    return x * 1000.0 if k_suffix else x


def parse_question(question: str) -> MarketSpec | None:
    q = " ".join(question.split())
    for kind, pat in _PATTERNS:
        m = pat.search(q)
        if not m:
            continue
        asset = _ASSETS[m.group(1).lower()]
        if kind == "range":
            lo = _to_number(m.group(2), m.group(3))
            hi = _to_number(m.group(4), m.group(5))
            lo, hi = min(lo, hi), max(lo, hi)
            return MarketSpec(kind, asset, lo, hi)
        return MarketSpec(kind, asset, _to_number(m.group(2), m.group(3)))
    return None
