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
    "font.sans-serif": ["Noto Sans CJK TC", "Noto Sans CJK JP", "PingFang TC", "Heiti TC",
                        "Microsoft JhengHei", "Arial Unicode MS", "DejaVu Sans"],
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
    ax.bar(buckets, summary["平均 edge"], width=0.5, color=BLUE)
    ax.axhline(0, color=TEXT_2, lw=1)
    ax.set_xlabel("期權推算的公允機率（分桶）")
    ax.set_ylabel("平均可捕捉 edge（美元／股）")
    ax.set_title(f"{len(df):,} 筆觀測：哪個機率區間的錯價最大？", loc="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_dir / "edge_by_bucket.png", dpi=160)
    plt.close(fig)
    return summary
