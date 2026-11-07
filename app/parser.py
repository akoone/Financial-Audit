"""Quick-entry parser: turns '12.5 usd taxi card #trip yesterday' into a structured entry.

Grammar (order of words after the amount is free):
  [+]<amount>[currency] words...         expense, or income with '+' / income keyword
  <amount> <from>><to> [received]        transfer between accounts (received = amount in target currency)
  today | yesterday | YYYY-MM-DD | DD.MM  date
  #tag                                   tag
  <account name>                         account to use
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₩": "KRW", "₺": "TRY", "¥": "JPY",
                    "₽": "RUB", "₹": "INR", "₸": "KZT"}
KNOWN_CURRENCIES = {
    "USD", "EUR", "GBP", "KRW", "TRY", "JPY", "CNY", "RUB", "INR", "KZT", "UZS", "AED", "SAR",
    "CHF", "CAD", "AUD", "SGD", "HKD", "PLN", "SEK", "NOK", "DKK", "CZK", "HUF", "UAH", "GEL",
    "AZN", "KGS", "TJS", "BRL", "MXN", "IDR", "MYR", "THB", "VND", "PHP", "EGP", "PKR", "BDT",
}
INCOME_WORDS = {"salary", "income", "bonus", "freelance", "dividend", "interest", "refund",
                "received", "wage", "cashback", "maas", "maaş", "월급", "earned", "got", "oylik",
                "daromad", "зарплата"}
FILLER_WORDS = {"spent", "spend", "paid", "pay", "bought", "buy", "for", "on", "at", "the", "a", "an",
                "i", "my", "to", "of", "in", "and", "was", "it", "earned", "got"}
MULTIPLIERS = {"k": 1000, "к": 1000, "ming": 1000, "тыс": 1000, "m": 1_000_000, "mln": 1_000_000,
               "млн": 1_000_000}

_AMOUNT_RE = re.compile(r"^([+-]?)([$€£₩₺¥₽₹₸]?)(\d[\d\s,.]*)([a-zA-Zа-яА-Я$€£₩₺¥₽₹₸]{0,4})$")


@dataclass
class ParsedEntry:
    kind: str  # expense | income | transfer
    amount: Decimal
    currency: str | None = None
    words: list[str] = field(default_factory=list)  # description words (for category + note)
    account: str | None = None
    to_account: str | None = None
    to_amount: Decimal | None = None
    tags: list[str] = field(default_factory=list)
    on: date | None = None
    free_form: bool = False  # amount was not the first word (natural sentence)

    @property
    def text(self) -> str:
        return " ".join(self.words)


def parse_amount(raw: str) -> Decimal | None:
    s = raw.replace(" ", "")
    if not s or not s[0].isdigit():
        return None
    # "1.234,56" / "1,234.56" / "12,5" / "1,000"
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 and head else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _parse_date(token: str, today: date) -> date | None:
    t = token.lower()
    if t in ("today", "bugün", "오늘"):
        return today
    if t in ("yesterday", "dün", "어제"):
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})", t)
    if m:
        try:
            d = date(today.year, int(m[2]), int(m[1]))
        except ValueError:
            return None
        return d if d <= today else date(today.year - 1, d.month, d.day)
    return None


def _currency(token: str) -> str | None:
    if token in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[token]
    up = token.upper()
    return up if up in KNOWN_CURRENCIES else None


def parse_entry(text: str, account_names: set[str] | None = None,
                today: date | None = None) -> ParsedEntry | None:
    """Return a ParsedEntry, or None if the text doesn't start with an amount."""
    today = today or date.today()
    accounts = {a.lower(): a for a in (account_names or set())}
    tokens = text.strip().split()
    if not tokens:
        return None

    m = _AMOUNT_RE.match(tokens[0])
    free_form = False
    if not m:
        # Natural sentence like "spent 50k on lunch": find the amount anywhere
        for i, tok in enumerate(tokens[1:], 1):
            if ">" in tok or _parse_date(tok, today):
                continue
            if (m := _AMOUNT_RE.match(tok)) and parse_amount(m.group(3)):
                tokens = [tok] + tokens[:i] + tokens[i + 1:]
                free_form = True
                break
        else:
            return None
    sign, sym, num, suffix = m.groups()
    amount = parse_amount(num)
    if amount is None:
        return None
    currency = CURRENCY_SYMBOLS.get(sym) if sym else None
    if suffix:
        if suffix.lower() in MULTIPLIERS:
            amount *= MULTIPLIERS[suffix.lower()]
        elif _currency(suffix):
            currency = _currency(suffix)
        else:
            return None
    if len(tokens) > 1 and tokens[1].lower() in MULTIPLIERS:  # "50 ming", "2 mln"
        amount *= MULTIPLIERS[tokens[1].lower()]
        tokens = [tokens[0]] + tokens[2:]

    entry = ParsedEntry(kind="income" if sign == "+" else "expense", amount=amount, currency=currency,
                        free_form=free_form)
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        low = tok.lower()
        if ">" in tok and tok.count(">") == 1:
            src, dst = tok.split(">")
            entry.kind = "transfer"
            entry.account = accounts.get(src.lower(), src)
            entry.to_account = accounts.get(dst.lower(), dst)
            if i + 1 < len(rest) and (nxt := parse_amount(rest[i + 1])) is not None:
                entry.to_amount = nxt
                i += 1
        elif tok.startswith("#") and len(tok) > 1:
            entry.tags.append(tok[1:].lower())
        elif (d := _parse_date(tok, today)) is not None:
            entry.on = d
        elif entry.currency is None and _currency(tok) and not entry.words:
            entry.currency = _currency(tok)
        elif low in accounts and entry.account is None:
            entry.account = accounts[low]
        else:
            entry.words.append(tok)
        i += 1

    if entry.kind == "expense" and any(w.lower() in INCOME_WORDS for w in entry.words):
        entry.kind = "income"
    if free_form or any(w.lower() in FILLER_WORDS for w in entry.words[:1]):
        kept = [w for w in entry.words if w.lower().strip(",.!") not in FILLER_WORDS]
        entry.words = kept or entry.words
    return entry
