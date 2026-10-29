from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from pmopt.__main__ import main
from pmopt.polymarket import candidate_event_slugs, fetch_book_top, load_snapshot, normalize_market
from pmopt.scanner import ScanConfig, fit_center, scan
from pmopt.surface import VolSurface

SNAP = Path(__file__).resolve().parents[1] / "data" / "snapshot_2026-10-29"
NOW = datetime(2026, 10, 29, 5, 59, 14, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def snapshot():
    surf = VolSurface.from_deribit(pd.read_csv(SNAP / "deribit_btc_options.csv"), NOW)
    markets = load_snapshot(SNAP / "polymarket_markets.csv")
    return surf, markets


def test_scan_skips_closed_markets_and_prices_everything_else(snapshot):
    surf, markets = snapshot
    res = scan(markets, surf, NOW)
    assert len(res) == int((~markets["closed"]).sum())
    assert res["fair"].between(0, 1).all()


def test_above_ladder_fair_is_decreasing(snapshot):
    surf, markets = snapshot
    res = scan(markets, surf, NOW).query("kind == 'above'").sort_values("strike")
    assert res["fair"].is_monotonic_decreasing


def test_edge_definitions(snapshot):
    surf, markets = snapshot
    r = scan(markets, surf, NOW).iloc[0]
    assert r["edge_buy_yes"] == pytest.approx(r["fair"] - r["ask"], abs=1e-4)
    assert r["edge_sell_yes"] == pytest.approx(r["bid"] - r["fair"], abs=1e-4)


def test_fees_reduce_edge(snapshot):
    surf, markets = snapshot
    a = scan(markets, surf, NOW, ScanConfig(taker_fee_coeff=0.0))
    b = scan(markets, surf, NOW, ScanConfig(taker_fee_coeff=0.07))
    assert (b["best_edge"] <= a["best_edge"] + 1e-12).all()


def test_fit_center_recovers_a_known_shift(snapshot):
    """造一份「Polymarket 剛好等於期權公允值，但期權中心價格偏移 1%」的假資料，看看能不能找回 1%。"""
    surf, markets = snapshot
    truth = scan(markets, surf.shifted(1.01), NOW).query("kind == 'above'")
    fake = markets[markets["question"].str.contains("above")].copy().reset_index(drop=True)
    fake["best_bid"] = truth["fair"].to_numpy() - 0.005
    fake["best_ask"] = truth["fair"].to_numpy() + 0.005
    assert fit_center(fake, surf, NOW) == pytest.approx(1.01, abs=2e-4)


def test_candidate_slugs_use_eastern_time():
    # 2026-11-01 03:00 UTC 在美東仍是 10/31 晚上
    slugs = candidate_event_slugs(datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc), days_ahead=1)
    assert "bitcoin-above-on-october-31-2026" in slugs
    assert "bitcoin-above-on-november-1-2026" in slugs
    assert "what-price-will-bitcoin-hit-in-october-2026" in slugs
    assert "what-price-will-bitcoin-hit-in-november-2026" in slugs


def test_normalize_market_parses_token_ids_string():
    m = {"question": "Will the price of Bitcoin be above $80,000 on October 31?", "endDate": "2026-10-31T16:00:00Z",
         "bestBid": "0.61", "bestAsk": 0.64, "closed": False, "clobTokenIds": '["111", "222"]'}
    row = normalize_market(m, "slug")
    assert row["token_yes"] == "111" and row["best_bid"] == 0.61 and row["best_ask"] == 0.64


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *a, **k):
        return _FakeResp(self.payload)


def test_book_top_picks_best_prices_regardless_of_order():
    book = {"bids": [{"price": "0.58", "size": "100"}, {"price": "0.61", "size": "40"}],
            "asks": [{"price": "0.66", "size": "10"}, {"price": "0.64", "size": "25"}]}
    top = fetch_book_top("tok", _FakeSession(book))
    assert top == dict(best_bid=0.61, bid_size=40.0, best_ask=0.64, ask_size=25.0)


def test_collect_and_summarize_with_mocked_network(tmp_path, monkeypatch, snapshot):
    """不連網，把 fetch_snapshot 換成回傳離線快照，跑完整的 collect → summarize 流程。"""
    import pmopt.live as live

    surf, markets = snapshot
    fake = live.LiveSnapshot(now=NOW, surface=surf, markets=markets, index_price=80_165.68, sync_gap_sec=0.4)
    monkeypatch.setattr(live, "fetch_snapshot", lambda *a, **k: fake)
    out_csv = tmp_path / "scans.csv"
    assert main(["collect", "--minutes", "0.02", "--interval", "0.4", "--out", str(out_csv)]) == 0
    df = pd.read_csv(out_csv)
    assert {"ts_utc", "sync_gap_sec", "center_factor", "best_edge_centered"} <= set(df.columns)
    pairs = pd.read_csv(tmp_path / "scans_pairs.csv")
    assert {"ts_utc", "pair", "slack", "p_gap", "ev"} <= set(pairs.columns)
    assert main(["summarize", str(out_csv), "--out", str(tmp_path / "summary")]) == 0
    assert (tmp_path / "summary" / "edge_by_bucket.png").exists()
    assert (tmp_path / "summary" / "pairs_summary.csv").exists()


def test_cli_offline_demo_runs(tmp_path, capsys):
    assert main(["scan", "--snapshot", str(SNAP), "--out", str(tmp_path), "--plot"]) == 0
    assert (tmp_path / "scan_raw.csv").exists() and (tmp_path / "scan_centered.csv").exists()
    assert (tmp_path / "ladder_polymarket_vs_options.png").exists()
    assert "中心價格" in capsys.readouterr().out


def test_cli_check_runs(tmp_path, capsys):
    assert main(["check", "--snapshot", str(SNAP), "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "沒有任何一組的成本低於 $1" in out and "77,500–78,000" in out
    assert (tmp_path / "pairs_touch_vs_terminal.csv").exists()
