"""Chart images for report messages (the interactive version lives in the Mini App)."""
from __future__ import annotations

import io
from datetime import timedelta
from itertools import accumulate

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

PALETTE = ["#2a78d6", "#e8743b", "#19a979", "#ed4a7b", "#945ecf", "#13a4b4", "#d6a419",
           "#6c8893", "#ee6868", "#525df4", "#bf399e", "#888888"]
SPEND, INCOME, PREV = "#2a78d6", "#19a979", "#9a9a9a"


def _short(v: float) -> str:
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{v / div:.1f}".rstrip("0").rstrip(".") + suf
    return f"{v:,.0f}"


def _clean(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _short(v)))
    ax.grid(axis="y", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)


def _line(ax, mtd: dict, prev_mtd: dict | None, prev_label: str):
    """Running totals from the 1st of the month to the report day."""
    days = list(mtd["daily"].keys())
    x = [d.day for d in days]
    spent = list(accumulate(float(v) for v in mtd["daily"].values()))
    earned = list(accumulate(float(v) for v in mtd["daily_income"].values()))
    ax.plot(x, spent, color=SPEND, lw=2.4, marker="o", ms=3.5, label="Spent")
    ax.fill_between(x, spent, color=SPEND, alpha=0.08)
    if any(earned):
        ax.plot(x, earned, color=INCOME, lw=2, marker="o", ms=3, label="Income")
    if prev_mtd:
        px = [d.day for d in prev_mtd["daily"]]
        pspent = list(accumulate(float(v) for v in prev_mtd["daily"].values()))
        ax.plot(px, pspent, color=PREV, lw=1.6, ls="--", label=f"Spent in {prev_label}")
    for series, color in ((spent, SPEND), (earned, INCOME)):
        if series and series[-1]:
            ax.annotate(_short(series[-1]), (x[-1], series[-1]), xytext=(6, 0), textcoords="offset points",
                        color=color, fontsize=9, weight="bold", va="center")
    ax.set_xlim(0.5, max(x[-1] + 1.5, 7))
    step = 1 if len(x) <= 10 else 2 if len(x) <= 20 else 3
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%d %b") if i == 0 else f"{d.day}" for i, d in enumerate(days)][::step], fontsize=8)
    _clean(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    whole = (days[-1] + timedelta(days=1)).day == 1
    ax.set_title(f"{days[0]:%B}{'' if whole else ' so far'}, running total ({mtd['currency']})", fontsize=11, loc="left")


def _pie(ax, st: dict, what: str):
    items = list(st["by_category"].items())
    if not items:
        ax.axis("off")
        ax.text(0.5, 0.5, f"No spending {what}", ha="center", va="center", fontsize=11, color="#777777")
        return
    if len(items) > 7:
        items = items[:7] + [("All the rest", sum(v for _, v in items[7:]))]
    labels = [k for k, _ in items]
    values = [float(v) for _, v in items]
    total = sum(values)
    wedges, _texts, autotexts = ax.pie(
        values, colors=PALETTE[:len(values)], startangle=90, counterclock=False,
        autopct=lambda p: f"{p:.0f}%" if p >= 6 else "", pctdistance=0.68,
        wedgeprops={"edgecolor": "white", "linewidth": 1.5})
    for t in autotexts:
        t.set_color("white"); t.set_fontsize(9); t.set_weight("bold")
    ax.legend(wedges, [f"{l}  {_short(v)}" for l, v in zip(labels, values)],
              loc="center left", bbox_to_anchor=(0.98, 0.5), frameon=False, fontsize=9)
    ax.set_title(f"Spending by category {what}: {total:,.0f} {st['currency']}", fontsize=11, loc="left")


def _bars_totals(ax, cur: dict, prev: dict, cur_label: str, prev_label: str):
    groups = ["Income", "Spent", "Saved"]
    a = [float(cur["income"]), float(cur["expense"]), float(cur["net"])]
    b = [float(prev["income"]), float(prev["expense"]), float(prev["net"])]
    xs = range(len(groups)); w = 0.38
    bp = ax.bar([i - w / 2 for i in xs], b, w, color="#c9c9c9", label=prev_label)
    bc = ax.bar([i + w / 2 for i in xs], a, w, color=[INCOME, SPEND, "#945ecf"], label=cur_label)
    for bars in (bp, bc):
        for r in bars:
            h = r.get_height()
            ax.annotate(_short(h), (r.get_x() + r.get_width() / 2, h), xytext=(0, 2 if h >= 0 else -10),
                        textcoords="offset points", ha="center", fontsize=8)
    ax.axhline(0, color="#999999", lw=0.8)
    ax.set_xticks(list(xs)); ax.set_xticklabels(groups, fontsize=9)
    _clean(ax)
    ax.legend(handles=[bp, bc[1]], labels=[prev_label, cur_label], frameon=False, fontsize=9)
    ax.set_title(f"{cur_label} vs {prev_label}", fontsize=11, loc="left")


def _bars_categories(ax, cur: dict, prev: dict, cur_label: str, prev_label: str):
    names = list(dict.fromkeys(list(cur["by_category"])[:7] + list(prev["by_category"])[:3]))[:8]
    if not names:
        ax.axis("off")
        return
    a = [float(cur["by_category"].get(n, 0)) for n in names]
    b = [float(prev["by_category"].get(n, 0)) for n in names]
    ys = range(len(names)); h = 0.38
    ax.barh([i + h / 2 for i in ys], b, h, color="#c9c9c9", label=prev_label)
    ax.barh([i - h / 2 for i in ys], a, h, color=SPEND, label=cur_label)
    for i, (va, vb) in enumerate(zip(a, b)):
        ax.text(va, i - h / 2, f" {_short(va)}", va="center", fontsize=8, color=SPEND)
        ax.text(vb, i + h / 2, f" {_short(vb)}", va="center", fontsize=8, color="#777777")
    ax.set_yticks(list(ys)); ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.spines[["top", "right"]].set_visible(False)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _short(v)))
    ax.grid(axis="x", color="#e6e6e6", lw=0.8); ax.set_axisbelow(True)
    ax.set_xlim(0, max(a + b) * 1.18 or 1)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    ax.set_title(f"Spending by category: {cur_label} vs {prev_label}", fontsize=11, loc="left")


def work_chart(w: dict, title: str) -> bytes | None:
    """Hours worked per day (bars, paid vs waiting not split) + share of hours by person (pie)."""
    days = list(w["daily"].keys())
    by_pay = any(w.get("daily_pay", {}).values())  # paid by the day: show money, otherwise hours
    hours = [float(w["daily_pay"][d]) for d in days] if by_pay else [m / 60 for m in w["daily"].values()]
    people = [c for c in w["by_client"] if c["minutes"] or c["days"] or c.get("earned")]
    fig = plt.figure(figsize=(10, 7.6), dpi=130)
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.42, top=0.9, bottom=0.04)
    ax = fig.add_subplot(gs[0])
    ax.bar(range(len(days)), hours, color=SPEND, width=0.7)
    for i, h in enumerate(hours):
        if h:
            ax.text(i, h, _short(h) if by_pay else f"{h:.1f}".rstrip("0").rstrip("."), ha="center", va="bottom", fontsize=8)
    step = 1 if len(days) <= 10 else 2 if len(days) <= 20 else 3
    ax.set_xticks(range(0, len(days), step))
    ax.set_xticklabels([d.strftime("%a %d") if len(days) <= 7 else str(d.day) for d in days][::step], fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e6e6e6", lw=0.8); ax.set_axisbelow(True)
    h, m = divmod(w["minutes"], 60)
    if by_pay:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _short(v)))
        ax.set_title(f"Earned per day: {float(w['earned']):,.0f} in {w['days']} day{'s' if w['days'] != 1 else ''}", fontsize=11, loc="left")
    else:
        ax.set_title(f"Hours worked per day: {h}h {m:02d}m in {w['days']} days", fontsize=11, loc="left")
    ax2 = fig.add_subplot(gs[1])
    vals = [float(c.get("earned") or 0) for c in people] if by_pay else [c["minutes"] / 60 or c["days"] for c in people]
    if len(people) > 1 or (people and people[0]["client"]):
        wedges, _t, auto = ax2.pie(vals, colors=PALETTE[:len(vals)], startangle=90, counterclock=False,
                                   autopct=lambda p: f"{p:.0f}%" if p >= 6 else "", pctdistance=0.68,
                                   wedgeprops={"edgecolor": "white", "linewidth": 1.5})
        for t in auto:
            t.set_color("white"); t.set_fontsize(9); t.set_weight("bold")
        ax2.legend(wedges, [f"{c['client'] or 'No person'}  {c['days']}d · " + (_short(float(c['earned'])) if by_pay else f"{c['minutes'] // 60}h")
                            for c in people],
                   loc="center left", bbox_to_anchor=(0.98, 0.5), frameon=False, fontsize=9)
        ax2.set_title("Earned by person" if by_pay else "Hours by person", fontsize=11, loc="left")
    else:
        ax2.axis("off")
        ax2.text(0.5, 0.5, f"⏳ {w['waiting_days']} days waiting for pay   ·   💰 {w['paid_days']} days paid",
                 ha="center", va="center", fontsize=12)
    fig.suptitle(title, fontsize=13, weight="bold", y=0.985)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def report_chart(st: dict, title: str, mtd: dict | None = None, prev_mtd: dict | None = None,
                 compare: tuple[dict, dict, str, str] | None = None, what: str = "") -> bytes | None:
    """Line chart (1st of the month to the report day) + pie of the period's spending;
    for the monthly report also bar charts against the previous month. None if there's nothing to draw."""
    mtd = mtd or st
    has_month = mtd["expense"] or mtd["income"]
    if not (st["by_category"] or has_month):
        return None
    if not st["by_category"] and mtd is not st and mtd["by_category"]:
        st, what = mtd, f"in {next(iter(mtd['daily'])):%B} so far"  # nothing spent in the period: show the month instead
    prev_label = compare[3] if compare else (f"{next(iter(prev_mtd['daily'])):%B}" if prev_mtd else "")
    if compare:
        fig = plt.figure(figsize=(10, 13.5), dpi=130)
        gs = fig.add_gridspec(3, 2, height_ratios=[1.05, 1, 1.05], hspace=0.42, wspace=0.75, top=0.95, bottom=0.04)
        _line(fig.add_subplot(gs[0, :]), mtd, prev_mtd, prev_label)
        _pie(fig.add_subplot(gs[1, 0]), st, what)
        cur, prev, cl, pl = compare
        _bars_totals(fig.add_subplot(gs[1, 1]), cur, prev, cl, pl)
        _bars_categories(fig.add_subplot(gs[2, :]), cur, prev, cl, pl)
    else:
        fig = plt.figure(figsize=(10, 8.6), dpi=130)
        gs = fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.38, top=0.92, bottom=0.04)
        _line(fig.add_subplot(gs[0]), mtd, prev_mtd, prev_label)
        _pie(fig.add_subplot(gs[1]), st, what)
    fig.suptitle(title, fontsize=13, weight="bold", y=0.99 if compare else 0.985)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
