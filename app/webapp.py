"""Web server: the Telegram Mini App and its API, the bank-alert webhook and the Telegram webhook.

Every /api call carries Telegram's signed initData (X-Telegram-Init-Data header), so a user can
only ever read and change their own records."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import available_timezones

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import ai, db, fx, importers, ocr, services as sv
from .config import get_settings
from .models import (
    ACCOUNT_TYPES, LIABILITY_TYPES, VALUED_TYPES, Account, AuditLog, Category, CategoryRule, ReceiptItem,
    ReportSchedule, Transaction, User,
)
from .categories import FALLBACK
from .parser import MULTIPLIERS, parse_amount

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COMMON_CURRENCIES = ["USD", "EUR", "UZS", "RUB", "KZT", "KRW", "TRY", "GBP", "AED", "CNY", "JPY", "INR"]
REPORT_KINDS = ("daily", "weekly", "monthly")
_SUFFIXES = sorted(MULTIPLIERS, key=len, reverse=True)
_tasks: set[asyncio.Task] = set()


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """Verify Telegram WebApp initData (https://core.telegram.org/bots/webapps). Returns the user dict."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("missing hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("bad hash")
    if max_age and time.time() - int(pairs.get("auth_date", 0)) > max_age:
        raise ValueError("expired")
    return json.loads(pairs.get("user", "{}"))


# ---------- input helpers ----------

def _f(x) -> float:
    return round(float(x), 2)


@lru_cache
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def parse_number(raw, positive: bool = True) -> Decimal:
    """'50k', '1.2m', '2 mln', '50 000', '1,250,000.50', 12.5 -> Decimal."""
    s = str(raw if raw is not None else "").strip().lower()
    s = s.replace(" ", "").replace(" ", "").replace("−", "-")
    neg = s.startswith("-")
    s = s.lstrip("+-")
    mult = 1
    for suffix in _SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            mult, s = MULTIPLIERS[suffix], s[:-len(suffix)]
            break
    if re.fullmatch(r"0+([.,]0*)?", s):
        value = Decimal(0)
    else:
        value = parse_amount(s)
        if value is None:
            raise HTTPException(422, "Enter an amount, e.g. 50000 or 50k")
    value = value * mult * (-1 if neg else 1)
    if positive and value <= 0:
        raise HTTPException(422, "Enter an amount above zero")
    if abs(value) >= Decimal("1e13"):
        raise HTTPException(422, "That amount is too large")
    return value.quantize(Decimal("0.0001"))


def parse_day(raw, today: date) -> date:
    try:
        d = date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise HTTPException(422, "Pick a valid date")
    if not date(1990, 1, 1) <= d <= today + timedelta(days=366):
        raise HTTPException(422, "Pick a date between 1990 and next year")
    return d


def parse_currency(raw, default: str | None = None) -> str:
    cur = str(raw or default or "").strip().upper()
    if not (len(cur) == 3 and cur.isalpha() and cur.isascii()):
        raise HTTPException(422, "Currency must be a 3-letter code, e.g. UZS or USD")
    return cur


def month_bounds(month: str, today: date) -> tuple[date, date]:
    try:
        y, m = (int(x) for x in month.split("-")[:2])
        start = date(y, m, 1)
    except (ValueError, TypeError):
        raise HTTPException(422, "Month must look like 2026-11")
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


# ---------- request context ----------

@dataclass
class Ctx:
    s: AsyncSession
    user: User | None
    tg: dict

    def need(self) -> User:
        if self.user is None:
            raise HTTPException(404, "Send /start to the bot first")
        return self.user


async def get_ctx(x_init_data: str = Header("", alias="X-Telegram-Init-Data")):
    settings = get_settings()
    try:
        tg = validate_init_data(x_init_data, settings.bot_token)
    except ValueError as e:
        raise HTTPException(401, f"unauthorized: {e}")
    if not tg.get("id"):
        raise HTTPException(401, "unauthorized: no user")
    if settings.allowed_ids and tg["id"] not in settings.allowed_ids:
        raise HTTPException(403, "This is a private bot.")
    ai.current_user.set(tg["id"])
    async with db.session() as s:
        yield Ctx(s, await sv.get_user(s, tg["id"]), tg)


async def _account(s: AsyncSession, user: User, account_id) -> Account:
    acc = await s.get(Account, int(account_id)) if str(account_id or "").isdigit() else None
    if not acc or acc.user_id != user.id:
        raise HTTPException(404, "Account not found")
    return acc


async def _category(s: AsyncSession, user: User, category_id) -> Category:
    cat = await s.get(Category, int(category_id)) if str(category_id or "").isdigit() else None
    if not cat or cat.user_id != user.id:
        raise HTTPException(404, "Category not found")
    return cat


async def _transaction(s: AsyncSession, user: User, tx_id: int) -> Transaction:
    t = await s.get(Transaction, tx_id)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "Entry not found (maybe already deleted)")
    return t


# ---------- JSON shapes ----------

def tx_json(t: Transaction, peer: Transaction | None = None) -> dict:
    emoji = t.category.emoji if t.category else {"transfer": "🔁", "adjustment": "⚖️", "debt": "🤝"}.get(t.type, "•")
    d = {"id": t.id, "date": str(t.occurred_on), "type": t.type, "amount": _f(t.amount), "currency": t.currency,
         "base_amount": _f(t.base_amount), "account_id": t.account_id, "account": t.account.name,
         "account_currency": t.account.currency, "category_id": t.category_id,
         "category": t.category.name if t.category else "", "emoji": emoji, "note": t.note,
         "merchant": t.merchant, "tags": t.tags or [], "source": t.created_via,
         "created_at": t.created_at.isoformat() if t.created_at else None}
    for x in filter(None, (t, peer)):
        if x.account.type in ("receivable", "debt"):
            d["debt_id"] = x.account_id
    if peer is not None:
        d.update(to_account_id=peer.account_id, to_account=peer.account.name, to_amount=_f(peer.amount),
                 to_currency=peer.currency)
    return d


def acc_json(a: Account, balance: Decimal, value: Decimal, user: User) -> dict:
    return {"id": a.id, "name": a.name, "type": a.type, "currency": a.currency, "balance": _f(balance),
            "value": _f(value), "default": a.id == user.default_account_id, "liability": a.is_liability,
            "valued": a.type in VALUED_TYPES, "in_net_worth": a.include_in_net_worth,
            "due": str(a.due_on) if a.due_on else None, "note": a.note or ""}


def _next_send(row: ReportSchedule, tz: str) -> str | None:
    """When this report goes out next, in the user's own time (ISO, no zone)."""
    if not row.enabled:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    from .scheduler import CRON
    try:
        hh, mm = map(int, row.send_time.split(":"))
        zone = ZoneInfo(tz)
        nxt = CronTrigger(hour=hh, minute=mm, timezone=zone, **CRON[row.kind]).get_next_fire_time(None, datetime.now(zone))
        return nxt.strftime("%Y-%m-%dT%H:%M") if nxt else None
    except Exception:
        return None


def create_app(bot: Bot | None = None, dp: Dispatcher | None = None) -> FastAPI:
    app = FastAPI(title="Money tracker", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        # Full details go to the server log; the app shows a short reference to report.
        log.exception("Error in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": f"Server error ({type(exc).__name__}). Details are in the server log."},
                            status_code=500)

    # The root serves the Mini App too, so it works whichever address is set in BotFather
    # (main Mini App, menu button, home screen shortcut). Outside Telegram the page itself
    # says to open it from the bot.
    @app.get("/")
    @app.get("/app")
    async def mini_app():
        return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        # never touches the database, so pings don't keep it awake
        return {"ok": True, "receipts": "ai" if get_settings().ai_enabled else ocr.describe()}

    # ---------- profile & setup ----------

    @app.get("/api/me")
    async def me(ctx: Ctx = Depends(get_ctx)):
        settings = get_settings()
        base = {"ai": settings.ai_enabled, "scan": settings.ai_enabled or ocr.available(),
                "storage": db.describe(), "account_types": ACCOUNT_TYPES,
                "reader": f"AI ({settings.ai_model})" if settings.ai_enabled else f"Free: {ocr.describe()}",
                "timezones": sorted(_timezones())}
        if ctx.user is None:
            return {"exists": False, "name": ctx.tg.get("first_name", ""), "currencies": COMMON_CURRENCIES,
                    "default_currency": settings.default_currency.upper(), **base}
        s, user = ctx.s, ctx.user
        nw = await sv.net_worth(s, user)
        cats = await sv.categories(s, user)
        sched = {r.kind: r for r in (await s.execute(select(ReportSchedule).where(
            ReportSchedule.user_id == user.id))).scalars()}
        used = sorted({a.currency for a, _, _ in nw.lines} | {user.base_currency})
        return {
            "exists": True, **base,
            "currencies": used + [c for c in COMMON_CURRENCIES if c not in used],
            "user": {"name": user.name, "base_currency": user.base_currency, "timezone": user.timezone,
                     "default_account_id": user.default_account_id, "today": str(sv.local_today(user))},
            "net_worth": _f(nw.net), "assets": _f(nw.assets), "liabilities": _f(nw.liabilities),
            "accounts": [acc_json(a, b, v, user) for a, b, v in nw.lines],
            "debts": {"owed_to_me": _f(sum((v for a, b, v in nw.lines if a.type == "receivable"), Decimal(0))),
                      "i_owe": _f(-sum((v for a, b, v in nw.lines if a.type == "debt"), Decimal(0)))},
            "archived": [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency,
                          "balance": _f(await sv.account_balance(s, a))}
                         for a in await sv.accounts(s, user, include_archived=True) if a.archived],
            "categories": {k: [{"id": c.id, "name": c.name, "emoji": c.emoji} for c in cats if c.kind == k]
                           for k in ("expense", "income")},
            "reports": [{"kind": k, "time": sched[k].send_time, "enabled": sched[k].enabled,
                         "next": _next_send(sched[k], user.timezone)}
                        for k in REPORT_KINDS if k in sched],
        }

    @app.post("/api/setup")
    async def setup(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        if ctx.user:
            return {"ok": True}
        cur = parse_currency(body.get("currency"))
        tz = body.get("timezone") if body.get("timezone") in _timezones() else get_settings().default_timezone
        name = " ".join(x for x in (ctx.tg.get("first_name"), ctx.tg.get("last_name")) if x)[:100]
        user = await sv.create_user(ctx.s, ctx.tg["id"], name, cur, tz)
        await ctx.s.commit()
        from .scheduler import schedule_user
        schedule_user(user)
        return {"ok": True}

    @app.post("/api/settings")
    async def settings_(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        if "timezone" in body:
            if body["timezone"] not in _timezones():
                raise HTTPException(422, "Unknown timezone")
            user.timezone = body["timezone"]
        for r in body.get("reports") or []:
            kind, at = r.get("kind"), str(r.get("time") or "")
            if kind not in REPORT_KINDS or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                raise HTTPException(422, "Report time must look like 21:00")
            row = (await s.execute(select(ReportSchedule).where(
                ReportSchedule.user_id == user.id, ReportSchedule.kind == kind))).scalar_one_or_none()
            if row is None:
                row = ReportSchedule(user_id=user.id, kind=kind)
                s.add(row)
            row.send_time, row.enabled = at, bool(r.get("enabled", True))
        await s.commit()
        from .scheduler import schedule_user
        schedule_user(user)  # takes effect right away, no restart
        rows = (await s.execute(select(ReportSchedule).where(ReportSchedule.user_id == user.id))).scalars()
        return {"ok": True, "reports": [{"kind": r.kind, "time": r.send_time, "enabled": r.enabled,
                                         "next": _next_send(r, user.timezone)} for r in rows]}

    # ---------- dashboard ----------

    @app.get("/api/dashboard")
    async def dashboard(period: str = "month", start: str = "", end: str = "", ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        today = sv.local_today(user)
        if period == "custom":  # any range, e.g. Nov 1 to Nov 10; compared with the same length before it
            a = parse_day(start, today) if start else today.replace(day=1)
            b = parse_day(end, today) if end else today
            if a > b:
                a, b = b, a
            if (b - a).days > 3 * 366:
                raise HTTPException(422, "Pick a range of up to 3 years")
            start, end = a, b
            pend = start - timedelta(days=1)
            pstart = pend - (end - start)
        else:
            if period not in ("day", "week", "month", "year"):
                period = "month"
            start, end, pstart, pend = sv.period_bounds(period, today)
        other = await sv.other_moves(s, user, start, end)
        cur = await sv.stats(s, user, start, end)
        prev = await sv.stats(s, user, pstart, pend)
        all_cats = await sv.categories(s, user)
        cat_ids = {c.name: c for c in all_cats if c.kind == "expense"}
        inc_ids = {c.name: c for c in all_cats if c.kind == "income"}

        months = []
        m_end = today
        for _ in range(12):
            m_start = m_end.replace(day=1)
            st = await sv.stats(s, user, m_start, m_end)
            months.append({"month": m_start.strftime("%Y-%m"), "income": _f(st["income"]),
                           "expense": _f(st["expense"])})
            m_end = m_start - timedelta(days=1)
        months.reverse()

        nw = await sv.net_worth(s, user)
        history = await sv.snapshot_history(s, user, today - timedelta(days=730))
        recent = await sv.history(s, user, limit=6)
        return JSONResponse({
            "currency": user.base_currency, "period": period, "today": str(today),
            "start": str(start), "end": str(end), "prev_start": str(pstart), "prev_end": str(pend),
            "income": _f(cur["income"]), "expense": _f(cur["expense"]), "net": _f(cur["net"]),
            "count": cur["count"], "other": {k: _f(v) for k, v in other.items()},
            "savings_rate": _f(cur["savings_rate"]) if cur["savings_rate"] is not None else None,
            "prev_expense": _f(prev["expense"]), "prev_income": _f(prev["income"]),
            "by_category": [{"name": k, "value": _f(v), "id": cat_ids[k].id if k in cat_ids else None,
                             "emoji": cat_ids[k].emoji if k in cat_ids else ""}
                            for k, v in cur["by_category"].items()],
            "income_by_category": [{"name": k, "value": _f(v), "id": inc_ids[k].id if k in inc_ids else None,
                                    "emoji": inc_ids[k].emoji if k in inc_ids else ""}
                                   for k, v in cur["income_by_category"].items()],
            "daily": [{"date": str(d), "value": _f(v)} for d, v in cur["daily"].items()],
            "months": months,
            "net_worth": _f(nw.net), "assets": _f(nw.assets), "liabilities": _f(nw.liabilities),
            "net_worth_history": [{"date": str(h.day), "value": _f(h.net_worth)} for h in history]
                                 + ([] if history and history[-1].day == today
                                    else [{"date": str(today), "value": _f(nw.net)}]),
            "accounts": [acc_json(a, b, v, user) for a, b, v in nw.lines],
            "recent": [tx_json(t) for t in recent if not (t.type == "transfer" and t.amount > 0)][:5],
        })

    # ---------- transactions ----------

    @app.get("/api/transactions")
    async def list_transactions(month: str = "", start: str = "", end: str = "", type: str = "", q: str = "",
                                account_id: int = 0, category_id: int = 0, limit: int = 200, offset: int = 0,
                                ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        today = sv.local_today(user)
        cond = [Transaction.user_id == user.id]
        if month:
            m_start, m_end = month_bounds(month, today)
            cond.append(Transaction.occurred_on.between(m_start, m_end))
        if start:
            cond.append(Transaction.occurred_on >= parse_day(start, today))
        if end:
            cond.append(Transaction.occurred_on <= parse_day(end, today))
        if type in ("income", "expense", "transfer", "adjustment", "debt"):
            cond.append(Transaction.type == type)
        elif not account_id:  # plain debt records live on the Debts screen
            cond.append(Transaction.type != "debt")
        if account_id:
            cond.append(Transaction.account_id == account_id)
        else:  # a transfer is listed once, as its outgoing side
            cond.append(or_(Transaction.type != "transfer", Transaction.amount < 0))
        if category_id:
            cond.append(Transaction.category_id == category_id)
        if q.strip():
            like = f"%{q.strip()[:60]}%"
            cond.append(or_(Transaction.note.ilike(like), Transaction.merchant.ilike(like), Category.name.ilike(like)))
        limit = max(1, min(limit, 500))
        base_q = select(Transaction).outerjoin(Category, Transaction.category_id == Category.id).where(*cond)
        rows = (await s.execute(base_q.order_by(Transaction.occurred_on.desc(), Transaction.id.desc())
                                .limit(limit + 1).offset(max(offset, 0)))).unique().scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        groups = [t.transfer_group for t in rows if t.transfer_group]
        peers: dict[str, list[Transaction]] = defaultdict(list)
        if groups:
            for p in (await s.execute(select(Transaction).where(Transaction.transfer_group.in_(groups)))).unique().scalars():
                peers[p.transfer_group].append(p)
        totals = dict((await s.execute(
            select(Transaction.type, func.coalesce(func.sum(Transaction.base_amount), 0))
            .select_from(Transaction).outerjoin(Category, Transaction.category_id == Category.id)
            .where(*cond).group_by(Transaction.type))).all())
        items = []
        for t in rows:
            peer = next((p for p in peers.get(t.transfer_group, []) if p.id != t.id), None) if t.transfer_group else None
            items.append(tx_json(t, peer))
        return {"items": items, "has_more": has_more, "currency": user.base_currency,
                "income": _f(totals.get("income", 0)), "expense": _f(-Decimal(str(totals.get("expense", 0))))}

    @app.get("/api/transactions/{tx_id}")
    async def get_transaction(tx_id: int, ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        t = await _transaction(s, user, tx_id)
        peer = None
        if t.transfer_group:
            peer = (await s.execute(select(Transaction).where(Transaction.transfer_group == t.transfer_group,
                                                              Transaction.id != t.id))).unique().scalar_one_or_none()
        ids = [t.id] + ([peer.id] if peer else [])
        log_rows = (await s.execute(select(AuditLog).where(AuditLog.user_id == user.id,
                                                           AuditLog.transaction_id.in_(ids))
                                    .order_by(AuditLog.id))).scalars().all()
        return {**tx_json(t, peer), "history": [{"at": a.at.isoformat() if a.at else None, "action": a.action,
                                                 "detail": a.detail} for a in log_rows]}

    @app.post("/api/transactions")
    async def add_transaction(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        today = sv.local_today(user)
        kind = body.get("type", "expense")
        if kind not in ("expense", "income", "transfer"):
            raise HTTPException(422, "Type must be expense, income or transfer")
        amount = parse_number(body.get("amount"))
        acc = await _account(s, user, body["account_id"]) if body.get("account_id") else await sv.find_account(s, user, None)
        if acc is None:
            raise HTTPException(422, "Pick an account")
        on = parse_day(body["date"], today) if body.get("date") else today
        note = str(body.get("note") or "").strip()[:255]
        try:
            if kind == "transfer":
                dst = await _account(s, user, body.get("to_account_id"))
                if dst.id == acc.id:
                    raise HTTPException(422, "Pick two different accounts")
                to_amount = parse_number(body["to_amount"]) if body.get("to_amount") not in (None, "") else None
                out, inn = await sv.add_transfer(s, user, acc, dst, amount, to_amount, note, on)
                await s.commit()
                return {"ok": True, "transaction": tx_json(out, inn)}
            currency = parse_currency(body.get("currency"), acc.currency)
            if body.get("category_id"):
                cat = await _category(s, user, body["category_id"])
                if cat.kind != kind:
                    raise HTTPException(422, "That category is for a different type")
                if note:
                    await sv.learn_rule(s, user, note, cat, source="user")
            else:
                cat, _ = await sv.guess_category(s, user, note, kind)
            t = await sv.add_transaction(s, user, acc, kind, amount, currency, cat, note=note,
                                         merchant=str(body.get("merchant") or "")[:120], on=on, created_via="app")
        except fx.FxUnavailable:
            raise HTTPException(503, "Couldn't get an exchange rate right now. Try again in a minute.")
        await s.commit()
        return {"ok": True, "transaction": tx_json(t)}

    @app.patch("/api/transactions/{tx_id}")
    async def edit_transaction(tx_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        t = await _transaction(s, user, tx_id)
        today = sv.local_today(user)
        kw = {}
        if body.get("type") in ("income", "expense"):
            kw["kind"] = body["type"]
        if body.get("amount") not in (None, ""):
            kw["amount"] = parse_number(body["amount"], positive=t.type != "adjustment")
        if body.get("currency"):
            kw["currency"] = parse_currency(body["currency"])
        if body.get("account_id"):
            kw["account"] = await _account(s, user, body["account_id"])
        if body.get("category_id"):
            kw["category"] = await _category(s, user, body["category_id"])
        if body.get("date"):
            kw["on"] = parse_day(body["date"], today)
        if "note" in body:
            kw["note"] = str(body.get("note") or "")
        try:
            changed = await sv.update_transaction(s, user, t, **kw)
        except fx.FxUnavailable:
            raise HTTPException(503, "Couldn't get an exchange rate right now. Try again in a minute.")
        await s.commit()
        await s.refresh(t, ["category", "account"])
        return {"ok": True, "changed": changed, "transaction": tx_json(t)}

    @app.delete("/api/transactions/{tx_id}")
    async def delete_transaction(tx_id: int, ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        t = await s.get(Transaction, tx_id)
        touched = []
        if t is not None and t.user_id == user.id:
            legs = [t]
            if t.transfer_group:
                legs = (await s.execute(select(Transaction).where(
                    Transaction.transfer_group == t.transfer_group))).unique().scalars().all()
            touched = list({x.account_id for x in legs})
        if not await sv.delete_transaction(s, user, tx_id):
            raise HTTPException(404, "Entry not found (maybe already deleted)")
        for acc_id in touched:  # a deleted debt payment can reopen the debt
            acc = await s.get(Account, acc_id)
            if acc is not None and acc.type in sv.DEBT_ACCOUNT_TYPES:
                await sv.sync_debt_state(s, user, acc)
        await s.commit()
        return {"ok": True}

    @app.get("/api/guess")
    async def guess(text: str = "", type: str = "expense", ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        if type not in ("expense", "income") or not text.strip():
            return {"category_id": None}
        cat, confident = await sv.guess_category(s, user, text[:200], type)
        return {"category_id": cat.id if confident and cat else None}

    @app.post("/api/scan")
    async def scan(request: Request, ctx: Ctx = Depends(get_ctx)):
        """Read a receipt photo or payment screenshot and return a pre-filled entry (not saved yet)."""
        s, user = ctx.s, ctx.need()
        data = await request.body()
        if not data:
            raise HTTPException(400, "No image")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Image is too large (max 10 MB)")
        ctype = request.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        ctype = ctype if ctype in ("image/jpeg", "image/png", "image/webp") else "image/jpeg"
        cats = await sv.categories(s, user, "expense")
        try:
            if ai.enabled():
                r = await ai.read_receipt(data, ctype, [c.name for c in cats], user.base_currency)
            elif ocr.available():
                from .bot import _read_with_ocr
                r = await _read_with_ocr(data)
            else:
                raise HTTPException(503, "Receipt reading isn't switched on for this bot.")
        except ai.AiError as e:
            raise HTTPException(502, f"Couldn't read the receipt: {e}")
        except HTTPException:
            raise
        except Exception:
            log.exception("scan failed")
            raise HTTPException(422, "Couldn't read this image. Try a sharper, straight photo.")
        if not r.is_receipt or r.total <= 0:
            return {"found": False}
        kind = "income" if r.is_income else "expense"
        by_name = {c.name.lower(): c for c in cats}
        sums: dict[int, Decimal] = defaultdict(Decimal)
        for it in r.items:
            if c := by_name.get((it.category or "").lower()):
                sums[c.id] += Decimal(str(it.amount))
        if kind == "expense" and sums:
            cat_id = max(sums, key=sums.get)
        else:
            text = " ".join([r.merchant] + [i.name for i in r.items[:5]])
            cat, _ = await sv.guess_category(s, user, text, kind)
            cat_id = cat.id if cat else None
        on = importers._to_date(r.date) if r.date else None
        return {"found": True, "type": kind, "amount": float(r.total), "merchant": r.merchant,
                "currency": (r.currency or user.base_currency).upper(), "date": str(on) if on else None,
                "category_id": cat_id, "lines": len(r.items), "categories": len(sums), "ai": ai.enabled()}

    # ---------- accounts ----------

    # ---------- categories ----------
    def _cat_name(body: dict) -> str:
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:60]
        if not name:
            raise HTTPException(422, "Give the category a name")
        return name

    def _emoji(body: dict, default: str = "") -> str:
        return str(body.get("emoji") if body.get("emoji") is not None else default).strip()[:8]

    @app.post("/api/categories")
    async def add_category(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        kind = body.get("kind")
        if kind not in ("expense", "income"):
            raise HTTPException(422, "Kind must be expense or income")
        name = _cat_name(body)
        if await sv.category_by_name(s, user, name, kind):
            raise HTTPException(409, "You already have a category with this name")
        cat = Category(user_id=user.id, name=name, kind=kind, emoji=_emoji(body, "🏷"))
        s.add(cat)
        await s.flush()
        await sv.audit(s, user, "category_added", detail=f"{kind}: {name}")
        await s.commit()
        return {"id": cat.id, "name": cat.name, "emoji": cat.emoji, "kind": cat.kind}

    @app.patch("/api/categories/{category_id}")
    async def edit_category(category_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        cat = await _category(s, user, category_id)
        old = cat.name
        if body.get("name") is not None:
            name = _cat_name(body)
            if name.lower() != old.lower():
                if old == FALLBACK[cat.kind]:
                    raise HTTPException(409, f"\"{old}\" is used when nothing else fits, so it keeps its name")
                if await sv.category_by_name(s, user, name, cat.kind):
                    raise HTTPException(409, "You already have a category with this name")
            cat.name = name
        if body.get("emoji") is not None:
            cat.emoji = _emoji(body)
        await sv.audit(s, user, "category_edited", detail=f"{old} -> {cat.emoji} {cat.name}")
        await s.commit()
        return {"id": cat.id, "name": cat.name, "emoji": cat.emoji, "kind": cat.kind}

    @app.delete("/api/categories/{category_id}")
    async def delete_category(category_id: int, move_to: int = 0, ctx: Ctx = Depends(get_ctx)):
        """Delete a category; its entries move to another category (default: Other), never lost."""
        s, user = ctx.s, ctx.need()
        cat = await _category(s, user, category_id)
        if cat.name == FALLBACK[cat.kind]:
            raise HTTPException(409, f"\"{cat.name}\" is used when nothing else fits, so it can't be deleted")
        target = await _category(s, user, move_to) if move_to else \
            await sv.category_by_name(s, user, FALLBACK[cat.kind], cat.kind)
        if target is None or target.id == cat.id or target.kind != cat.kind:
            raise HTTPException(422, "Pick another category of the same type to move its entries to")
        moved = (await s.execute(update(Transaction).where(
            Transaction.user_id == user.id, Transaction.category_id == cat.id).values(category_id=target.id))).rowcount
        await s.execute(update(ReceiptItem).where(ReceiptItem.category_id == cat.id).values(category_id=target.id))
        await s.execute(update(Category).where(Category.parent_id == cat.id).values(parent_id=None))
        await s.execute(delete(CategoryRule).where(CategoryRule.category_id == cat.id))
        await sv.audit(s, user, "category_deleted", detail=f"{cat.name}: {moved} entries moved to {target.name}")
        await s.delete(cat)
        await s.commit()
        return {"ok": True, "moved": moved, "to": target.name}

    # ---------- personal debts (kept apart from net worth) ----------
    async def _debt(s, user, account_id) -> Account:
        acc = await _account(s, user, account_id)
        if acc.type not in sv.DEBT_ACCOUNT_TYPES:
            raise HTTPException(422, "This account isn't a personal debt")
        return acc

    async def _money(s, user, body) -> Account | None:
        """The account the money really moved from/into, or None for a plain record."""
        if not body.get("account_id"):
            return None
        money = await _account(s, user, body["account_id"])
        if money.type in sv.DEBT_ACCOUNT_TYPES or money.archived:
            raise HTTPException(422, "Pick the account the money went from or into")
        return money

    @app.post("/api/debts")
    async def add_debt(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        """Lent money to someone (direction=lent) or borrowed from someone (direction=borrowed)."""
        s, user = ctx.s, ctx.need()
        direction = body.get("direction")
        if direction not in sv.DEBT_TYPES:
            raise HTTPException(422, "Pick: you lent or you borrowed")
        person = re.sub(r"\s+", " ", str(body.get("person") or "")).strip()
        if not person:
            raise HTTPException(422, "Who is it? Enter a name")
        amount = parse_number(body.get("amount"))
        money = await _money(s, user, body)
        today = sv.local_today(user)
        on = parse_day(body["date"], today) if body.get("date") else today
        due = parse_day(body["due"], today) if body.get("due") else None
        acc = await sv.open_debt(s, user, direction, person, amount, money, on=on, due=due,
                                 note=str(body.get("note") or "").strip(),
                                 currency=parse_currency(body.get("currency"), user.base_currency))
        await s.commit()
        return {"ok": True, "id": acc.id, "name": acc.name}

    @app.get("/api/debts/{account_id}")
    async def get_debt(account_id: int, ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        out = []
        for t in await sv.debt_entries(s, debt):
            peer = None
            if t.transfer_group:
                peer = (await s.execute(select(Transaction).where(
                    Transaction.transfer_group == t.transfer_group, Transaction.id != t.id))).unique().scalar_one_or_none()
            # positive = more owed to you (receivable) / more you owe (debt)
            grows = t.account_amount > 0 if debt.type == "receivable" else t.account_amount < 0
            out.append({"id": t.id, "date": str(t.occurred_on), "amount": _f(abs(t.account_amount)),
                        "kind": "add" if grows else "return", "note": t.note,
                        "account": peer.account.name if peer else None})
        return {"id": debt.id, "name": debt.name, "type": debt.type, "currency": debt.currency,
                "left": _f(abs(await sv.account_balance(s, debt))), "due": str(debt.due_on) if debt.due_on else None,
                "note": debt.note or "", "archived": debt.archived, "entries": out[::-1]}

    @app.patch("/api/debts/{account_id}")
    async def edit_debt(account_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        changes = []
        if body.get("name") is not None:
            name = re.sub(r"\s+", " ", str(body["name"])).strip()[:50]
            if not name:
                raise HTTPException(422, "Enter a name")
            other = await sv.find_account(s, user, name)
            if other and other.id != debt.id:
                raise HTTPException(409, "You already have an account or debt with this name")
            if name != debt.name:
                changes.append(f"name {debt.name} -> {name}")
                debt.name = name
        if "due" in body:
            debt.due_on = parse_day(body["due"], sv.local_today(user)) if body["due"] else None
            changes.append(f"due {debt.due_on or 'none'}")
        if "note" in body:
            debt.note = str(body["note"] or "").strip()[:200]
            changes.append("note")
        if changes:
            await sv.audit(s, user, "debt_edited", detail=f"{debt.name}: " + ", ".join(changes))
        await s.commit()
        return {"ok": True}

    @app.delete("/api/debts/{account_id}")
    async def delete_debt(account_id: int, ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        n = await sv.delete_debt(s, user, debt)
        await s.commit()
        return {"ok": True, "deleted_entries": n}

    @app.post("/api/debts/{account_id}/repay")
    async def repay_debt(account_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        money = await _money(s, user, body)
        amount = parse_number(body.get("amount"))
        today = sv.local_today(user)
        on = parse_day(body["date"], today) if body.get("date") else today
        try:
            left = await sv.repay_debt(s, user, debt, amount, money, on=on)
        except ValueError as e:
            raise HTTPException(422, str(e))
        await s.commit()
        return {"ok": True, "left": _f(left), "closed": left == 0}

    @app.patch("/api/debts/{account_id}/entries/{tx_id}")
    async def edit_debt_entry(account_id: int, tx_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        t = await _transaction(s, user, tx_id)
        if t.account_id != debt.id:
            raise HTTPException(404, "Entry not found")
        today = sv.local_today(user)
        amount = parse_number(body["amount"]) if body.get("amount") not in (None, "") else None
        on = parse_day(body["date"], today) if body.get("date") else None
        await sv.edit_debt_entry(s, user, debt, t, amount=amount, on=on)
        await s.commit()
        return {"ok": True, "left": _f(abs(await sv.account_balance(s, debt)))}

    @app.delete("/api/debts/{account_id}/entries/{tx_id}")
    async def delete_debt_entry(account_id: int, tx_id: int, ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        debt = await _debt(s, user, account_id)
        t = await _transaction(s, user, tx_id)
        if t.account_id != debt.id:
            raise HTTPException(404, "Entry not found")
        await sv.delete_transaction(s, user, t.id)
        await sv.sync_debt_state(s, user, debt)
        await s.commit()
        return {"ok": True, "left": _f(abs(await sv.account_balance(s, debt)))}

    @app.post("/api/accounts")
    async def add_account(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        name = str(body.get("name") or "").strip()[:60]
        type_ = str(body.get("type") or "")
        if not name:
            raise HTTPException(422, "Give the account a name")
        if type_ not in ACCOUNT_TYPES:
            raise HTTPException(422, "Pick an account type")
        if await sv.find_account(s, user, name):
            raise HTTPException(409, "An account with this name already exists")
        currency = parse_currency(body.get("currency"), user.base_currency)
        opening = parse_number(body["opening"], positive=False) if body.get("opening") not in (None, "") else Decimal(0)
        if type_ in LIABILITY_TYPES:
            opening = -abs(opening)  # what you owe is stored as a negative balance
        acc = await sv.add_account(s, user, name, type_, currency, opening)
        await s.commit()
        return {"ok": True, "id": acc.id}

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: int, ctx: Ctx = Depends(get_ctx)):
        """Only archived accounts can be deleted for good (archive first, so it is never a slip)."""
        s, user = ctx.s, ctx.need()
        acc = await _account(s, user, account_id)
        if not acc.archived:
            raise HTTPException(409, "Archive the account first, then delete it from the archived list")
        n = await sv.delete_account(s, user, acc)
        await s.commit()
        return {"ok": True, "deleted_entries": n}

    @app.patch("/api/accounts/{account_id}")
    async def edit_account(account_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        acc = await _account(s, user, account_id)
        if body.get("name"):
            name = str(body["name"]).strip()[:60]
            other = await sv.find_account(s, user, name)
            if other and other.id != acc.id:
                raise HTTPException(409, "An account with this name already exists")
            acc.name = name
        if body.get("default"):
            if acc.archived:
                raise HTTPException(422, "Archived accounts can't be the default")
            user.default_account_id = acc.id
        if "in_net_worth" in body:
            acc.include_in_net_worth = bool(body["in_net_worth"])
        if "due" in body:
            acc.due_on = parse_day(body["due"], sv.local_today(user)) if body["due"] else None
        if "note" in body:
            acc.note = str(body["note"] or "").strip()[:200]
        if body.get("archived") is False and acc.archived:
            acc.archived = False
            await sv.audit(s, user, "account_restored", detail=acc.name)
        if body.get("archived") and not acc.archived:
            others = [a for a in await sv.accounts(s, user) if a.id != acc.id]
            if not others:
                raise HTTPException(409, "Keep at least one account")
            acc.archived = True
            if user.default_account_id == acc.id:
                user.default_account_id = others[0].id
        await s.commit()
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/balance")
    async def set_balance(account_id: int, body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        """'The real balance is X': records a correction (or a new value for investments/property)."""
        s, user = ctx.s, ctx.need()
        acc = await _account(s, user, account_id)
        actual = parse_number(body.get("balance"), positive=False)
        try:
            t = await sv.reconcile(s, user, acc, actual)
        except fx.FxUnavailable:
            raise HTTPException(503, "Couldn't get an exchange rate right now. Try again in a minute.")
        await s.commit()
        return {"ok": True, "adjustment": tx_json(t) if t else None,
                "balance": _f(await sv.account_balance(s, acc))}

    # ---------- statements, files ----------

    @app.get("/api/statement")
    async def statement(month: str = "", year: str = "", ctx: Ctx = Depends(get_ctx)):
        s, user = ctx.s, ctx.need()
        today = sv.local_today(user)
        if year:
            if not year.isdigit():
                raise HTTPException(422, "Year must look like 2026")
            start, end = date(int(year), 1, 1), date(int(year), 12, 31)
        else:
            start, end = month_bounds(month or today.strftime("%Y-%m"), today)
        if start > today:
            raise HTTPException(422, "That period hasn't started yet")
        end = min(end, today)
        st = await sv.statement(s, user, start, end)
        await s.commit()  # the statement may have saved today's record
        snap = lambda x: {"day": str(x.day), "net_worth": _f(x.net_worth)} if x else None  # noqa: E731
        return {"start": str(start), "end": str(end), "currency": st.currency,
                "opening": snap(st.opening), "closing": snap(st.closing),
                "income": _f(st.income), "expense": _f(st.expense), "saved": _f(st.saved),
                "change": _f(st.change) if st.opening else None, "other": _f(st.other) if st.opening else None,
                "lines": [{"account": n, "currency": c, "opening": _f(o), "closing": _f(cl)}
                          for n, c, o, cl in st.lines]}

    @app.post("/api/send")
    async def send_file(body: dict = Body(...), ctx: Ctx = Depends(get_ctx)):
        """Send a backup, statement or CSV export into the user's chat with the bot."""
        s, user = ctx.s, ctx.need()
        if bot is None:
            raise HTTPException(503, "The bot isn't connected to this server")
        from .bot import send_backup, statement_text
        what = body.get("what")
        today = sv.local_today(user)
        try:
            if what == "backup":
                await send_backup(bot, s, user)
            elif what == "export":
                await bot.send_document(user.telegram_id, BufferedInputFile(
                    await sv.export_csv(s, user), f"transactions_{today}.csv"),
                    caption="All transactions (opens in Excel / Google Sheets).")
            elif what == "statement":
                start, end = month_bounds(body.get("month") or today.strftime("%Y-%m"), today)
                end = min(end, today)
                st = await sv.statement(s, user, start, end)
                await bot.send_message(user.telegram_id, statement_text(st))
                await bot.send_document(user.telegram_id, BufferedInputFile(
                    sv.statement_csv(st), f"statement_{start}_{end}.csv"))
            else:
                raise HTTPException(422, "Unknown file")
        except TelegramForbiddenError:
            raise HTTPException(409, "Open the chat with the bot and press Start, then try again.")
        await s.commit()
        return {"ok": True}

    # ---------- bank alerts from the phone ----------

    @app.post("/ingest/{token}")
    async def ingest(token: str, request: Request):
        """Bank alert webhook for phone automations: body is the alert text (plain, JSON or form)."""
        raw = (await request.body()).decode(errors="ignore")
        ctype = request.headers.get("content-type", "")
        text = raw
        if "json" in ctype:
            try:
                body = json.loads(raw)
                text = body.get("text") or body.get("message") or body.get("body") or raw
            except (ValueError, AttributeError):
                pass
        elif "form" in ctype:
            form = dict(parse_qsl(raw))
            text = form.get("text") or form.get("message") or form.get("body") or raw
        text = (text or "").strip()[:2000]
        if not text:
            raise HTTPException(400, "empty")
        async with db.session() as s:
            user = (await s.execute(select(User).where(User.ingest_token == token))).scalar_one_or_none()
            if not user:
                raise HTTPException(404, "unknown token")
            if not ai.enabled():
                raise HTTPException(503, "AI not configured")
            ai.current_user.set(user.telegram_id)
            cats = [c.name for c in await sv.categories(s, user)]
            try:
                tx = await ai.parse_bank_message(text, cats, user.base_currency)
            except ai.AiError as e:
                raise HTTPException(502, str(e))
            if not tx.is_transaction or tx.amount <= 0:
                return {"status": "ignored"}
            t, status = await importers.save_bank_tx(s, user, tx, "alert", raw=text)
            await s.commit()
            if bot and status == "added":
                from .bot import tx_kb, tx_line
                await s.refresh(t, ["category", "account"])
                await bot.send_message(user.telegram_id, f"🏦 From your bank:\n{tx_line(t)}", reply_markup=tx_kb(t))
            return {"status": status, "id": t.id if t else None}

    # ---------- Telegram webhook (used on hosts that sleep, like Render) ----------

    if dp is not None and bot is not None:
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request,
                                   secret: str = Header("", alias="X-Telegram-Bot-Api-Secret-Token")):
            if not hmac.compare_digest(secret.encode(), get_settings().webhook_token.encode()):
                raise HTTPException(401, "bad secret")
            try:
                update = await request.json()
            except ValueError:
                raise HTTPException(400, "bad json")
            # Answer Telegram at once; receipts and AI can take longer than its timeout.
            task = asyncio.create_task(_process_update(dp, bot, update))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
            return {"ok": True}

    return app


async def _process_update(dp: Dispatcher, bot: Bot, update: dict):
    try:
        await dp.feed_raw_update(bot, update)
    except Exception:
        log.exception("Failed to handle update %s", update.get("update_id"))
