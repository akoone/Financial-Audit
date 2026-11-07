"""Chart images for report messages (the interactive version lives in the Mini App)."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PALETTE = ["#2a78d6", "#e8743b", "#19a979", "#ed4a7b", "#945ecf", "#13a4b4", "#d6a419",
           "#6c8893", "#ee6868", "#525df4", "#bf399e", "#888888"]


def report_chart(st: dict, title: str) -> bytes | None:
    """Donut of spending by category + daily spending bars. None if there's nothing to draw."""
    cats = st["by_category"]
    if not cats:
        return None
    items = list(cats.items())
    if len(items) > 7:
        items = items[:7] + [("Other", sum(v for _, v in items[7:]))]
    labels = [k for k, _ in items]
    values = [float(v) for _, v in items]
    show_daily = st["days"] > 1

    fig, axes = plt.subplots(1, 2 if show_daily else 1, figsize=(10 if show_daily else 6, 4.6), dpi=130)
    ax1 = axes[0] if show_daily else axes
    wedges, _ = ax1.pie(values, colors=PALETTE[:len(values)], startangle=90, counterclock=False,
                        wedgeprops={"width": 0.38, "edgecolor": "white"})
    total = sum(values)
    ax1.text(0, 0, f"{total:,.0f}\n{st['currency']}", ha="center", va="center", fontsize=13, weight="bold")
    ax1.legend(wedges, [f"{l}  {v / total * 100:.0f}%" for l, v in zip(labels, values)],
               loc="center left", bbox_to_anchor=(0.95, 0.5), frameon=False, fontsize=9)
    ax1.set_title("Spending by category", fontsize=11)

    if show_daily:
        ax2 = axes[1]
        days = list(st["daily"].keys())
        vals = [float(v) for v in st["daily"].values()]
        ax2.bar(range(len(days)), vals, color="#2a78d6", width=0.7)
        step = max(1, len(days) // 8)
        ax2.set_xticks(range(0, len(days), step))
        ax2.set_xticklabels([d.strftime("%d %b") for d in days][::step], fontsize=8, rotation=0)
        ax2.spines[["top", "right"]].set_visible(False)
        ax2.set_title(f"Daily spending ({st['currency']})", fontsize=11)
        avg = sum(vals) / len(vals)
        ax2.axhline(avg, color="#e8743b", lw=1, ls="--")
        ax2.text(-0.4, avg, f"avg {avg:,.0f}", color="#e8743b", fontsize=8, va="bottom", ha="left",
                 bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})

    fig.suptitle(title, fontsize=12, weight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
