"""全域風控：什麼情況下全部撤單停止（kill），什麼情況下只暫停下新單（pause）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import MMConfig


@dataclass
class RiskStatus:
    kill: bool = False          # 全部撤單、停止程式
    pause_new: bool = False     # 不下新單（已經掛著的照常撤換）
    reason: str | None = None


def check(cfg: MMConfig, states: dict, api_errors: list[float], heartbeat_ok: bool, now: float | None = None) -> RiskStatus:
    now = now or time.time()
    day_pnl = sum(st.day_pnl() for st in states.values())
    if day_pnl < -cfg.max_daily_loss_frac * cfg.budget:
        return RiskStatus(kill=True, reason=f"全域單日虧損 {day_pnl:,.0f} 超過上限")
    recent = [t for t in api_errors if now - t < 60]
    if len(recent) > cfg.max_api_errors_per_min:
        return RiskStatus(kill=True, reason=f"一分鐘內 API 錯誤 {len(recent)} 次")
    if not heartbeat_ok:
        return RiskStatus(pause_new=True, reason="心跳不正常，暫停下新單")
    return RiskStatus()
