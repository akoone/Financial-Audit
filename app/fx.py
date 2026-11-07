"""Daily exchange rates, cached in the fx_rates table.

Source: open.er-api.com (free, no key, ~160 currencies, updated daily).
If the API is unreachable, the latest cached rate is used.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FxRate

log = logging.getLogger(__name__)
API = "https://open.er-api.com/v6/latest/{base}"


class FxUnavailable(Exception):
    pass


async def _fetch(base: str) -> dict[str, Decimal]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(API.format(base=base))
        r.raise_for_status()
        data = r.json()
    if data.get("result") != "success":
        raise FxUnavailable(data.get("error-type", "unknown error"))
    return {k: Decimal(str(v)) for k, v in data["rates"].items()}


async def get_rate(s: AsyncSession, base: str, quote: str, day: date | None = None) -> Decimal:
    """How many `quote` units one `base` unit buys (on `day`, or the closest earlier cached day)."""
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return Decimal(1)
    day = day or date.today()

    row = (await s.execute(select(FxRate).where(
        FxRate.base == base, FxRate.quote == quote, FxRate.day == day))).scalar_one_or_none()
    if row:
        return row.rate

    if day >= date.today():
        try:
            rates = await _fetch(base)
            for q, r in rates.items():
                if q == quote:
                    s.add(FxRate(day=day, base=base, quote=q, rate=r))
            await s.flush()
            if quote in rates:
                return rates[quote]
        except Exception as e:  # network, API error
            log.warning("FX fetch %s failed: %s", base, e)

    # Historical day or fetch failed: closest cached rate (direct, then inverse)
    row = (await s.execute(select(FxRate).where(FxRate.base == base, FxRate.quote == quote)
                           .order_by(FxRate.day.desc()).limit(1))).scalar_one_or_none()
    if row:
        return row.rate
    inv = (await s.execute(select(FxRate).where(FxRate.base == quote, FxRate.quote == base)
                           .order_by(FxRate.day.desc()).limit(1))).scalar_one_or_none()
    if inv and inv.rate:
        return Decimal(1) / inv.rate
    if day < date.today():
        return await get_rate(s, base, quote, date.today())
    raise FxUnavailable(f"No rate for {base}->{quote}")


async def convert(s: AsyncSession, amount: Decimal, frm: str, to: str, day: date | None = None) -> Decimal:
    return (amount * await get_rate(s, frm, to, day)).quantize(Decimal("0.0001"))
