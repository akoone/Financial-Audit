"""Web server: Telegram Mini App dashboard + bank-alert webhook."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from . import ai, db, importers, services as sv
from .config import get_settings
from .models import Transaction, User

STATIC = Path(__file__).parent / "static"


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


def _f(x) -> float:
    return round(float(x), 2)


def create_app(bot: Bot | None = None) -> FastAPI:
    app = FastAPI(title="Finance bot", docs_url=None, redoc_url=None)

    @app.get("/app")
    async def mini_app():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/dashboard")
    async def dashboard(period: str = "month", x_init_data: str = Header("", alias="X-Telegram-Init-Data")):
        settings = get_settings()
        try:
            tg = validate_init_data(x_init_data, settings.bot_token)
        except ValueError as e:
            raise HTTPException(401, f"unauthorized: {e}")
        if settings.allowed_ids and tg.get("id") not in settings.allowed_ids:
            raise HTTPException(403, "forbidden")
        async with db.session() as s:
            user = await sv.get_user(s, tg["id"])
            if not user:
                raise HTTPException(404, "send /start to the bot first")
            today = date.today()
            if period not in ("day", "week", "month", "year"):
                period = "month"
            start, end, pstart, pend = sv.period_bounds(period, today)
            cur = await sv.stats(s, user, start, end)
            prev = await sv.stats(s, user, pstart, pend)

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
            txs = (await s.execute(select(Transaction).where(
                Transaction.user_id == user.id, Transaction.occurred_on.between(start, end),
                Transaction.type.in_(("income", "expense"))).order_by(Transaction.occurred_on.desc(),
                                                                    Transaction.id.desc()))).unique().scalars()
            return JSONResponse({
                "currency": user.base_currency, "period": period,
                "start": str(start), "end": str(end),
                "income": _f(cur["income"]), "expense": _f(cur["expense"]), "net": _f(cur["net"]),
                "savings_rate": _f(cur["savings_rate"]) if cur["savings_rate"] is not None else None,
                "prev_expense": _f(prev["expense"]), "prev_income": _f(prev["income"]),
                "by_category": [{"name": k, "value": _f(v)} for k, v in cur["by_category"].items()],
                "daily": [{"date": str(d), "value": _f(v)} for d, v in cur["daily"].items()],
                "months": months,
                "net_worth": _f(nw.net), "assets": _f(nw.assets), "liabilities": _f(nw.liabilities),
                "net_worth_history": [{"date": str(h.day), "value": _f(h.net_worth)} for h in history]
                                     + ([] if history and history[-1].day == today
                                        else [{"date": str(today), "value": _f(nw.net)}]),
                "accounts": [{"name": a.name, "type": a.type, "balance": _f(b), "currency": a.currency,
                              "value": _f(v)} for a, b, v in nw.lines],
                "transactions": [{"date": str(t.occurred_on), "amount": _f(t.base_amount),
                                  "original": f"{_f(t.amount)} {t.currency}",
                                  "category": t.category.name if t.category else "",
                                  "what": t.merchant or t.note, "account": t.account.name}
                                 for t in txs][:300],
            })

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

    return app
