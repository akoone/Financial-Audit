"""Business logic: users, accounts, transactions, balances, net worth and period statistics."""
from __future__ import annotations

import csv
import io
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import fx
from .categories import DEFAULT_CATEGORIES, FALLBACK, keyword_category
from .models import (
    LIABILITY_TYPES, VALUED_TYPES, Account, AssetValuation, Category, CategoryRule,
    ReportSchedule, Transaction, User,
)

Q2 = Decimal("0.01")


def fmt(amount: Decimal | float, currency: str = "") -> str:
    a = Decimal(str(amount)).quantize(Q2)
    s = f"{a:,.2f}"
    return f"{s} {currency}".strip()


def local_today(user: User | None) -> date:
    """Today's date in the user's own timezone (the server usually runs on UTC)."""
    try:
        return datetime.now(ZoneInfo(user.timezone or "UTC")).date() if user else date.today()
    except Exception:
        return date.today()


# ---------- users ----------

async def get_user(s: AsyncSession, telegram_id: int) -> User | None:
    return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


async def create_user(s: AsyncSession, telegram_id: int, name: str, currency: str, tz: str) -> User:
    user = User(telegram_id=telegram_id, name=name, base_currency=currency.upper(), timezone=tz,
                ingest_token=secrets.token_urlsafe(24))
    s.add(user)
    await s.flush()
    for kind, cats in DEFAULT_CATEGORIES.items():
        for name_, emoji, _ in cats:
            s.add(Category(user_id=user.id, name=name_, kind=kind, emoji=emoji))
    accounts = [Account(user_id=user.id, name=n, type=t, currency=user.base_currency)
                for n, t in (("Cash", "cash"), ("Bank", "bank"), ("Card", "card"))]
    s.add_all(accounts)
    await s.flush()
    user.default_account_id = accounts[2].id
    for kind, t in (("daily", "21:00"), ("weekly", "20:00"), ("monthly", "10:00")):
        s.add(ReportSchedule(user_id=user.id, kind=kind, send_time=t))
    await s.flush()
    return user


# ---------- accounts & categories ----------

async def accounts(s: AsyncSession, user: User, include_archived=False) -> list[Account]:
    q = select(Account).where(Account.user_id == user.id)
    if not include_archived:
        q = q.where(Account.archived.is_(False))
    return list((await s.execute(q.order_by(Account.id))).scalars())


async def find_account(s: AsyncSession, user: User, name: str | None) -> Account | None:
    if not name:
        return await s.get(Account, user.default_account_id) if user.default_account_id else None
    for a in await accounts(s, user):
        if a.name.lower() == name.lower():
            return a
    return None


async def add_account(s: AsyncSession, user: User, name: str, type_: str, currency: str,
                      opening: Decimal = Decimal(0)) -> Account:
    acc = Account(user_id=user.id, name=name, type=type_, currency=currency.upper(),
                  opening_balance=opening)
    s.add(acc)
    await s.flush()
    return acc


async def categories(s: AsyncSession, user: User, kind: str | None = None) -> list[Category]:
    q = select(Category).where(Category.user_id == user.id)
    if kind:
        q = q.where(Category.kind == kind)
    return list((await s.execute(q.order_by(Category.id))).scalars())


async def category_by_name(s: AsyncSession, user: User, name: str, kind: str) -> Category | None:
    for c in await categories(s, user, kind):
        if c.name.lower() == name.lower():
            return c
    return None


async def guess_category(s: AsyncSession, user: User, text: str, kind: str,
                         ai_guess=None) -> tuple[Category, bool]:
    """Pick a category: learned rules -> keywords -> AI (optional) -> fallback.

    Returns (category, confident)."""
    low = text.lower()
    rules = (await s.execute(select(CategoryRule).where(CategoryRule.user_id == user.id))).scalars()
    best = None
    for r in rules:
        if r.pattern in low and (best is None or len(r.pattern) > len(best.pattern)):
            best = r
    if best:
        cat = await s.get(Category, best.category_id)
        if cat and cat.kind == kind:
            best.hits += 1
            return cat, True
    if name := keyword_category(text, kind):
        if cat := await category_by_name(s, user, name, kind):
            return cat, True
    if ai_guess and text.strip():
        cats = await categories(s, user, kind)
        try:
            name = await ai_guess(text, [c.name for c in cats])
        except Exception:
            name = None
        if name and (cat := await category_by_name(s, user, name, kind)):
            await learn_rule(s, user, text, cat, source="ai")
            return cat, True
    return await category_by_name(s, user, FALLBACK[kind], kind), False


async def learn_rule(s: AsyncSession, user: User, text: str, cat: Category, source="user"):
    """Remember text -> category. Uses the merchant/first meaningful word(s)."""
    pattern = " ".join(text.lower().split()[:2]).strip()[:100]
    if len(pattern) < 2:
        return
    existing = (await s.execute(select(CategoryRule).where(
        CategoryRule.user_id == user.id, CategoryRule.pattern == pattern))).scalar_one_or_none()
    if existing:
        if source == "user" or existing.source == "ai":
            existing.category_id, existing.source = cat.id, source
    else:
        s.add(CategoryRule(user_id=user.id, pattern=pattern, category_id=cat.id, source=source))
    await s.flush()


# ---------- transactions ----------

async def add_transaction(s: AsyncSession, user: User, account: Account, kind: str, amount: Decimal,
                          currency: str | None = None, category: Category | None = None,
                          note: str = "", merchant: str = "", tags: list | None = None,
                          on: date | None = None, created_via="text", external_ref: str | None = None,
                          receipt_id: int | None = None) -> Transaction:
    """kind: income | expense | adjustment. `amount` positive (adjustment may be signed)."""
    on = on or local_today(user)
    currency = (currency or account.currency).upper()
    signed = -abs(amount) if kind == "expense" else (abs(amount) if kind == "income" else amount)
    acc_amount = await fx.convert(s, signed, currency, account.currency, on)
    rate = await fx.get_rate(s, currency, user.base_currency, on)
    tx = Transaction(user_id=user.id, account_id=account.id, type=kind, amount=signed,
                     currency=currency, account_amount=acc_amount,
                     base_amount=(signed * rate).quantize(Decimal("0.0001")), fx_rate_to_base=rate,
                     category_id=category.id if category else None, note=note[:255],
                     merchant=merchant[:120], tags=tags or [], occurred_on=on,
                     created_via=created_via, external_ref=external_ref, receipt_id=receipt_id)
    s.add(tx)
    await s.flush()
    await s.refresh(tx, ["category", "account"])
    await audit(s, user, "create", tx)
    return tx


async def add_transfer(s: AsyncSession, user: User, src: Account, dst: Account, amount: Decimal,
                       to_amount: Decimal | None = None, note: str = "",
                       on: date | None = None) -> tuple[Transaction, Transaction]:
    """Move money between own accounts. Not income or spending.

    `amount` is in src currency; `to_amount` (optional) is what arrived in dst currency,
    which records the real exchange rate you got."""
    on = on or local_today(user)
    group = str(uuid.uuid4())
    if to_amount is None:
        to_amount = await fx.convert(s, amount, src.currency, dst.currency, on)
    rate_src = await fx.get_rate(s, src.currency, user.base_currency, on)
    rate_dst = await fx.get_rate(s, dst.currency, user.base_currency, on)
    out = Transaction(user_id=user.id, account_id=src.id, type="transfer", amount=-amount,
                      currency=src.currency, account_amount=-amount,
                      base_amount=(-amount * rate_src).quantize(Decimal("0.0001")),
                      fx_rate_to_base=rate_src, note=note or f"→ {dst.name}", occurred_on=on,
                      transfer_group=group)
    inn = Transaction(user_id=user.id, account_id=dst.id, type="transfer", amount=to_amount,
                      currency=dst.currency, account_amount=to_amount,
                      base_amount=(to_amount * rate_dst).quantize(Decimal("0.0001")),
                      fx_rate_to_base=rate_dst, note=note or f"← {src.name}", occurred_on=on,
                      transfer_group=group)
    s.add_all([out, inn])
    await s.flush()
    for t in (out, inn):
        await s.refresh(t, ["category", "account"])
        await audit(s, user, "create", t)
    return out, inn


# ---------- personal debts ----------
# Personal debts are kept apart: each person is an account of type receivable (they owe you)
# or debt (you owe them) that is left out of net worth, totals and statements. Entries are
# either a plain record (type "debt", touches no other account) or, when the money really
# left/entered one of your accounts, a transfer with that account. Neither is spending or income.
DEBT_TYPES = {"lent": "receivable", "borrowed": "debt"}
DEBT_ACCOUNT_TYPES = set(DEBT_TYPES.values())


async def _debt_record(s: AsyncSession, user: User, debt: Account, amount: Decimal, note: str,
                       on: date) -> Transaction:
    """Signed amount on the debt account only (positive = more owed to you / less owed by you)."""
    rate = await fx.get_rate(s, debt.currency, user.base_currency, on)
    t = Transaction(user_id=user.id, account_id=debt.id, type="debt", amount=amount, currency=debt.currency,
                    account_amount=amount, base_amount=(amount * rate).quantize(Decimal("0.0001")),
                    fx_rate_to_base=rate, note=note, occurred_on=on, created_via="app")
    s.add(t)
    await s.flush()
    await s.refresh(t, ["category", "account"])
    await audit(s, user, "create", t)
    return t


async def open_debt(s: AsyncSession, user: User, direction: str, person: str, amount: Decimal,
                    money: Account | None = None, on: date | None = None, due: date | None = None,
                    note: str = "", currency: str | None = None) -> Account:
    """money=None: just a record. With an account: the money really moved (a transfer)."""
    type_ = DEBT_TYPES[direction]
    on = on or local_today(user)
    person = person.strip()[:50]
    acc = None
    for a in await accounts(s, user):
        if a.name.lower() == person.lower():
            acc = a
            break
    if acc is not None and acc.type != type_:  # same name, other kind of account
        acc = None
        person = f"{person} ({'owes me' if direction == 'lent' else 'I owe'})"
        for a in await accounts(s, user):
            if a.name.lower() == person.lower() and a.type == type_:
                acc = a
    if acc is None:
        cur = (money.currency if money else (currency or user.base_currency)).upper()
        acc = await add_account(s, user, person, type_, cur)
        acc.include_in_net_worth = False
    if due:
        acc.due_on = due
    if note:
        acc.note = note[:200]
    label = f"Lent to {acc.name}" if direction == "lent" else f"Borrowed from {acc.name}"
    if money is None:
        await _debt_record(s, user, acc, amount if direction == "lent" else -amount, label, on)
    elif direction == "lent":
        await add_transfer(s, user, money, acc, amount, note=label, on=on)
    else:
        await add_transfer(s, user, acc, money, amount, note=label, on=on)
    return acc


async def repay_debt(s: AsyncSession, user: User, debt: Account, amount: Decimal,
                     money: Account | None = None, on: date | None = None) -> Decimal:
    """Record money paid back (in the debt's currency). Returns what is still owed; at 0 the
    debt is archived (closed) but its history stays."""
    on = on or local_today(user)
    left = abs(await account_balance(s, debt))
    if amount > left:
        raise ValueError(f"Only {fmt(left, debt.currency)} is left to pay")
    owes_me = debt.type == "receivable"
    label = f"{debt.name} paid back" if owes_me else f"Paid back {debt.name}"
    if money is None:
        await _debt_record(s, user, debt, -amount if owes_me else amount, label, on)
    elif owes_me:
        to_amount = amount if money.currency == debt.currency else None
        await add_transfer(s, user, debt, money, amount, to_amount=to_amount, note=label, on=on)
    else:
        src = amount if money.currency == debt.currency else \
            await fx.convert(s, amount, debt.currency, money.currency, on)
        await add_transfer(s, user, money, debt, src, to_amount=amount, note=label, on=on)
    left -= amount
    await sync_debt_state(s, user, debt)
    return left


async def sync_debt_state(s: AsyncSession, user: User, debt: Account):
    """Paid off -> archived; owing again after an edit or a deleted return -> back in the list."""
    await s.flush()
    bal = await account_balance(s, debt)
    if bal == 0 and not debt.archived:
        debt.archived = True
        await audit(s, user, "debt_closed", detail=debt.name)
    elif bal != 0 and debt.archived:
        debt.archived = False


async def debt_entries(s: AsyncSession, debt: Account) -> list[Transaction]:
    return list((await s.execute(select(Transaction).where(Transaction.account_id == debt.id)
                                 .order_by(Transaction.occurred_on, Transaction.id))).unique().scalars())


async def edit_debt_entry(s: AsyncSession, user: User, debt: Account, t: Transaction,
                          amount: Decimal | None = None, on: date | None = None) -> str:
    """Change the amount (in the debt's currency, positive) or date of one debt entry."""
    peer = None
    if t.transfer_group:
        peer = (await s.execute(select(Transaction).where(Transaction.transfer_group == t.transfer_group,
                                                          Transaction.id != t.id))).unique().scalar_one_or_none()
    old = f"{fmt(abs(t.account_amount), debt.currency)} on {t.occurred_on}"
    await audit(s, user, "update", t, detail=f"was {old}")
    if on is not None:
        for x in filter(None, (t, peer)):
            x.occurred_on = on
    if amount is not None:
        sign = 1 if t.account_amount >= 0 else -1
        new = sign * amount
        t.amount = t.account_amount = new
        rate = await fx.get_rate(s, debt.currency, user.base_currency, t.occurred_on)
        t.fx_rate_to_base = rate
        t.base_amount = (new * rate).quantize(Decimal("0.0001"))
        if peer is not None:
            other = -new if peer.currency == debt.currency else \
                await fx.convert(s, -new, debt.currency, peer.currency, t.occurred_on)
            prate = await fx.get_rate(s, peer.currency, user.base_currency, t.occurred_on)
            peer.amount = peer.account_amount = other
            peer.fx_rate_to_base = prate
            peer.base_amount = (other * prate).quantize(Decimal("0.0001"))
    await sync_debt_state(s, user, debt)
    return old


async def delete_account(s: AsyncSession, user: User, acc: Account) -> int:
    """Delete an (archived) account for good: its entries go too, including both sides of
    transfers with other accounts. Every deleted entry is kept in the audit log."""
    from .models import AssetValuation
    n = 0
    for t in await debt_entries(s, acc):
        if await s.get(Transaction, t.id) is not None and await delete_transaction(s, user, t.id):
            n += 1
    await s.execute(delete(AssetValuation).where(AssetValuation.account_id == acc.id))
    await audit(s, user, "account_deleted", detail=f"{acc.name}: {n} entries")
    if user.default_account_id == acc.id:
        user.default_account_id = None
    await s.delete(acc)
    await s.flush()
    return n


async def delete_debt(s: AsyncSession, user: User, debt: Account) -> int:
    """Remove a debt entered by mistake: all its entries (and the matching account moves) are
    deleted, each one logged in the audit trail, then the debt itself."""
    n = 0
    for t in await debt_entries(s, debt):
        if await s.get(Transaction, t.id) is not None and await delete_transaction(s, user, t.id):
            n += 1
    await audit(s, user, "debt_deleted", detail=f"{debt.name}: {n} entries")
    if user.default_account_id == debt.id:
        user.default_account_id = None
    await s.delete(debt)
    await s.flush()
    return n


async def delete_transaction(s: AsyncSession, user: User, tx_id: int) -> bool:
    tx = await s.get(Transaction, tx_id)
    if not tx or tx.user_id != user.id:
        return False
    if tx.transfer_group:
        group = (await s.execute(select(Transaction).where(
            Transaction.transfer_group == tx.transfer_group))).unique().scalars().all()
        for g in group:
            await audit(s, user, "delete", g)
        await s.execute(delete(Transaction).where(Transaction.transfer_group == tx.transfer_group))
    else:
        await audit(s, user, "delete", tx)
        await s.delete(tx)
    await s.flush()
    return True


async def last_transaction(s: AsyncSession, user: User) -> Transaction | None:
    return (await s.execute(select(Transaction).where(Transaction.user_id == user.id)
                            .order_by(Transaction.id.desc()).limit(1))).unique().scalar_one_or_none()


async def history(s: AsyncSession, user: User, limit=20) -> list[Transaction]:
    return list((await s.execute(select(Transaction).where(Transaction.user_id == user.id)
                                 .order_by(Transaction.occurred_on.desc(), Transaction.id.desc())
                                 .limit(limit))).unique().scalars())


async def find_duplicate(s: AsyncSession, user: User, amount: Decimal, currency: str, on: date,
                         days: int = 2) -> Transaction | None:
    """An expense with the same absolute amount/currency within ±days (receipt vs bank alert)."""
    q = select(Transaction).where(
        Transaction.user_id == user.id, Transaction.type == "expense",
        Transaction.currency == currency.upper(), Transaction.amount == -abs(amount),
        Transaction.occurred_on.between(on - timedelta(days=days), on + timedelta(days=days)))
    return (await s.execute(q.limit(1))).unique().scalar_one_or_none()


async def update_transaction(s: AsyncSession, user: User, t: Transaction, *, kind: str | None = None,
                             amount: Decimal | None = None, currency: str | None = None,
                             account: Account | None = None, category: Category | None = None,
                             on: date | None = None, note: str | None = None) -> str:
    """Edit an entry. The full old version goes to the audit log first. Returns what changed.

    Income/expense: anything can change (amounts are re-converted at that day's rate).
    Transfers: only the date and note (on both sides); delete and re-add to change amounts."""
    changes = []
    if t.type == "transfer":
        legs = (await s.execute(select(Transaction).where(
            Transaction.transfer_group == t.transfer_group))).unique().scalars().all()
        if on and on != t.occurred_on:
            changes.append(f"date {t.occurred_on} -> {on}")
        if note is not None and note != t.note:
            changes.append("note")
        if changes:
            for leg in legs:
                await audit(s, user, "update", leg, "; ".join(changes))
                leg.occurred_on = on or leg.occurred_on
                if note is not None:
                    leg.note = note[:255]
        await s.flush()
        return "; ".join(changes)

    new_kind = kind if kind in ("income", "expense") and t.type in ("income", "expense") else t.type
    if t.type == "adjustment":  # corrections keep their sign
        old_abs = t.amount
        new_amount = amount if amount is not None else t.amount
    else:
        old_abs = abs(t.amount)
        new_amount = abs(amount) if amount is not None else old_abs
    new_currency = (currency or t.currency).upper()
    new_account = account or t.account
    new_on = on or t.occurred_on
    if new_kind != t.type:
        changes.append(f"type {t.type} -> {new_kind}")
    if new_amount != old_abs or new_currency != t.currency:
        changes.append(f"amount {fmt(old_abs, t.currency)} -> {fmt(new_amount, new_currency)}")
    if new_account.id != t.account_id:
        changes.append(f"account {t.account.name} -> {new_account.name}")
    if new_on != t.occurred_on:
        changes.append(f"date {t.occurred_on} -> {new_on}")
    if new_kind != t.type and (category is None or category.kind != new_kind):
        category = await category_by_name(s, user, FALLBACK[new_kind], new_kind)
    if category is not None and category.id != t.category_id:
        changes.append(f"category {t.category.name if t.category else '-'} -> {category.name}")
    if note is not None and note.strip() != t.note:
        changes.append("note")
    if not changes:
        return ""
    await audit(s, user, "update", t, "; ".join(changes))

    if (new_kind, new_amount, new_currency, new_account.id, new_on) != (
            t.type, old_abs, t.currency, t.account_id, t.occurred_on):
        signed = -new_amount if new_kind == "expense" else new_amount
        t.account_amount = await fx.convert(s, signed, new_currency, new_account.currency, new_on)
        rate = await fx.get_rate(s, new_currency, user.base_currency, new_on)
        t.fx_rate_to_base = rate
        t.base_amount = (signed * rate).quantize(Decimal("0.0001"))
        t.type, t.amount, t.currency, t.account_id, t.occurred_on = (
            new_kind, signed, new_currency, new_account.id, new_on)
    if note is not None:
        t.note = note.strip()[:255]
    if category is not None and category.id != t.category_id:
        t.category_id = category.id
        if t.merchant or t.note:
            await learn_rule(s, user, t.merchant or t.note, category, source="user")
    await s.flush()
    await s.refresh(t, ["category", "account"])
    return "; ".join(changes)


async def reconcile(s: AsyncSession, user: User, acc: Account, actual: Decimal,
                    on: date | None = None) -> Transaction | None:
    """Make the recorded balance match reality (cash counted, bank app balance, amount owed).

    Valued accounts (investments, property...) get a new valuation; others get a 'Balance
    correction' adjustment for the difference. Adjustments are not income or spending, so
    reports stay honest, and the statement shows them under corrections."""
    if acc.type in VALUED_TYPES:
        await set_valuation(s, acc, actual, on or local_today(user))
        return None
    if acc.type in LIABILITY_TYPES:
        actual = -abs(actual)  # debts are negative balances
    diff = actual - await account_balance(s, acc)
    if diff == 0:
        return None
    return await add_transaction(s, user, acc, "adjustment", diff, acc.currency, None,
                                 note="Balance correction", on=on, created_via="app")


# ---------- balances & net worth ----------

async def account_balance(s: AsyncSession, acc: Account) -> Decimal:
    if acc.type in VALUED_TYPES:
        val = (await s.execute(select(AssetValuation).where(AssetValuation.account_id == acc.id)
                               .order_by(AssetValuation.valued_on.desc(), AssetValuation.id.desc())
                               .limit(1))).scalar_one_or_none()
        if val:
            return val.value
    total = (await s.execute(select(func.coalesce(func.sum(Transaction.account_amount), 0))
                             .where(Transaction.account_id == acc.id))).scalar_one()
    return Decimal(str(acc.opening_balance)) + Decimal(str(total))


async def set_valuation(s: AsyncSession, acc: Account, value: Decimal, on: date | None = None):
    s.add(AssetValuation(account_id=acc.id, value=value, valued_on=on or date.today()))
    await s.flush()


@dataclass
class NetWorth:
    assets: Decimal
    liabilities: Decimal
    lines: list[tuple[Account, Decimal, Decimal]]  # account, balance (own ccy), value in base

    @property
    def net(self) -> Decimal:
        return self.assets - self.liabilities


async def net_worth(s: AsyncSession, user: User) -> NetWorth:
    assets = liabilities = Decimal(0)
    lines = []
    for acc in await accounts(s, user):
        bal = await account_balance(s, acc)
        try:
            in_base = await fx.convert(s, bal, acc.currency, user.base_currency)
        except fx.FxUnavailable:
            in_base = Decimal(0)
        lines.append((acc, bal, in_base))
        if not acc.include_in_net_worth or acc.type in DEBT_ACCOUNT_TYPES:
            continue
        if acc.type in LIABILITY_TYPES:
            liabilities += -in_base if in_base < 0 else in_base
        else:
            assets += in_base
    return NetWorth(assets, liabilities, lines)


# ---------- period statistics ----------

def period_bounds(kind: str, today: date) -> tuple[date, date, date, date]:
    """(start, end, prev_start, prev_end) for 'day' | 'week' | 'month' | 'year' ending at today."""
    if kind in ("day", "daily"):
        return today, today, today - timedelta(days=1), today - timedelta(days=1)
    if kind in ("week", "weekly"):
        start = today - timedelta(days=today.weekday())
        return start, today, start - timedelta(days=7), start - timedelta(days=1)
    if kind in ("month", "monthly"):
        start = today.replace(day=1)
        prev_end = start - timedelta(days=1)
        return start, today, prev_end.replace(day=1), prev_end
    if kind == "year":
        start = today.replace(month=1, day=1)
        return start, today, start.replace(year=start.year - 1), start - timedelta(days=1)
    raise ValueError(kind)


async def stats(s: AsyncSession, user: User, start: date, end: date) -> dict:
    """Aggregates for [start, end] in base currency (transfers excluded)."""
    rows = (await s.execute(select(Transaction).where(
        Transaction.user_id == user.id, Transaction.occurred_on.between(start, end),
        Transaction.type.in_(("income", "expense"))))).unique().scalars().all()
    income = expense = Decimal(0)
    by_cat: dict[str, Decimal] = defaultdict(Decimal)
    inc_by_cat: dict[str, Decimal] = defaultdict(Decimal)
    daily: dict[date, Decimal] = defaultdict(Decimal)
    merchants: dict[str, Decimal] = defaultdict(Decimal)
    top = []
    for t in rows:
        amt = Decimal(str(t.base_amount))
        name = t.category.name if t.category else "Uncategorized"
        if t.type == "income":
            income += amt
            inc_by_cat[name] += amt
        else:
            expense += -amt
            by_cat[name] += -amt
            daily[t.occurred_on] += -amt
            if t.merchant or t.note:
                merchants[(t.merchant or t.note).lower()[:40]] += -amt
            top.append((-amt, t))
    top.sort(key=lambda x: x[0], reverse=True)
    days = (end - start).days + 1
    return {
        "start": start, "end": end, "days": days, "currency": user.base_currency,
        "income": income, "expense": expense, "net": income - expense,
        "savings_rate": (income - expense) / income * 100 if income else None,
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "income_by_category": dict(sorted(inc_by_cat.items(), key=lambda x: -x[1])),
        "daily": {start + timedelta(days=i): daily.get(start + timedelta(days=i), Decimal(0))
                  for i in range(days)},
        "top": [(a, t.merchant or t.note or (t.category.name if t.category else ""), t.occurred_on)
                for a, t in top[:5]],
        "merchants": dict(sorted(merchants.items(), key=lambda x: -x[1])[:10]),
        "count": len(rows),
    }


def stats_for_ai(cur: dict, prev: dict, extra: dict | None = None) -> dict:
    """Compact, JSON-safe aggregates sent to the AI (no raw transaction history)."""
    f = lambda d: {k: float(round(v, 2)) for k, v in d.items()}  # noqa: E731
    return {
        "currency": cur["currency"],
        "period": f"{cur['start']} to {cur['end']}",
        "income": float(cur["income"]), "expenses": float(cur["expense"]), "net": float(cur["net"]),
        "savings_rate_pct": float(round(cur["savings_rate"], 1)) if cur["savings_rate"] is not None else None,
        "expenses_by_category": f(cur["by_category"]),
        "income_by_category": f(cur["income_by_category"]),
        "top_merchants": f(cur["merchants"]),
        "largest_expenses": [{"amount": float(a), "what": w, "date": str(d)} for a, w, d in cur["top"]],
        "previous_period": {
            "period": f"{prev['start']} to {prev['end']}",
            "income": float(prev["income"]), "expenses": float(prev["expense"]),
            "expenses_by_category": f(prev["by_category"]),
        },
        **(extra or {}),
    }


async def export_csv(s: AsyncSession, user: User, start: date | None = None, end: date | None = None) -> bytes:
    q = select(Transaction).where(Transaction.user_id == user.id)
    if start:
        q = q.where(Transaction.occurred_on >= start)
    if end:
        q = q.where(Transaction.occurred_on <= end)
    rows = (await s.execute(q.order_by(Transaction.occurred_on, Transaction.id))).unique().scalars()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "date", "type", "account", "amount", "currency", "account_amount",
                f"amount_{user.base_currency}", "category", "merchant", "note", "tags", "source"])
    for t in rows:
        w.writerow([t.id, t.occurred_on, t.type, t.account.name, t.amount, t.currency, t.account_amount,
                    t.base_amount, t.category.name if t.category else "", t.merchant, t.note,
                    " ".join(t.tags or []), t.created_via])
    return buf.getvalue().encode("utf-8-sig")


# ---------- accountability: audit log, snapshots, statements, backups ----------

def tx_dict(t: Transaction) -> dict:
    return {"id": t.id, "date": str(t.occurred_on), "type": t.type, "account_id": t.account_id,
            "amount": str(t.amount), "currency": t.currency, "account_amount": str(t.account_amount),
            "base_amount": str(t.base_amount), "category_id": t.category_id, "merchant": t.merchant,
            "note": t.note, "tags": t.tags, "source": t.created_via, "transfer_group": t.transfer_group}


async def audit(s: AsyncSession, user: User, action: str, t: Transaction | None = None, detail: str = ""):
    from .models import AuditLog
    s.add(AuditLog(user_id=user.id, transaction_id=t.id if t else None, action=action,
                   detail=detail[:255], data=tx_dict(t) if t else {}))
    await s.flush()


async def take_snapshot(s: AsyncSession, user: User, day: date | None = None):
    """Freeze today's balances and net worth (re-taking the same day replaces it)."""
    from .models import Snapshot, SnapshotLine
    day = day or local_today(user)
    old = (await s.execute(select(Snapshot).where(Snapshot.user_id == user.id, Snapshot.day == day))
           ).scalar_one_or_none()
    if old:
        await s.execute(delete(SnapshotLine).where(SnapshotLine.snapshot_id == old.id))
        await s.delete(old)
        await s.flush()
    nw = await net_worth(s, user)
    snap = Snapshot(user_id=user.id, day=day, base_currency=user.base_currency, assets=nw.assets,
                    liabilities=nw.liabilities, net_worth=nw.net)
    s.add(snap)
    await s.flush()
    for acc, bal, in_base in nw.lines:
        if acc.type in DEBT_ACCOUNT_TYPES:  # personal debts are kept apart from net worth
            continue
        rate = (in_base / bal) if bal else Decimal(1)
        s.add(SnapshotLine(snapshot_id=snap.id, account_id=acc.id, account_name=acc.name,
                           account_type=acc.type, currency=acc.currency, balance=bal, value_base=in_base,
                           fx_rate=rate))
    await s.flush()
    return snap


async def snapshot_on_or_before(s: AsyncSession, user: User, day: date):
    from .models import Snapshot
    return (await s.execute(select(Snapshot).where(Snapshot.user_id == user.id, Snapshot.day <= day)
                            .order_by(Snapshot.day.desc()).limit(1))).scalar_one_or_none()


async def snapshot_history(s: AsyncSession, user: User, start: date | None = None) -> list:
    from .models import Snapshot
    q = select(Snapshot).where(Snapshot.user_id == user.id)
    if start:
        q = q.where(Snapshot.day >= start)
    return list((await s.execute(q.order_by(Snapshot.day))).scalars())


async def snapshot_lines(s: AsyncSession, snap) -> list:
    from .models import SnapshotLine
    return list((await s.execute(select(SnapshotLine).where(SnapshotLine.snapshot_id == snap.id)
                                 .order_by(SnapshotLine.account_id))).scalars())


@dataclass
class Statement:
    start: date
    end: date
    currency: str
    opening: object | None   # Snapshot
    closing: object          # Snapshot
    income: Decimal
    expense: Decimal
    lines: list[tuple[str, str, Decimal, Decimal]]  # account, currency, opening value, closing value

    @property
    def change(self) -> Decimal:
        return self.closing.net_worth - (self.opening.net_worth if self.opening else Decimal(0))

    @property
    def saved(self) -> Decimal:
        return self.income - self.expense

    @property
    def other(self) -> Decimal:
        """Change not explained by income minus spending: market value, FX, corrections."""
        return self.change - self.saved if self.opening else Decimal(0)


async def statement(s: AsyncSession, user: User, start: date, end: date) -> Statement:
    """Balance statement for a period: opening vs closing net worth and what moved it."""
    opening = await snapshot_on_or_before(s, user, start - timedelta(days=1))
    closing = await snapshot_on_or_before(s, user, end)
    today = local_today(user)
    if closing is None or (end >= today and closing.day < today):
        closing = await take_snapshot(s, user)
    st = await stats(s, user, start, end)
    open_lines = {l.account_id: l for l in await snapshot_lines(s, opening)} if opening else {}
    rows = []
    for l in await snapshot_lines(s, closing):
        o = open_lines.pop(l.account_id, None)
        rows.append((l.account_name, l.currency, o.value_base if o else Decimal(0), l.value_base))
    for o in open_lines.values():
        rows.append((o.account_name, o.currency, o.value_base, Decimal(0)))
    return Statement(start, end, user.base_currency, opening, closing, st["income"], st["expense"], rows)


def statement_csv(stmt: Statement) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Statement {stmt.start} to {stmt.end}", f"currency {stmt.currency}"])
    w.writerow(["account", "account currency", f"opening ({stmt.start})", f"closing ({stmt.end})", "change"])
    for name, cur, o, c in stmt.lines:
        w.writerow([name, cur, f"{o:.2f}", f"{c:.2f}", f"{c - o:.2f}"])
    op = stmt.opening.net_worth if stmt.opening else Decimal(0)
    w.writerow(["NET WORTH", stmt.currency, f"{op:.2f}", f"{stmt.closing.net_worth:.2f}", f"{stmt.change:.2f}"])
    w.writerow([])
    w.writerow(["income", f"{stmt.income:.2f}"])
    w.writerow(["spending", f"{stmt.expense:.2f}"])
    w.writerow(["saved (income - spending)", f"{stmt.saved:.2f}"])
    w.writerow(["value / FX / corrections", f"{stmt.other:.2f}"])
    return buf.getvalue().encode("utf-8-sig")


async def backup_zip(s: AsyncSession, user: User) -> bytes:
    """Complete copy of the user's data: CSV files (Excel) + one JSON file (machine-readable)."""
    import json
    import zipfile
    from .models import AuditLog, SnapshotLine
    out = io.BytesIO()
    accs = await accounts(s, user, include_archived=True)
    cats = await categories(s, user)
    snaps = await snapshot_history(s, user)
    audit_rows = list((await s.execute(select(AuditLog).where(AuditLog.user_id == user.id)
                                       .order_by(AuditLog.id))).scalars())
    txs = list((await s.execute(select(Transaction).where(Transaction.user_id == user.id)
                                .order_by(Transaction.occurred_on, Transaction.id))).unique().scalars())

    def to_csv(header, rows):
        b = io.StringIO()
        w = csv.writer(b)
        w.writerow(header)
        w.writerows(rows)
        return b.getvalue().encode("utf-8-sig")

    snap_lines = []
    for sn in snaps:
        for l in (await s.execute(select(SnapshotLine).where(SnapshotLine.snapshot_id == sn.id))).scalars():
            snap_lines.append([sn.day, l.account_name, l.account_type, l.currency, l.balance, l.value_base])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("transactions.csv", await export_csv(s, user))
        z.writestr("accounts.csv", to_csv(["id", "name", "type", "currency", "opening_balance", "archived"],
                                          [[a.id, a.name, a.type, a.currency, a.opening_balance, a.archived] for a in accs]))
        z.writestr("net_worth_history.csv", to_csv(
            ["date", "currency", "assets", "liabilities", "net_worth"],
            [[x.day, x.base_currency, x.assets, x.liabilities, x.net_worth] for x in snaps]))
        z.writestr("account_balances_history.csv", to_csv(
            ["date", "account", "type", "currency", "balance", f"value_{user.base_currency}"], snap_lines))
        z.writestr("edit_history.csv", to_csv(["at", "action", "transaction_id", "detail", "record"],
                                              [[a.at, a.action, a.transaction_id, a.detail, json.dumps(a.data, ensure_ascii=False)]
                                               for a in audit_rows]))
        z.writestr("backup.json", json.dumps({
            "exported_at": str(local_today(user)), "base_currency": user.base_currency, "timezone": user.timezone,
            "accounts": [{"id": a.id, "name": a.name, "type": a.type, "currency": a.currency,
                          "opening_balance": str(a.opening_balance), "archived": a.archived} for a in accs],
            "categories": [{"id": c.id, "name": c.name, "kind": c.kind} for c in cats],
            "transactions": [tx_dict(t) for t in txs],
            "snapshots": [{"date": str(x.day), "net_worth": str(x.net_worth), "assets": str(x.assets),
                           "liabilities": str(x.liabilities)} for x in snaps],
        }, ensure_ascii=False, indent=1, default=str))
    return out.getvalue()
