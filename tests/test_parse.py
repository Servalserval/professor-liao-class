import pytest

from pmopt.parse import MarketSpec, parse_question


@pytest.mark.parametrize(
    "q, expected",
    [
        ("Will the price of Bitcoin be above $80,000 on October 31?", MarketSpec("above", "BTC", 80_000)),
        ("Will the price of Bitcoin be above $80000 on October 31?", MarketSpec("above", "BTC", 80_000)),
        ("Will the price of Ethereum be above $2,350 on November 6?", MarketSpec("above", "ETH", 2_350)),
        ("Will the price of Bitcoin be below $76k on October 31?", MarketSpec("below", "BTC", 76_000)),
        ("Will the price of Bitcoin be between $80,000 and $82,000 on October 31?",
         MarketSpec("range", "BTC", 80_000, 82_000)),
        ("Will Bitcoin reach $87,500 in October?", MarketSpec("touch_up", "BTC", 87_500)),
        ("Will Bitcoin dip to $77,500 in October?", MarketSpec("touch_down", "BTC", 77_500)),
        ("Will Ethereum reach $3,000 in November?", MarketSpec("touch_up", "ETH", 3_000)),
    ],
)
def test_parse_supported(q, expected):
    assert parse_question(q) == expected


@pytest.mark.parametrize("q", [
    "Will the Fed cut rates in December?",
    "Bitcoin Up or Down - October 29, 2AM ET",
    "Will Solana reach $300 in October?",
])
def test_parse_unsupported_returns_none(q):
    assert parse_question(q) is None
