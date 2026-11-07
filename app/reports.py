"""Builds daily / weekly / monthly report messages: text, chart image and AI advice."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from . import ai, services as sv
from .charts import report_chart
from .models import AiInsight, User

log = logging.getLogger(__name__)
LABELS = {"day": "Daily", "week": "Weekly", "month": "Monthly", "year": "Yearly"}


@dataclass
class Report:
    text: str
    chart: bytes | None


def _pct_change(cur, prev) -> str:
    if not prev:
        return ""
    ch = (cur - prev) / prev * 100
    arrow = "▲" if ch > 0 else "▼"
    return f" ({arrow}{abs(ch):.0f}% vs previous)"


async def build_report(s: AsyncSession, user: User, kind: str, today: date | None = None,
                       with_ai: bool = True) -> Report:
    today = today or sv.local_today(user)
    kind = {"daily": "day", "weekly": "week", "monthly": "month"}.get(kind, kind)
    if kind == "month" and today.day == 1:
        # The monthly report sent on the 1st covers the whole previous month
        today = today - timedelta(days=1)
        start, _, pstart, pend = sv.period_bounds("month", today)
        end = today
    else:
        start, end, pstart, pend = sv.period_bounds(kind, today)
    cur = await sv.stats(s, user, start, end)
    prev = await sv.stats(s, user, pstart, pend)
    c = user.base_currency
    label = LABELS[kind]
    period = start.strftime("%d %b") if start == end else f"{start:%d %b} – {end:%d %b %Y}"

    lines = [f"<b>{label} report</b> · {period}", ""]
    lines.append(f"💸 Spent: <b>{sv.fmt(cur['expense'], c)}</b>{_pct_change(cur['expense'], prev['expense'])}")
    if cur["income"] or kind != "day":
        lines.append(f"💰 Income: <b>{sv.fmt(cur['income'], c)}</b>")
        lines.append(f"📊 Net: <b>{sv.fmt(cur['net'], c)}</b>"
                     + (f" · savings rate {cur['savings_rate']:.0f}%" if cur["savings_rate"] is not None else ""))
    if cur["by_category"]:
        lines += ["", "<b>Top categories</b>"]
        for name, amt in list(cur["by_category"].items())[:5]:
            share = amt / cur["expense"] * 100 if cur["expense"] else 0
            lines.append(f"• {html.escape(name)}: {sv.fmt(amt, c)} ({share:.0f}%)")
    if kind != "day" and cur["top"]:
        lines += ["", "<b>Biggest purchases</b>"]
        for amt, what, d in cur["top"][:3]:
            lines.append(f"• {html.escape(what or '—')}: {sv.fmt(amt, c)} ({d:%d %b})")
    if kind == "month":
        nw = await sv.net_worth(s, user)
        lines += ["", f"🏦 Net worth: <b>{sv.fmt(nw.net, c)}</b>"]
    if cur["count"] == 0:
        lines += ["", "No transactions logged in this period. Send something like <code>12 coffee</code>."]

    if with_ai and ai.enabled() and cur["count"] > 0:
        try:
            extra = {}
            if kind == "month":
                nw = await sv.net_worth(s, user)
                extra = {"net_worth": float(nw.net), "assets": float(nw.assets),
                         "liabilities": float(nw.liabilities)}
            ins = await ai.insights(sv.stats_for_ai(cur, prev, extra), f"{label} ({period})")
            lines += ["", "🤖 <b>AI summary</b>", html.escape(ins.summary)]
            if ins.observations:
                lines += ["", "<b>Noticed</b>"] + [f"• {html.escape(o)}" for o in ins.observations[:4]]
            if ins.suggestions:
                lines += ["", "<b>Suggestions</b>"] + [f"✅ {html.escape(x)}" for x in ins.suggestions[:3]]
            s.add(AiInsight(user_id=user.id, period_start=start, period_end=end, summary=ins.summary,
                            details=ins.model_dump()))
        except ai.AiError as e:
            log.warning("AI insight failed: %s", e)
            lines += ["", f"<i>AI summary unavailable: {html.escape(str(e))}</i>"]

    chart = report_chart(cur, f"{label} · {period}")
    return Report("\n".join(lines), chart)
