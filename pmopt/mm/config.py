"""掛單系統的所有參數。預設值來自 docs/market-making.md 的回測（規則 F1，拿掉波動暫停）。

可以用 JSON 檔覆蓋任何欄位：python -m pmopt mm-paper --config my.json
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _default_hurdle() -> dict[str, float]:
    # 每 $1 每天的預期成交虧損（只算虧損部分的平均），各類別往全體平均收縮後的值
    return {"_": 0.00302, "其他": 0.00435, "政治": 0.00256, "總經": 0.00275, "運動／電競": 0.00252, "加密貨幣": 0.00302}


def _default_coverage() -> dict[str, float]:
    # 回測裡規則 F1 的平均掛單時間比例
    return {"_": 0.83, "其他": 0.72, "政治": 0.85, "總經": 0.91, "運動／電競": 0.95, "加密貨幣": 0.83}


def _default_freeze() -> list[dict]:
    # 已排定的資訊事件：期間內符合關鍵字的市場一律不掛。請依實際日曆自行增減。
    return [dict(name="美國期中選舉開票", start="2026-11-03T12:00:00Z", end="2026-11-06T00:00:00Z",
                 keywords=["Senate", "House", "governor", "Governor", "Balance of Power", "midterm", "Midterm"])]


@dataclass
class MMConfig:
    # ---------- 資金與選市場 ----------
    budget: float = 350_000.0          # 總本金上限（美元）；研究建議 $1M 先投入約 30–35 萬
    market_cap: float = 20_000.0       # 單一市場上限
    event_cap: float = 30_000.0        # 單一事件（同一個 event slug）上限
    hurdle_mult: float = 2.0           # 進場門檻 = 預期虧損 × 這個倍數 + 資金成本；預算不夠時會自動調高
    hurdle_by_category: dict = field(default_factory=_default_hurdle)
    coverage_by_category: dict = field(default_factory=_default_coverage)
    cost_of_capital: float = 0.04      # 年化
    min_days_left: float = 1.0         # 剩不到這麼多天到期的市場不放
    mid_band: tuple = (0.03, 0.97)     # 中價太接近 0 或 1 不放
    max_markets: int | None = None     # 分階段上線時，限制市場數
    exclude_categories: list = field(default_factory=list)

    # ---------- 掛單規則（F1） ----------
    dist_frac: float = 0.7             # 掛在離中價 0.7 × 最大合格價差
    stop_loss_frac: float = 0.025      # 單一市場當天虧損 > 本金 × 2.5% → 停到 UTC 午夜
    cooldown_min: int = 30             # 跳價（1 分鐘移動 > 掛單距離）或被吃之後，暫停幾分鐘
    vol_window_min: int = 0            # 波動暫停（0 = 關閉；回測裡關掉略好）
    vol_mult: float = 2.0
    volu_window_min: int = 10          # 成交量暫停：過去 10 分鐘成交量 > 5 × 過去 24 小時的平均
    volu_mult: float = 5.0
    trail_hours: int = 6               # 過去 6 小時「假設一直掛」的淨額 < 0 → 暫停
    markout_min: int = 10              # 影子帳：成交後幾分鐘結算
    unwind_cost: float = 0.005         # 影子帳：每股平倉成本
    warmup_min: int = 30               # 新加入的市場先觀察幾分鐘才開始掛
    sports_stop_before_min: int = 30   # 運動盤：比賽開始前幾分鐘停掛
    stop_before_end_hours: float = 0.0 # 截止日前幾小時停掛（回測顯示幫助不大，預設只在截止後停）
    freeze_windows: list = field(default_factory=_default_freeze)

    # ---------- 執行 ----------
    tick_seconds: float = 10.0         # 主迴圈間隔
    universe_refresh_min: int = 60     # 每隔多久重新選市場、配本金
    requote_frac: float = 0.25         # 目標價移動超過 max(0.25 × 掛單距離, 1 tick) 才撤單重掛（減少洗單，維持獎勵計分）
    gtd_minutes: int = 60              # 每張單 60 分鐘後自動失效（程式當掉時的第二道保險；第一道是 10 秒的心跳）
    gtd_renew_minutes: int = 5         # 剩不到 5 分鐘就續掛（20 分鐘的話每張單每 14 分鐘就要重掛一次，太常重新計時）
    post_only: bool = True             # 只掛單不吃單
    max_inventory_frac: float = 0.5    # 單一市場庫存價值 > 本金 50% → 只掛減倉那一邊
    auto_merge: bool = False           # 同時持有 Yes 與 No 時自動 merge 回現金（實盤需要錢包支援免 gas 交易）
    max_new_orders_per_tick: int = 150 # 每一輪最多下幾張新單（官方上限 40 張／秒；暖機結束時會一次冒出很多市場）

    # ---------- 全域風控 ----------
    max_daily_loss_frac: float = 0.01  # 全部市場當天虧損 > 總本金 1% → 全部撤單、停止
    stale_data_seconds: float = 60.0   # 訂單簿超過這麼久沒更新 → 該市場撤單
    max_api_errors_per_min: int = 30   # API 錯誤太多 → 全部撤單、停止
    paper_fill_at_level: float = 0.5   # 模擬：成交價剛好等於我的價位時，假設成交一半

    # ---------- 記錄 ----------
    log_dir: str = "data/mm_logs"

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides) -> "MMConfig":
        data = {}
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.update({k: v for k, v in overrides.items() if v is not None})
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"設定檔有不認得的欄位：{sorted(unknown)}")
        if "mid_band" in data:
            data["mid_band"] = tuple(data["mid_band"])
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)
