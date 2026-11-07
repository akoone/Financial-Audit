"""Bank data import: forwarded alerts (SMS/push/email) and statement files (CSV, PDF)."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import ai, services as sv
from .models import Account, Receipt, Transaction, User
from .parser import parse_amount

DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y.%m.%d", "%Y/%m/%d",
                "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"]


@dataclass
class ImportResult:
    added: list[Transaction]
    skipped: int = 0
    merged: int = 0


def ext_ref(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:40]


def _to_date(raw: str | None) -> date | None:
    if not raw:
        return None
    raw = raw.strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(raw, f).date()
        except ValueError:
            continue
    return None


def _signed_amount(raw: str) -> Decimal | None:
    raw = raw.strip().replace("−", "-")
    neg = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    num = re.sub(r"[^\d.,]", "", raw)
    val = parse_amount(num) if num else None
    if val is None:
        return None
    return -val if neg else val


async def _account_for_hint(s: AsyncSession, user: User, hint: str) -> Account | None:
    hint = (hint or "").lower()
    if hint:
        for acc in await sv.accounts(s, user):
            if acc.name.lower() in hint or (len(hint) >= 4 and hint[-4:] in acc.name.lower()):
                return acc
    return await sv.find_account(s, user, None)


async def save_bank_tx(s: AsyncSession, user: User, tx: ai.BankTx, via: str,
                       account: Account | None = None, raw: str = "") -> tuple[Transaction | None, str]:
    """Store an AI-parsed bank transaction with de-duplication.

    Returns (transaction, status) with status in {'added', 'duplicate', 'merged'}."""
    on = _to_date(tx.date) or date.today()
    amount = Decimal(str(abs(tx.amount)))
    currency = (tx.currency or user.base_currency).upper()
    account = account or await _account_for_hint(s, user, tx.card_or_account_hint)
    ref = ext_ref(user.id, on, amount, currency, tx.merchant.lower(), raw[:200])

    if (await s.execute(select(Transaction.id).where(
            Transaction.user_id == user.id, Transaction.external_ref == ref))).first():
        return None, "duplicate"
    kind = "income" if tx.type == "income" else "expense"
    if kind == "expense":
        # A receipt split across categories: match on the receipt total
        rec_tx = (await s.execute(select(Transaction).join(Receipt, Transaction.receipt_id == Receipt.id).where(
            Receipt.user_id == user.id, Receipt.status == "confirmed", Receipt.total == amount,
            Receipt.currency == currency, Transaction.external_ref.is_(None),
            Transaction.occurred_on.between(on - timedelta(days=1), on + timedelta(days=1)))
            .order_by(Transaction.id))).unique().scalars().all()
        if rec_tx:
            for t in rec_tx:
                t.external_ref = ref if t is rec_tx[0] else f"{ref[:36]}-{t.id}"
            return rec_tx[0], "merged"
        dup = await sv.find_duplicate(s, user, amount, currency, on, days=1)
        if dup and dup.created_via in ("receipt", "text"):
            # Same purchase already logged by hand or from a receipt: keep it, link the bank ref.
            dup.external_ref = dup.external_ref or ref
            if not dup.merchant:
                dup.merchant = tx.merchant[:120]
            return dup, "merged"

    cat = await sv.category_by_name(s, user, tx.category, kind)
    if cat is None or cat.name in ("Other", "Other income"):
        cat, _ = await sv.guess_category(s, user, tx.merchant, kind)
    t = await sv.add_transaction(s, user, account, kind, amount, currency, cat, merchant=tx.merchant,
                                 on=on, created_via=via, external_ref=ref)
    return t, "added"


# ---------- statements ----------

_COLS = {
    "date": ["date", "transaction date", "posting date", "booking date", "tarih", "işlem tarihi",
             "거래일", "거래일시", "value date"],
    "desc": ["description", "details", "narrative", "merchant", "payee", "memo", "açıklama",
             "aciklama", "적요", "내용", "transaction", "name"],
    "amount": ["amount", "tutar", "금액", "transaction amount", "sum"],
    "debit": ["debit", "withdrawal", "withdrawals", "paid out", "borç", "출금", "money out", "out"],
    "credit": ["credit", "deposit", "deposits", "paid in", "alacak", "입금", "money in", "in"],
    "currency": ["currency", "ccy", "döviz", "통화"],
}


def _find_col(headers: list[str], key: str) -> int | None:
    norm = [h.strip().lower() for h in headers]
    for cand in _COLS[key]:
        if cand in norm:
            return norm.index(cand)
    for i, h in enumerate(norm):
        if any(c in h for c in _COLS[key] if len(c) > 3):
            return i
    return None


def parse_csv_statement(data: bytes) -> list[dict] | None:
    """Heuristic CSV reader. Returns rows {date, desc, amount(signed), currency} or None if unknown."""
    text = data.decode("utf-8-sig", errors="ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    for h_idx, headers in enumerate(rows[:15]):
        di, de = _find_col(headers, "date"), _find_col(headers, "desc")
        ai_, dr, cr = _find_col(headers, "amount"), _find_col(headers, "debit"), _find_col(headers, "credit")
        if di is None or (ai_ is None and dr is None and cr is None):
            continue
        cu = _find_col(headers, "currency")
        out = []
        for r in rows[h_idx + 1:]:
            if len(r) <= di:
                continue
            d = _to_date(r[di])
            if not d:
                continue
            amt = None
            if ai_ is not None and ai_ < len(r) and r[ai_].strip():
                amt = _signed_amount(r[ai_])
            else:
                deb = _signed_amount(r[dr]) if dr is not None and dr < len(r) and r[dr].strip() else None
                cre = _signed_amount(r[cr]) if cr is not None and cr < len(r) and r[cr].strip() else None
                if deb:
                    amt = -abs(deb)
                elif cre:
                    amt = abs(cre)
            if not amt:
                continue
            out.append({"date": d, "desc": r[de].strip() if de is not None and de < len(r) else "",
                        "amount": amt,
                        "currency": r[cu].strip().upper() if cu is not None and cu < len(r) else None})
        return out
    return None


async def import_statement(s: AsyncSession, user: User, account: Account, data: bytes,
                           filename: str, media_type: str) -> ImportResult:
    res = ImportResult(added=[])
    rows = parse_csv_statement(data) if filename.lower().endswith((".csv", ".txt")) else None
    if rows is None:
        if not ai.enabled():
            raise ai.AiError("This statement format needs AI (set ANTHROPIC_API_KEY) or a CSV file")
        cats = [c.name for c in await sv.categories(s, user)]
        parsed = await ai.parse_statement(data, media_type, cats, account.currency)
        rows = [{"date": _to_date(p.date) or date.today(), "desc": p.merchant,
                 "amount": Decimal(str(abs(p.amount))) * (1 if p.type == "income" else -1),
                 "currency": p.currency, "category": p.category} for p in parsed]

    for r in rows:
        kind = "income" if r["amount"] > 0 else "expense"
        cur = (r.get("currency") or account.currency).upper()
        amount = abs(r["amount"])
        ref = ext_ref(user.id, account.id, r["date"], r["amount"], cur, r["desc"].lower())
        if (await s.execute(select(Transaction.id).where(
                Transaction.user_id == user.id, Transaction.external_ref == ref))).first():
            res.skipped += 1
            continue
        if kind == "expense" and (dup := await sv.find_duplicate(s, user, amount, cur, r["date"], days=1)) \
                and dup.external_ref is None:
            dup.external_ref = ref
            res.merged += 1
            continue
        cat = await sv.category_by_name(s, user, r["category"], kind) if r.get("category") else None
        if cat is None:
            cat, _ = await sv.guess_category(s, user, r["desc"], kind)
        res.added.append(await sv.add_transaction(
            s, user, account, kind, amount, cur, cat, merchant=r["desc"][:120], on=r["date"],
            created_via="import", external_ref=ref))
    return res
