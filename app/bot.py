"""Telegram handlers."""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import available_timezones

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    TelegramObject, WebAppInfo,
)
from sqlalchemy import select

from . import ai, db, fx, importers, ocr, services as sv
from .config import get_settings
from .models import ACCOUNT_TYPES, Receipt, ReceiptItem, ReportSchedule, Transaction, User
from .parser import parse_entry
from .reports import build_report

log = logging.getLogger(__name__)
router = Router()
SETUP_CURRENCIES = ["USD", "EUR", "GBP", "KRW", "TRY", "UZS", "KZT", "RUB", "AED", "JPY", "CNY", "INR"]

HELP = """<b>How to log money</b>
<code>Spent 50k on lunch</code> → just write it naturally
<code>35 coffee</code> → expense (category guessed)
<code>12.5 usd taxi card</code> → expense in USD from Card
<code>+3000 salary</code> → income
<code>500 bank>cash</code> → transfer between accounts
<code>1000 usd>bank 1350000</code> → transfer with the amount received
<code>45 groceries yesterday #trip</code> → date and tag
📷 Send a <b>receipt photo</b> or a <b>payment screenshot</b> (Click, Payme, bank app) → read and categorized
📩 Forward a <b>bank SMS / notification</b> text → logged automatically
📄 Send a bank <b>statement</b> (CSV or PDF) → imported without duplicates
❓ Start a message with <code>?</code> to ask about your money

<b>Commands</b>
/balance – all accounts
/networth – assets, debts, net worth
/report day|week|month|year – report with chart and AI advice
/dashboard – interactive charts
/history – last transactions (edit/delete)
/undo – delete the last entry
/accounts, /addaccount, /value, /default – manage accounts
/reports – automatic report times
/export – CSV of all transactions
/statement – month-end statement (opening vs closing)
/backup – full backup file · /edits – change history
/connect – automatic bank alerts from your phone
/currency, /tz – base currency and timezone
/deleteme – delete all your data"""


# ---------- middleware: whitelist + DB session + user ----------

class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        tg_user = data.get("event_from_user")
        allowed = get_settings().allowed_ids
        if tg_user is None or (allowed and tg_user.id not in allowed):
            if isinstance(event, Message):
                await event.answer("This is a private bot.")
            return None
        ai.current_user.set(tg_user.id)
        async with db.session() as s:
            data["s"] = s
            data["user"] = await sv.get_user(s, tg_user.id)
            result = await handler(event, data)
            await s.commit()
            return result


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def dashboard_button(period: str = "month") -> InlineKeyboardButton | None:
    url = get_settings().webapp_url.rstrip("/")
    if url.startswith("https://"):
        return InlineKeyboardButton(text="📊 Open dashboard", web_app=WebAppInfo(url=f"{url}/app?period={period}"))
    return None


def tx_line(t: Transaction) -> str:
    cat = f"{t.category.emoji} {t.category.name}" if t.category else ("🔁 Transfer" if t.type == "transfer" else "")
    what = t.merchant or t.note
    when = "today" if t.occurred_on == date.today() else t.occurred_on.strftime("%d %b")
    sign = "+" if t.amount > 0 else ""
    return f"{cat} · <b>{sign}{sv.fmt(t.amount, t.currency)}</b> · {html.escape(t.account.name)} · {when}" + \
        (f"\n<i>{html.escape(what)}</i>" if what else "")


def tx_kb(t: Transaction) -> InlineKeyboardMarkup:
    if t.type == "transfer":
        return kb([[("↩️ Undo", f"del:{t.id}")]])
    return kb([[("🗂 Category", f"cat:{t.id}"), ("🏦 Account", f"acc:{t.id}"),
                ("📅 Date", f"date:{t.id}"), ("↩️ Undo", f"del:{t.id}")]])


async def require_user(msg: Message, user: User | None) -> bool:
    if user is None:
        await msg.answer("Send /start first to set up your base currency.")
        return False
    return True


# ---------- setup ----------

@router.message(CommandStart())
async def start(msg: Message, command: CommandObject, s, user: User | None):
    if user:
        await msg.answer(f"Welcome back! Base currency: <b>{user.base_currency}</b>.\n\n{HELP}")
        return
    arg = (command.args or "").strip().upper()
    if len(arg) == 3 and arg.isalpha():
        user = await sv.create_user(s, msg.from_user.id, msg.from_user.full_name, arg,
                                    get_settings().default_timezone)
        await s.commit()
        from .scheduler import schedule_user
        schedule_user(user)
        await msg.answer(_welcome(arg))
        return
    rows = [[(c, f"setup:{c}") for c in SETUP_CURRENCIES[i:i + 4]] for i in range(0, len(SETUP_CURRENCIES), 4)]
    await msg.answer("👋 Let's set up your money tracker.\n\nWhich currency should totals be shown in? "
                     "(You can still log any currency. Another one: <code>/start XYZ</code>)", reply_markup=kb(rows))


def _welcome(cur: str) -> str:
    return (f"✅ Done. Base currency <b>{cur}</b>. I created accounts <b>Cash</b>, <b>Bank</b> and <b>Card</b> "
            f"(Card is the default).\n\nSet your timezone so reports arrive on time, e.g. "
            f"<code>/tz Europe/Istanbul</code> or <code>/tz Asia/Seoul</code>.\n"
            f"Add starting balances like <code>+1500 bank</code>, or new accounts with /addaccount.\n\n{HELP}")


@router.callback_query(F.data.startswith("setup:"))
async def setup_currency(cb: CallbackQuery, s, user):
    if user:
        await cb.answer("Already set up")
        return
    cur = cb.data.split(":")[1]
    user = await sv.create_user(s, cb.from_user.id, cb.from_user.full_name, cur, get_settings().default_timezone)
    from .scheduler import schedule_user  # avoid import cycle
    await s.commit()
    schedule_user(user)
    await cb.message.edit_text(_welcome(cur))


@router.message(Command("help"))
async def help_(msg: Message):
    await msg.answer(HELP)


@router.message(Command("tz"))
async def set_tz(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    tz = (command.args or "").strip()
    if tz not in available_timezones():
        await msg.answer(f"Current timezone: <b>{user.timezone}</b>.\nUsage: <code>/tz Asia/Seoul</code> "
                         "(any IANA name, e.g. Europe/Istanbul, Asia/Tashkent, America/New_York)")
        return
    user.timezone = tz
    from .scheduler import schedule_user
    schedule_user(user)
    await msg.answer(f"🕒 Timezone set to <b>{tz}</b>.")


@router.message(Command("currency"))
async def set_currency(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    cur = (command.args or "").strip().upper()
    if len(cur) != 3:
        await msg.answer(f"Base currency: <b>{user.base_currency}</b>. Change with <code>/currency EUR</code>.")
        return
    try:
        await fx.get_rate(s, user.base_currency, cur)
    except fx.FxUnavailable:
        await msg.answer("I couldn't find exchange rates for that currency.")
        return
    await sv.audit(s, user, "recalc", None, f"base currency {user.base_currency} -> {cur}")
    user.base_currency = cur
    txs = (await s.execute(select(Transaction).where(Transaction.user_id == user.id))).unique().scalars()
    n = 0
    for t in txs:
        rate = await fx.get_rate(s, t.currency, cur, t.occurred_on)
        t.fx_rate_to_base, t.base_amount = rate, (t.amount * rate).quantize(Decimal("0.0001"))
        n += 1
    await msg.answer(f"💱 Base currency is now <b>{cur}</b>. Recalculated {n} transactions.")


# ---------- accounts ----------

@router.message(Command("accounts"))
async def list_accounts(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    lines = ["<b>Accounts</b>"]
    for a in await sv.accounts(s, user):
        star = " ⭐" if a.id == user.default_account_id else ""
        lines.append(f"• {html.escape(a.name)} ({a.type}, {a.currency}){star}")
    lines += ["", "Add: <code>/addaccount Name type CUR [opening]</code>",
              f"Types: {', '.join(ACCOUNT_TYPES)}",
              "Default for entries: <code>/default Name</code>",
              "Value of investments/property: <code>/value Name 12000</code>",
              "Archive: <code>/archive Name</code>"]
    await msg.answer("\n".join(lines))


@router.message(Command("addaccount"))
async def add_account(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or parts[1].lower() not in ACCOUNT_TYPES:
        await msg.answer("Usage: <code>/addaccount Name type [CUR] [opening]</code>\n"
                         f"Types: {', '.join(ACCOUNT_TYPES)}\n"
                         "Examples:\n<code>/addaccount Kaspi card KZT 25000</code>\n"
                         "<code>/addaccount Stocks investment USD</code>\n"
                         "<code>/addaccount CarLoan loan USD 8000</code> (amount you owe)")
        return
    name, type_ = parts[0], parts[1].lower()
    cur = parts[2].upper() if len(parts) > 2 and len(parts[2]) == 3 and parts[2].isalpha() else user.base_currency
    opening = Decimal(0)
    for p in parts[2:]:
        try:
            opening = Decimal(p.replace(",", "."))
        except Exception:
            continue
    if await sv.find_account(s, user, name):
        await msg.answer("An account with that name exists.")
        return
    if type_ in ("loan", "credit_card") and opening > 0:
        opening = -opening  # liabilities are negative balances
    acc = await sv.add_account(s, user, name, type_, cur, opening)
    await msg.answer(f"✅ Added <b>{html.escape(acc.name)}</b> ({type_}, {cur}), balance {sv.fmt(opening, cur)}.")


@router.message(Command("default"))
async def set_default(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    acc = await sv.find_account(s, user, (command.args or "").strip())
    if not acc or not command.args:
        await msg.answer("Usage: <code>/default Card</code>")
        return
    user.default_account_id = acc.id
    await msg.answer(f"⭐ Default account: <b>{html.escape(acc.name)}</b>")


@router.message(Command("archive"))
async def archive(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    acc = await sv.find_account(s, user, (command.args or "").strip())
    if not acc or not command.args:
        await msg.answer("Usage: <code>/archive Name</code>")
        return
    acc.archived = True
    await msg.answer(f"🗄 Archived {html.escape(acc.name)}.")


@router.message(Command("value"))
async def value(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    parts = (command.args or "").split()
    acc = await sv.find_account(s, user, parts[0]) if parts else None
    try:
        val = Decimal(parts[1].replace(",", ""))
    except Exception:
        val = None
    if not acc or val is None:
        await msg.answer("Usage: <code>/value Stocks 15400</code> – sets today's market value of an account "
                         "(investments, crypto, property, vehicle).")
        return
    await sv.set_valuation(s, acc, val)
    await msg.answer(f"📈 {html.escape(acc.name)} valued at {sv.fmt(val, acc.currency)}.")


@router.message(Command("balance"))
async def balance(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    nw = await sv.net_worth(s, user)
    groups = defaultdict(list)
    for acc, bal, in_base in nw.lines:
        groups[acc.type].append((acc, bal, in_base))
    lines = ["<b>Balances</b>"]
    for type_, items in groups.items():
        lines.append(f"\n<i>{type_.replace('_', ' ').title()}</i>")
        for acc, bal, in_base in items:
            conv = f" ≈ {sv.fmt(in_base, user.base_currency)}" if acc.currency != user.base_currency else ""
            lines.append(f"• {html.escape(acc.name)}: <b>{sv.fmt(bal, acc.currency)}</b>{conv}")
    lines.append(f"\nNet worth: <b>{sv.fmt(nw.net, user.base_currency)}</b>")
    await msg.answer("\n".join(lines))


@router.message(Command("networth"))
async def networth(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    nw = await sv.net_worth(s, user)
    c = user.base_currency
    assets = [(a, v) for a, _, v in nw.lines if not a.is_liability and v and a.include_in_net_worth]
    debts = [(a, v) for a, _, v in nw.lines if a.is_liability and v]
    lines = [f"🏦 <b>Net worth: {sv.fmt(nw.net, c)}</b>", "",
             f"<b>Assets {sv.fmt(nw.assets, c)}</b>"]
    lines += [f"• {html.escape(a.name)}: {sv.fmt(v, c)}" for a, v in sorted(assets, key=lambda x: -x[1])]
    lines += ["", f"<b>Liabilities {sv.fmt(nw.liabilities, c)}</b>"]
    lines += [f"• {html.escape(a.name)}: {sv.fmt(abs(v), c)}" for a, v in debts] or ["• none"]
    await sv.take_snapshot(s, user)
    today = date.today()
    hist = []
    for label, d in (("30 days ago", today - timedelta(days=30)), ("start of year", today.replace(month=1, day=1)),
                     ("1 year ago", today - timedelta(days=365))):
        snap = await sv.snapshot_on_or_before(s, user, d)
        if snap and snap.day < today:
            ch = nw.net - snap.net_worth
            hist.append(f"• vs {label} ({snap.day:%d %b %Y}): {'+' if ch >= 0 else ''}{sv.fmt(ch, c)}")
    if hist:
        lines += ["", "<b>Change</b>"] + hist
    lines += ["", f"<i>Saved as today's record ({today:%d %b %Y}). Month statement: /statement · "
                  "full backup: /backup</i>"]
    await msg.answer("\n".join(lines))


# ---------- reports ----------

async def send_report(bot: Bot, s, user: User, kind: str, chat_id: int | None = None):
    rep = await build_report(s, user, kind)
    chat_id = chat_id or user.telegram_id
    period = {"daily": "day", "weekly": "week", "monthly": "month"}.get(kind, kind)
    btn = dashboard_button(period)
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None
    if rep.chart:
        await bot.send_photo(chat_id, BufferedInputFile(rep.chart, "report.png"))
    for i in range(0, len(rep.text), 4000):
        chunk = rep.text[i:i + 4000]
        await bot.send_message(chat_id, chunk, reply_markup=markup if i + 4000 >= len(rep.text) else None)


@router.message(Command("report"))
async def report(msg: Message, command: CommandObject, bot: Bot, s, user):
    if not await require_user(msg, user):
        return
    kind = (command.args or "month").strip().lower()
    kind = {"today": "day", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}.get(kind, kind)
    if kind not in ("day", "week", "month", "year"):
        await msg.answer("Usage: <code>/report day|week|month|year</code>")
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    await send_report(bot, s, user, kind, msg.chat.id)


@router.message(Command("dashboard"))
async def dashboard(msg: Message, user):
    btn = dashboard_button()
    if not btn:
        await msg.answer("The dashboard needs a public HTTPS address. Set WEBAPP_URL in the server's .env "
                         "(see README → Dashboard).")
        return
    await msg.answer("Your interactive dashboard:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]))


@router.message(Command("reports"))
async def reports_settings(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    args = (command.args or "").split()
    rows = {r.kind: r for r in (await s.execute(select(ReportSchedule).where(
        ReportSchedule.user_id == user.id))).scalars()}
    if len(args) == 2 and args[0] in ("daily", "weekly", "monthly"):
        r = rows.get(args[0]) or ReportSchedule(user_id=user.id, kind=args[0], send_time="21:00")
        s.add(r)
        if args[1] == "off":
            r.enabled = False
        elif args[1] == "noai":
            r.include_ai = not r.include_ai
        else:
            try:
                hh, mm = map(int, args[1].split(":"))
                assert 0 <= hh < 24 and 0 <= mm < 60
            except Exception:
                await msg.answer("Time format is HH:MM, e.g. <code>/reports daily 21:30</code>")
                return
            r.send_time, r.enabled = f"{hh:02d}:{mm:02d}", True
        await s.flush()
        rows[args[0]] = r
        from .scheduler import schedule_user
        schedule_user(user, list(rows.values()))
    when = {"daily": "every day", "weekly": "every Sunday", "monthly": "on the 1st"}
    lines = [f"<b>Automatic reports</b> ({user.timezone})"]
    for k in ("daily", "weekly", "monthly"):
        r = rows.get(k)
        state = f"{when[k]} at {r.send_time}" + ("" if r.include_ai else ", no AI") if r and r.enabled else "off"
        lines.append(f"• {k.title()}: {state}")
    lines += ["", "Change: <code>/reports daily 21:30</code> · <code>/reports weekly off</code> · "
                  "<code>/reports monthly noai</code> (toggle AI)"]
    await msg.answer("\n".join(lines))


# ---------- history / edit ----------

@router.message(Command("history"))
async def history(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    txs = await sv.history(s, user, 15)
    if not txs:
        await msg.answer("No transactions yet. Try <code>12 coffee</code>.")
        return
    seen, rows, lines = set(), [], ["<b>Recent transactions</b>"]
    for i, t in enumerate(txs, 1):
        if t.transfer_group and t.transfer_group in seen:
            continue
        seen.add(t.transfer_group)
        lines.append(f"{i}. {tx_line(t)}")
        rows.append((f"✏️ {i}", f"edit:{t.id}"))
    await msg.answer("\n".join(lines), reply_markup=kb([rows[i:i + 5] for i in range(0, len(rows), 5)]))


@router.message(Command("undo"))
async def undo(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    t = await sv.last_transaction(s, user)
    if not t:
        await msg.answer("Nothing to undo.")
        return
    line = tx_line(t)
    await sv.delete_transaction(s, user, t.id)
    await msg.answer(f"🗑 Deleted: {line}")


@router.message(Command("export"))
async def export(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    data = await sv.export_csv(s, user)
    await msg.answer_document(BufferedInputFile(data, f"transactions_{date.today()}.csv"),
                              caption="All transactions (opens in Excel / Google Sheets).")


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, s, user):
    t = await s.get(Transaction, int(cb.data.split(":")[1]))
    if not t or t.user_id != user.id:
        await cb.answer("Not found")
        return
    await cb.message.answer(tx_line(t), reply_markup=tx_kb(t))
    await cb.answer()


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery, s, user):
    ok = await sv.delete_transaction(s, user, int(cb.data.split(":")[1]))
    await cb.answer("Deleted" if ok else "Already gone")
    if ok:
        await cb.message.edit_text(f"<s>{cb.message.html_text}</s>\n🗑 deleted")


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery, s, user):
    t = await s.get(Transaction, int(cb.data.split(":")[1]))
    if not t or t.user_id != user.id:
        await cb.answer("Not found")
        return
    cats = await sv.categories(s, user, t.type if t.type in ("income", "expense") else "expense")
    btns = [(f"{c.emoji} {c.name}", f"setcat:{t.id}:{c.id}") for c in cats]
    await cb.message.edit_reply_markup(reply_markup=kb([btns[i:i + 3] for i in range(0, len(btns), 3)]))
    await cb.answer()


@router.callback_query(F.data.startswith("setcat:"))
async def cb_setcat(cb: CallbackQuery, s, user):
    _, tid, cid = cb.data.split(":")
    t = await s.get(Transaction, int(tid))
    cat = await s.get(sv.Category, int(cid))
    if not t or t.user_id != user.id or not cat:
        await cb.answer("Not found")
        return
    await sv.audit(s, user, "update", t, f"category -> {cat.name}")
    t.category_id = cat.id
    if t.merchant or t.note:
        await sv.learn_rule(s, user, t.merchant or t.note, cat, source="user")
    await s.flush()
    await s.refresh(t, ["category", "account"])
    await cb.message.edit_text(tx_line(t), reply_markup=tx_kb(t))
    await cb.answer(f"Saved. I'll remember '{(t.merchant or t.note)[:20]}' → {cat.name}")


@router.callback_query(F.data.startswith("acc:"))
async def cb_account(cb: CallbackQuery, s, user):
    tid = int(cb.data.split(":")[1])
    btns = [(a.name, f"setacc:{tid}:{a.id}") for a in await sv.accounts(s, user)]
    await cb.message.edit_reply_markup(reply_markup=kb([btns[i:i + 3] for i in range(0, len(btns), 3)]))
    await cb.answer()


@router.callback_query(F.data.startswith("setacc:"))
async def cb_setacc(cb: CallbackQuery, s, user):
    _, tid, aid = cb.data.split(":")
    t = await s.get(Transaction, int(tid))
    acc = await s.get(sv.Account, int(aid))
    if not t or t.user_id != user.id or not acc or acc.user_id != user.id:
        await cb.answer("Not found")
        return
    await sv.audit(s, user, "update", t, f"account -> {acc.name}")
    t.account_id = acc.id
    t.account_amount = await fx.convert(s, t.amount, t.currency, acc.currency, t.occurred_on)
    await s.flush()
    await s.refresh(t, ["category", "account"])
    await cb.message.edit_text(tx_line(t), reply_markup=tx_kb(t))
    await cb.answer("Account changed")


@router.callback_query(F.data.startswith("date:"))
async def cb_date(cb: CallbackQuery):
    tid = cb.data.split(":")[1]
    today = date.today()
    opts = [(("Today" if i == 0 else "Yesterday" if i == 1 else (today - timedelta(days=i)).strftime("%a %d")),
             f"setdate:{tid}:{i}") for i in range(0, 7)]
    await cb.message.edit_reply_markup(reply_markup=kb([opts[:4], opts[4:]]))
    await cb.answer()


@router.callback_query(F.data.startswith("setdate:"))
async def cb_setdate(cb: CallbackQuery, s, user):
    _, tid, back = cb.data.split(":")
    t = await s.get(Transaction, int(tid))
    if not t or t.user_id != user.id:
        await cb.answer("Not found")
        return
    new_day = date.today() - timedelta(days=int(back))
    await sv.audit(s, user, "update", t, f"date -> {new_day}")
    t.occurred_on = new_day
    await s.flush()
    await cb.message.edit_text(tx_line(t), reply_markup=tx_kb(t))
    await cb.answer("Date changed")


# ---------- bank alerts from phone ----------

@router.message(Command("connect"))
async def connect(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    url = get_settings().webapp_url.rstrip("/")
    if not url:
        await msg.answer("Automatic alerts need the bot's web address (WEBAPP_URL in .env). Until then, just "
                         "<b>forward or paste</b> your bank's SMS/notification text here and I'll log it.")
        return
    await msg.answer(
        "<b>Automatic bank alerts</b>\nSet your phone to send each bank SMS / notification to:\n"
        f"<code>{url}/ingest/{user.ingest_token}</code>\n(HTTP POST, body = the alert text)\n\n"
        "• <b>iPhone</b>: Shortcuts → Automation → Message (sender = your bank) → "
        "'Get contents of URL' (POST, Request body: Text = Message content).\n"
        "• <b>Android</b>: an SMS-forwarder app (e.g. 'SMS Forwarder' or Tasker) → webhook to the URL above.\n"
        "• <b>Email alerts</b>: any email-to-webhook service pointed at the same URL.\n\n"
        "Keep this link private. Or simply forward alert texts to this chat.")


async def _handle_bank_text(msg: Message, s, user: User, text: str):
    cats = [c.name for c in await sv.categories(s, user)]
    try:
        tx = await ai.parse_bank_message(text, cats, user.base_currency)
    except ai.AiError as e:
        await msg.answer(f"Couldn't read that: {html.escape(str(e))}")
        return
    if not tx.is_transaction or tx.amount <= 0:
        await msg.answer("That doesn't look like a transaction. Format: <code>35 coffee</code>. /help")
        return
    t, status = await importers.save_bank_tx(s, user, tx, "alert", raw=text)
    if status == "duplicate":
        await msg.answer("Already logged this one ✅")
    elif status == "merged":
        await msg.answer(f"Matched an entry you already logged ✅\n{tx_line(t)}", reply_markup=tx_kb(t))
    else:
        await msg.answer(f"✅ Logged:\n{tx_line(t)}", reply_markup=tx_kb(t))


# ---------- receipts ----------

async def _download(bot: Bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = await bot.download_file(f.file_path)
    return buf.read()


@router.message(F.photo)
async def receipt_photo(msg: Message, bot: Bot, s, user):
    """Receipt photos and payment/transfer screenshots."""
    if not await require_user(msg, user):
        return
    if not ai.enabled() and not ocr.available():
        await msg.answer("Receipt reading isn't switched on for this bot yet. Type it instead, e.g. <code>120k groceries</code>.")
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    photo = msg.photo[-1]
    data = await _download(bot, photo.file_id)
    await _process_receipt(msg, s, user, data, "image/jpeg", photo.file_id)


async def _read_with_ocr(data: bytes) -> ai.ReceiptData:
    """Free Tesseract reading, shaped like the AI result so the same save flow is used."""
    res = await ocr.read_image(data)
    items = [ai.ReceiptLine(name=i.name, amount=float(i.amount), category="") for i in res.items]
    if res.total and not items:
        items = [ai.ReceiptLine(name=res.merchant or "Purchase", amount=float(res.total), category="")]
    return ai.ReceiptData(is_receipt=res.total is not None, is_income=res.is_income,
                          merchant=res.merchant, date=str(res.on) if res.on else None,
                          currency=res.currency, total=float(res.total or 0), items=items)


async def _process_receipt(msg: Message, s, user: User, data: bytes, media_type: str, file_id: str):
    cats = await sv.categories(s, user, "expense")
    used_ocr = not ai.enabled()
    try:
        if used_ocr:
            r = await _read_with_ocr(data)
        else:
            r = await ai.read_receipt(data, media_type, [c.name for c in cats], user.base_currency)
    except ai.AiError as e:
        await msg.answer(f"Couldn't read the receipt: {html.escape(str(e))}")
        return
    except Exception:
        log.exception("receipt reading failed")
        await msg.answer("Couldn't read this image. Try a sharper, straight photo, or type it like "
                         "<code>120k groceries</code>.")
        return
    if not r.is_receipt or r.total <= 0:
        await msg.answer("I don't see a receipt or payment in this image. For a manual entry type e.g. "
                         "<code>35 coffee</code>.")
        return
    if r.is_income:
        cat, _ = await sv.guess_category(s, user, r.merchant, "income")
        acc = await sv.find_account(s, user, None)
        t = await sv.add_transaction(s, user, acc, "income", Decimal(str(r.total)),
                                     (r.currency or user.base_currency), cat, merchant=r.merchant,
                                     on=importers._to_date(r.date), created_via="receipt")
        await msg.answer(f"💰 Income from screenshot:\n{tx_line(t)}", reply_markup=tx_kb(t))
        return
    rec = Receipt(user_id=user.id, telegram_file_id=file_id, merchant=r.merchant[:120],
                  purchased_on=importers._to_date(r.date), total=Decimal(str(r.total)),
                  currency=(r.currency or user.base_currency).upper(), raw_ai_json=r.model_dump())
    s.add(rec)
    await s.flush()
    by_name = {c.name.lower(): c for c in cats}
    groups: dict[str, Decimal] = defaultdict(Decimal)
    for it in r.items:
        cat, _ = (by_name.get(it.category.lower()), True)
        if cat is None:
            cat, _ = await sv.guess_category(s, user, it.name, "expense")
        s.add(ReceiptItem(receipt_id=rec.id, name=it.name[:200], amount=Decimal(str(it.amount)), category_id=cat.id))
        groups[f"{cat.emoji} {cat.name}"] += Decimal(str(it.amount))
    items_sum = sum(groups.values())
    lines = [f"🧾 <b>{html.escape(r.merchant or 'Receipt')}</b> · {sv.fmt(r.total, rec.currency)}"
             + (f" · {rec.purchased_on:%d %b}" if rec.purchased_on else ""), ""]
    lines += [f"• {html.escape(k)}: {sv.fmt(v, rec.currency)}" for k, v in sorted(groups.items(), key=lambda x: -x[1])]
    lines.append(f"\n{len(r.items)} lines read.")
    if used_ocr:
        lines.append("<i>Read with free text recognition. Check the total; if it's wrong, tap Discard and "
                     "type it, e.g. <code>120k groceries</code>.</i>")
    if items_sum and abs(items_sum - rec.total) > Decimal("0.05") * rec.total:
        lines.append(f"⚠️ Lines add up to {sv.fmt(items_sum, rec.currency)}; the total will be used and the "
                     "difference added to the largest category.")
    accs = await sv.accounts(s, user)
    default = await sv.find_account(s, user, None)
    lines.append(f"Paid from: <b>{html.escape(default.name)}</b>")
    rows = [[("✅ Save", f"rsave:{rec.id}:{default.id}"), ("❌ Discard", f"rdrop:{rec.id}")],
            [(f"From {a.name}", f"rsave:{rec.id}:{a.id}") for a in accs if a.id != default.id][:3]]
    await msg.answer("\n".join(lines), reply_markup=kb(rows))


@router.callback_query(F.data.startswith("rdrop:"))
async def cb_receipt_drop(cb: CallbackQuery, s, user):
    rec = await s.get(Receipt, int(cb.data.split(":")[1]))
    if rec and rec.user_id == user.id:
        rec.status = "discarded"
    await cb.message.edit_text(cb.message.html_text + "\n\n❌ Discarded")
    await cb.answer()


@router.callback_query(F.data.startswith("rsave:"))
async def cb_receipt_save(cb: CallbackQuery, s, user):
    _, rid, aid = cb.data.split(":")
    rec = await s.get(Receipt, int(rid))
    acc = await s.get(sv.Account, int(aid))
    if not rec or rec.user_id != user.id or rec.status != "pending" or not acc:
        await cb.answer("Already handled")
        return
    items = (await s.execute(select(ReceiptItem).where(ReceiptItem.receipt_id == rec.id))).scalars().all()
    on = rec.purchased_on or date.today()
    groups: dict[int, Decimal] = defaultdict(Decimal)
    for it in items:
        groups[it.category_id] += it.amount
    if not groups:
        other, _ = await sv.guess_category(s, user, rec.merchant, "expense")
        groups[other.id] = rec.total
    diff = rec.total - sum(groups.values())
    biggest = max(groups, key=groups.get)
    groups[biggest] += diff

    note = ""
    dup = await sv.find_duplicate(s, user, rec.total, rec.currency, on, days=1)
    if dup and dup.created_via in ("alert", "import") and dup.receipt_id is None:
        # Bank already reported this purchase: replace it with the detailed receipt split
        acc = dup.account
        await sv.delete_transaction(s, user, dup.id)
        note = "\n🔗 Replaced the matching bank entry with this receipt's details."
    for cid, amt in groups.items():
        if amt <= 0:
            continue
        cat = await s.get(sv.Category, cid)
        await sv.add_transaction(s, user, acc, "expense", amt, rec.currency, cat, merchant=rec.merchant,
                                 on=on, created_via="receipt", receipt_id=rec.id)
    if rec.merchant and len(groups) == 1:
        await sv.learn_rule(s, user, rec.merchant, await s.get(sv.Category, biggest), source="ai")
    rec.status = "confirmed"
    await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Saved from {html.escape(acc.name)}{note}")
    await cb.answer("Saved")


# ---------- statements ----------

@router.message(F.document)
async def document(msg: Message, bot: Bot, s, user):
    if not await require_user(msg, user):
        return
    doc = msg.document
    name = (doc.file_name or "").lower()
    mime = doc.mime_type or ""
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await msg.answer("File too large (max 15 MB).")
        return
    data = await _download(bot, doc.file_id)
    if mime.startswith("image/"):
        if ai.enabled() or ocr.available():
            await _process_receipt(msg, s, user, data, mime, doc.file_id)
        return
    if not name.endswith((".csv", ".txt", ".pdf")):
        await msg.answer("Send a bank statement as CSV or PDF, or a receipt as a photo.")
        return
    acc = await sv.find_account(s, user, (msg.caption or "").strip() or None)
    if acc is None:
        await msg.answer("Add the account name as the file caption, e.g. <code>Bank</code>.")
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    try:
        res = await importers.import_statement(s, user, acc, data, name,
                                               "application/pdf" if name.endswith(".pdf") else "text/csv")
    except ai.AiError as e:
        await msg.answer(f"Couldn't import: {html.escape(str(e))}")
        return
    total_out = sum(-t.base_amount for t in res.added if t.type == "expense")
    await msg.answer(f"📄 Imported into <b>{html.escape(acc.name)}</b>: {len(res.added)} new, "
                     f"{res.skipped} already imported, {res.merged} matched existing entries.\n"
                     f"New spending: {sv.fmt(total_out, user.base_currency)}. See /history or /report month.")


# ---------- questions & quick entry ----------

@router.message(Command("ask"))
async def ask_cmd(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    await _ask(msg, s, user, command.args or "")


async def _ask(msg: Message, s, user: User, question: str):
    if not question.strip():
        await msg.answer("Ask e.g. <code>? how much did I spend on food this month</code>")
        return
    if not ai.enabled():
        await msg.answer("Questions aren't switched on for this bot yet. Try /report month.")
        return
    today = date.today()
    ctx = {"today": str(today), "base_currency": user.base_currency, "months": []}
    m_end = today
    for _ in range(6):
        m_start = m_end.replace(day=1)
        st = await sv.stats(s, user, m_start, m_end)
        ctx["months"].append({"month": m_start.strftime("%Y-%m"), "income": float(st["income"]),
                              "expenses": float(st["expense"]),
                              "by_category": {k: float(v) for k, v in st["by_category"].items()},
                              "top_merchants": {k: float(v) for k, v in st["merchants"].items()}})
        m_end = m_start - timedelta(days=1)
    nw = await sv.net_worth(s, user)
    ctx["net_worth"] = float(nw.net)
    ctx["accounts"] = {a.name: f"{b:.2f} {a.currency}" for a, b, _ in nw.lines}
    try:
        await msg.answer(html.escape(await ai.answer(question, ctx)))
    except ai.AiError as e:
        await msg.answer(f"Couldn't answer: {html.escape(str(e))}")


@router.message(F.text & ~F.text.startswith("/"))
async def text_entry(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    text = msg.text.strip()
    if text.startswith("?"):
        await _ask(msg, s, user, text[1:])
        return
    accs = await sv.accounts(s, user)
    entry = parse_entry(text, {a.name for a in accs})
    # Sentences that don't start with the amount (bank alerts, "spent 50k on lunch") are read by
    # AI when available; otherwise the simple sentence parser result is used.
    if entry is not None and entry.free_form and ai.enabled() and len(text.split()) > 3:
        entry = None
    if entry is None:
        if ai.enabled() and any(ch.isdigit() for ch in text):
            await _handle_bank_text(msg, s, user, text)
        else:
            await msg.answer("Start with an amount, e.g. <code>35 coffee</code> or <code>+2000 salary</code>. /help")
        return

    if entry.kind == "transfer":
        src = await sv.find_account(s, user, entry.account)
        dst = await sv.find_account(s, user, entry.to_account)
        if not src or not dst:
            missing = entry.account if not src else entry.to_account
            await msg.answer(f"Unknown account '{html.escape(missing or '')}'. See /accounts.")
            return
        out, inn = await sv.add_transfer(s, user, src, dst, entry.amount, entry.to_amount, entry.text, entry.on)
        await msg.answer(f"🔁 {sv.fmt(entry.amount, src.currency)} {html.escape(src.name)} → "
                         f"{sv.fmt(inn.amount, dst.currency)} {html.escape(dst.name)}",
                         reply_markup=kb([[("↩️ Undo", f"del:{out.id}")]]))
        return

    acc = await sv.find_account(s, user, entry.account)
    if acc is None:
        await msg.answer("No default account. Set one with /default Card")
        return
    if not entry.words and entry.kind == "income" and text.startswith("+"):
        # "+1500 bank" with no description = opening balance / top-up
        cat = await sv.category_by_name(s, user, "Other income", "income")
        confident = True
    else:
        cat, confident = await sv.guess_category(
            s, user, entry.text, entry.kind, ai.pick_category if ai.enabled() else None)
    try:
        t = await sv.add_transaction(s, user, acc, entry.kind, entry.amount, entry.currency, cat,
                                     note=entry.text, tags=entry.tags, on=entry.on)
    except fx.FxUnavailable:
        await msg.answer("Couldn't get an exchange rate right now. Try again in a minute.")
        return
    await s.refresh(t, ["category", "account"])
    hint = "" if confident else "\n<i>Not sure about the category, tap 🗂 to set it (I'll remember).</i>"
    await msg.answer(tx_line(t) + hint, reply_markup=tx_kb(t))


# ---------- privacy ----------

@router.message(Command("deleteme"))
async def delete_me(msg: Message, user):
    if not await require_user(msg, user):
        return
    await msg.answer("⚠️ This permanently deletes <b>all</b> your accounts, transactions and settings.",
                     reply_markup=kb([[("Yes, delete everything", "wipe:yes"), ("Cancel", "wipe:no")]]))


@router.callback_query(F.data.startswith("wipe:"))
async def cb_wipe(cb: CallbackQuery, s, user):
    if cb.data != "wipe:yes" or user is None:
        await cb.message.edit_text("Cancelled.")
        return
    from sqlalchemy import delete as sql_delete
    from .models import (Account, AiInsight, AssetValuation, AuditLog, Category, CategoryRule, Receipt,
                         ReceiptItem as RI, Snapshot, SnapshotLine)
    rec_ids = select(Receipt.id).where(Receipt.user_id == user.id)
    acc_ids = select(Account.id).where(Account.user_id == user.id)
    snap_ids = select(Snapshot.id).where(Snapshot.user_id == user.id)
    for stmt in (sql_delete(SnapshotLine).where(SnapshotLine.snapshot_id.in_(snap_ids)),
                 sql_delete(Snapshot).where(Snapshot.user_id == user.id),
                 sql_delete(AuditLog).where(AuditLog.user_id == user.id),
                 sql_delete(Transaction).where(Transaction.user_id == user.id),
                 sql_delete(RI).where(RI.receipt_id.in_(rec_ids)),
                 sql_delete(Receipt).where(Receipt.user_id == user.id),
                 sql_delete(AssetValuation).where(AssetValuation.account_id.in_(acc_ids)),
                 sql_delete(Account).where(Account.user_id == user.id),
                 sql_delete(CategoryRule).where(CategoryRule.user_id == user.id),
                 sql_delete(Category).where(Category.user_id == user.id),
                 sql_delete(ReportSchedule).where(ReportSchedule.user_id == user.id),
                 sql_delete(AiInsight).where(AiInsight.user_id == user.id),
                 sql_delete(User).where(User.id == user.id)):
        await s.execute(stmt)
    from .scheduler import scheduler
    for kind in ("daily", "weekly", "monthly", "snapshot"):
        if scheduler.get_job(f"{user.id}:{kind}"):
            scheduler.remove_job(f"{user.id}:{kind}")
    await cb.message.edit_text("🗑 All your data has been deleted. Send /start to begin again.")


# ---------- statements & backups ----------

def statement_text(st: sv.Statement) -> str:
    c = st.currency
    op = st.opening.net_worth if st.opening else None
    lines = [f"📒 <b>Statement {st.start:%d %b %Y} – {st.end:%d %b %Y}</b>", ""]
    if op is not None:
        lines.append(f"Net worth at start ({st.opening.day:%d %b}): <b>{sv.fmt(op, c)}</b>")
    lines.append(f"Net worth at end ({st.closing.day:%d %b}): <b>{sv.fmt(st.closing.net_worth, c)}</b>")
    if op is not None:
        sign = "+" if st.change >= 0 else ""
        lines += [f"Change: <b>{sign}{sv.fmt(st.change, c)}</b>", "",
                  f"• Income: {sv.fmt(st.income, c)}", f"• Spending: {sv.fmt(st.expense, c)}",
                  f"• Saved: {sv.fmt(st.saved, c)}",
                  f"• Value / exchange-rate / corrections: {sv.fmt(st.other, c)}"]
    else:
        lines += ["", f"• Income: {sv.fmt(st.income, c)}", f"• Spending: {sv.fmt(st.expense, c)}",
                  "<i>No record from before this period yet, so the opening balance is unknown. "
                  "Records are saved every night from now on.</i>"]
    lines += ["", "<b>Accounts</b> (start → end)"]
    for name, cur, o, cl in st.lines:
        lines.append(f"• {html.escape(name)}: {sv.fmt(o, c)} → {sv.fmt(cl, c)}")
    return "\n".join(lines)


@router.message(Command("statement"))
async def statement_cmd(msg: Message, command: CommandObject, s, user):
    if not await require_user(msg, user):
        return
    arg = (command.args or "").strip().lower()
    today = date.today()
    try:
        if arg in ("", "month"):
            start, end = today.replace(day=1), today
        elif arg in ("last", "lastmonth", "previous"):
            end = today.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif arg == "year":
            start, end = today.replace(month=1, day=1), today
        elif len(arg) == 4 and arg.isdigit():
            start, end = date(int(arg), 1, 1), min(date(int(arg), 12, 31), today)
        else:
            y, m = map(int, arg.split("-"))
            start = date(y, m, 1)
            end = min((date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)), today)
    except ValueError:
        await msg.answer("Usage: <code>/statement</code> (this month), <code>/statement last</code>, "
                         "<code>/statement 2026-10</code>, <code>/statement 2026</code>")
        return
    st = await sv.statement(s, user, start, end)
    await msg.answer(statement_text(st))
    await msg.answer_document(BufferedInputFile(sv.statement_csv(st), f"statement_{start}_{end}.csv"))


async def send_backup(bot: Bot, s, user: User, chat_id: int | None = None, caption: str = ""):
    data = await sv.backup_zip(s, user)
    await bot.send_document(chat_id or user.telegram_id,
                            BufferedInputFile(data, f"money_backup_{date.today()}.zip"),
                            caption=caption or "🗄 Full backup: all transactions, accounts, net worth history "
                                               "and edit history (CSV for Excel + JSON). Keep it pinned or saved.")


@router.message(Command("backup"))
async def backup_cmd(msg: Message, bot: Bot, s, user):
    if not await require_user(msg, user):
        return
    await send_backup(bot, s, user, msg.chat.id)


@router.message(Command("edits"))
async def edits_cmd(msg: Message, s, user):
    if not await require_user(msg, user):
        return
    from .models import AuditLog
    rows = (await s.execute(select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.action != "create")
                            .order_by(AuditLog.id.desc()).limit(15))).scalars().all()
    if not rows:
        await msg.answer("No edits or deletions yet. Every change will be listed here.")
        return
    lines = ["<b>Recent edits & deletions</b>"]
    for a in rows:
        d = a.data or {}
        what = f"{d.get('amount', '')} {d.get('currency', '')} {d.get('merchant') or d.get('note') or ''}".strip()
        lines.append(f"• {a.at:%d %b %H:%M} {a.action}: {html.escape(what)} {html.escape(a.detail)}")
    lines.append("\nThe full history is in /backup (edit_history.csv).")
    await msg.answer("\n".join(lines))
