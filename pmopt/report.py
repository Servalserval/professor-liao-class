"""畫圖與彙整。

配色：藍色 = 期權（Deribit）推算的公允值，橘色 = Polymarket 報價。
兩個系列都會有圖例，文字一律用中性色，不用系列色。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .pricing import digital_call_smile  # noqa: E402
from .surface import VolSurface, year_frac  # noqa: E402

BLUE = "#2a78d6"      # 期權公允值
ORANGE = "#eb6834"    # Polymarket
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"

plt.rcParams.update({
    "font.family": "sans-serif",
    # 依序找第一個有中文字的字型；Linux 常見的是 Noto CJK 或文泉驛
    "font.sans-serif": ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC", "PingFang TC", "Heiti TC",
                        "Microsoft JhengHei", "WenQuanYi Zen Hei", "Arial Unicode MS", "DejaVu Sans"],
    "axes.unicode_minus": False,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT_2,
    "axes.titlecolor": TEXT,
    "xtick.color": TEXT_2,
    "ytick.color": TEXT_2,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 1.0,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "font.size": 11,
})


def _ladder_panel(ax, scan_df: pd.DataFrame, surface: VolSurface, now: datetime, end: datetime, title: str):
    T = year_frac(now, end)
    F = surface.forward(T)
    ks = np.linspace(scan_df["strike"].min() - 1000, scan_df["strike"].max() + 1000, 300)
    fair = [digital_call_smile(F, k, T, lambda x: surface.vol(x, T)) for k in ks]
    ax.plot(ks, fair, color=BLUE, lw=2, solid_capstyle="round", label="期權推算的公允機率（Deribit）")
    d = scan_df.dropna(subset=["bid", "ask"])
    ax.vlines(d["strike"], d["bid"], d["ask"], color=ORANGE, lw=4, capstyle="round", zorder=3,
              label="Polymarket 買價–賣價（點為中價）")
    ax.scatter(d["strike"], (d["bid"] + d["ask"]) / 2, s=30, color=ORANGE, edgecolor=SURFACE, linewidth=1.5, zorder=4)
    ax.axvline(F, color=TEXT_2, lw=1, alpha=0.6)
    ax.text(F, 1.02, f"期權 forward {F:,.0f}", color=TEXT_2, ha="center", va="bottom", fontsize=9,
            transform=ax.get_xaxis_transform())
    ax.set_title(title, loc="left", fontsize=12, pad=18)
    ax.set_xlabel("履約價 K（美元）")
    ax.set_ylim(-0.02, 1.02)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x/1000:.0f}K"))


def plot_snapshot(scan_raw: pd.DataFrame, scan_centered: pd.DataFrame, surface: VolSurface,
                  surface_centered: VolSurface, now: datetime, end: datetime, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    raw = scan_raw[scan_raw["kind"] == "above"]
    cen = scan_centered[scan_centered["kind"] == "above"]

    # 圖一：到期價格盤，原始 vs 把中心價格對齊之後
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True)
    shift = surface_centered.spot - surface.spot
    _ladder_panel(axes[0], raw, surface, now, end, "① 原始比較")
    _ladder_panel(axes[1], cen, surface_centered, now, end, f"② 期權中心價格平移 {shift:+,.0f} 美元之後")
    axes[0].set_ylabel("「收盤價高於 K」的機率")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.02))
    fig.suptitle(f"BTC 在 {end:%m/%d %H:%M} UTC 收盤高於 K 的機率：Polymarket vs 期權市場",
                 x=0.01, ha="left", fontsize=13, color=TEXT)
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    p = out_dir / "ladder_polymarket_vs_options.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    paths.append(p)

    # 圖二：隱含波動率，同一個市場兩邊各自隱含多少波動率
    d = cen.dropna(subset=["pm_iv"])
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    ax.plot(d["strike"], d["deribit_vol"], color=BLUE, lw=2, marker="o", ms=7,
            markeredgecolor=SURFACE, markeredgewidth=2, label="Deribit 期權的隱含波動率")
    ax.plot(d["strike"], d["pm_iv"], color=ORANGE, lw=2, marker="o", ms=7,
            markeredgecolor=SURFACE, markeredgewidth=2, label="Polymarket 中價反推的波動率")
    ax.set_xlabel("履約價 K（美元）")
    ax.set_ylabel("年化波動率（%）")
    ax.set_ylim(0, max(d["pm_iv"].max(), d["deribit_vol"].max()) * 1.15)
    ax.set_xticks(d["strike"])
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x/1000:g}K"))
    ax.set_title("對齊中心價格後，Polymarket 隱含的波動率普遍比期權高", loc="left", fontsize=12)
    ax.legend(loc="upper center", ncol=1)
    fig.tight_layout()
    p = out_dir / "implied_vol_comparison.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    paths.append(p)
    return paths


def summarize_collected(csv_path: Path, out_dir: Path, min_edge: float = 0.01) -> pd.DataFrame:
    """把 collect 收集到的資料彙整：依公允機率分桶，看錯價的大小和持續程度。"""
    df = pd.read_csv(csv_path)
    df["fair_bucket"] = pd.cut(df["fair"], [0, 0.05, 0.2, 0.5, 0.8, 0.95, 1.0], include_lowest=True)
    col = "best_edge_centered" if "best_edge_centered" in df.columns else "best_edge"
    g = df.groupby("fair_bucket", observed=True)[col]
    summary = pd.DataFrame({
        "樣本數": g.size(),
        "平均 edge": g.mean().round(4),
        "edge 中位數": g.median().round(4),
        f"edge ≥ {min_edge} 的比例": g.apply(lambda s: (s >= min_edge).mean()).round(3),
    })
    out_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out_dir / "summary_by_bucket.csv")

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    buckets = [str(b) for b in summary.index]
    ax.bar(buckets, summary["平均 edge"], width=0.35, color=BLUE)
    ax.axhline(0, color=TEXT_2, lw=1)
    ax.grid(axis="x", visible=False)
    ax.set_axisbelow(True)
    ax.set_xlabel("期權推算的公允機率（分桶）")
    ax.set_ylabel("平均可捕捉 edge（美元／股）")
    ax.set_title(f"{len(df):,} 筆觀測：哪個機率區間的錯價最大？", loc="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_dir / "edge_by_bucket.png", dpi=160)
    plt.close(fig)
    return summary


def summarize_pairs(csv_path: Path, out_dir: Path) -> pd.DataFrame:
    """彙整 collect 記下的 Polymarket 內部組合：哪些組合常常便宜？便宜多久？"""
    df = pd.read_csv(csv_path)
    g = df.groupby(["relation", "pair"])
    summary = pd.DataFrame({
        "觀測次數": g.size(),
        "成本 < $1 的比例": g["slack"].apply(lambda s: (s > 0).mean()).round(3),
        "slack 中位數": g["slack"].median().round(4),
        "slack 最大值": g["slack"].max().round(4),
    })
    if "p_gap" in df.columns:
        summary["p_gap 中位數"] = g["p_gap"].median().round(4)
        summary["ev 中位數"] = g["ev"].median().round(4)
    summary = summary.sort_values(["成本 < $1 的比例", "slack 中位數"], ascending=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out_dir / "pairs_summary.csv")
    return summary


def plot_pair_payoff(row: dict, out_dir: Path) -> Path:
    """畫「觸價盤 + 到期盤」組合在到期時的報酬：x 軸是到期盤那根 K 線的收盤價，兩條線是兩種路徑。

    row 需要：pair, _touch_kind, _B, _K, cost_fee, p_gap, gap_lo, gap_hi（evaluate_near_arbs 的輸出）。
    """
    B, K, cost = float(row["_B"]), float(row["_K"]), float(row["cost_fee"])
    down = row["_touch_kind"] == "touch_down"
    lo, hi = min(B, K) - 2_500, max(B, K) + 2_500
    xs = np.linspace(lo, hi, 2_001)
    if down:
        # 曾碰到 B：觸價腳付 $1，收盤 > K 時到期腳再付 $1；沒碰到 B：收盤必定 > B，只有到期腳
        touched = 1.0 + (xs > K)
        untouched = np.where(xs > B, (xs > K).astype(float), np.nan)
        leg2 = f"高於 {K:,.0f} Yes"
    else:
        touched = 1.0 + (xs <= K)
        untouched = np.where(xs < B, (xs <= K).astype(float), np.nan)
        leg2 = f"高於 {K:,.0f} No"

    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.axvspan(row["gap_lo"], row["gap_hi"], color=GRID, alpha=0.8, lw=0, zorder=0)
    ax.axhline(cost, color=TEXT_2, lw=1, zorder=1)
    ax.text(lo + 150, cost - 0.05, f"成本 \\${cost:.3f}（比 \\$1 少 {(1 - cost) * 100:.1f}¢）",
            color=TEXT_2, fontsize=9, va="top")
    ax.plot(xs, touched, color=BLUE, lw=2, solid_capstyle="round", zorder=3,
            label=f"窗口內曾碰到 {B:,.0f}")
    ax.plot(xs, untouched, color=ORANGE, lw=2, solid_capstyle="round", zorder=3,
            label=f"整個窗口都沒碰到 {B:,.0f}")
    ax.axvline(B, color=TEXT_2, lw=1, alpha=0.5)
    ax.text(B, 2.18, f"觸價 {B:,.0f}", color=TEXT_2, ha="center", fontsize=9)
    ax.annotate(f"缺口：兩腳都付 $0\n機率約 {row['p_gap'] * 100:.1f}%（模型估計）",
                xy=((row["gap_lo"] + row["gap_hi"]) / 2, 0.04), xytext=(max(B, K) + 900, 0.45),
                ha="left", va="center", fontsize=9, color=TEXT,
                arrowprops=dict(arrowstyle="-", color=TEXT_2, lw=1))
    ax.set_ylim(-0.1, 2.35)
    ax.set_yticks([0, 1, 2])
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"${y:.0f}"))
    ax.set_xlim(lo, hi)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.set_xlabel("到期盤結算那根 1 分鐘 K 線的收盤價（Binance BTC/USDT）")
    ax.set_ylabel("組合到期時拿回的錢")
    kind = "觸及 ↓" if down else "觸及 ↑"
    ax.set_title(f"買「{kind}{B:,.0f} Yes」+「{leg2}」：只有灰色缺口會虧", loc="left", fontsize=12)
    ax.legend(loc="center left", bbox_to_anchor=(0, 0.62))
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "near_arb_payoff.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    return p


def plot_reliability(by_pm: pd.DataFrame, by_model: pd.DataFrame, out_dir: Path, title_note: str = "") -> Path:
    """可靠度圖：x 軸是預測的機率（分組平均），y 軸是實際發生的頻率；準的話會落在對角線上。

    by_pm 用 Polymarket 價格分組，by_model 用模型機率分組（history.calibration_table 的輸出）。
    """
    fig, ax = plt.subplots(figsize=(6.4, 5.6))
    ax.plot([0, 1], [0, 1], color=TEXT_2, lw=1, alpha=0.6)
    for d, x, color, label in ((by_model, "model_prob", BLUE, "模型（DVOL 對數常態）"),
                               (by_pm, "pm_price", ORANGE, "Polymarket 價格")):
        ax.errorbar(d[x], d["realized"], yerr=1.96 * d["realized_se"].fillna(0), color=color, lw=2,
                    marker="o", ms=7, mec=SURFACE, mew=2, capsize=0, elinewidth=1, label=label)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_aspect("equal")
    ax.set_xlabel("結算前的預測機率（分組平均）")
    ax.set_ylabel("實際發生的頻率（誤差線：95%，以天為群組）")
    ax.set_title("每日盤的校準：灰色對角線 = 完全準確，點在下方 = 高估", loc="left", fontsize=12, pad=20)
    if title_note:
        ax.text(0, 1.015, title_note, transform=ax.transAxes, color=TEXT_2, fontsize=9, va="bottom")
    ax.legend(loc="upper left", bbox_to_anchor=(0, 0.95))
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "reliability.png"
    fig.savefig(p, dpi=160)
    plt.close(fig)
    return p
