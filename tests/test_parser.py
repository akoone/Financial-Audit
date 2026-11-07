from datetime import date
from decimal import Decimal

from app.parser import parse_amount, parse_entry

TODAY = date(2026, 11, 7)
ACC = {"Cash", "Bank", "Card"}


def p(t):
    return parse_entry(t, ACC, TODAY)


def test_simple_expense():
    e = p("35 coffee")
    assert e.kind == "expense" and e.amount == Decimal(35) and e.text == "coffee" and e.currency is None


def test_currency_and_account():
    e = p("12.5 usd taxi card")
    assert e.currency == "USD" and e.amount == Decimal("12.5") and e.account == "Card" and e.text == "taxi"


def test_symbol_and_suffix():
    assert p("$20 lunch").currency == "USD"
    assert p("15000krw bus").currency == "KRW"
    assert p("₩8000 kimbap").amount == Decimal(8000)


def test_income():
    assert p("+3000 salary").kind == "income"
    assert p("3000 salary").kind == "income"


def test_transfer_with_received_amount():
    e = p("1000 bank>cash 950")
    assert e.kind == "transfer" and e.account == "Bank" and e.to_account == "Cash" and e.to_amount == Decimal(950)


def test_date_and_tags():
    e = p("45 groceries yesterday #trip")
    assert e.on == date(2026, 11, 6) and e.tags == ["trip"] and e.text == "groceries"
    assert p("10 bread 03.11").on == date(2026, 11, 3)


def test_not_an_entry():
    assert p("hello there") is None
    assert p("") is None


def test_amount_formats():
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("12,5") == Decimal("12.5")
    assert parse_amount("1,000") == Decimal("1000")
    assert parse_amount("0") is None


def test_natural_sentences_and_shorthand():
    e = p("Spent 50k on lunch")
    assert e.kind == "expense" and e.amount == Decimal(50000) and e.text == "lunch"
    e = p("taxi 25000 cash")
    assert e.amount == Decimal(25000) and e.account == "Cash" and e.text == "taxi"
    e = p("got 3m salary")
    assert e.kind == "income" and e.amount == Decimal(3000000) and e.text == "salary"
    assert p("120 ming bozor").amount == Decimal(120000)
    assert p("1.5m rent").amount == Decimal(1500000)
    assert p("paid $12 for netflix").currency == "USD"


def test_neon_url_normalized():
    from app.db import normalize_url
    u = normalize_url("postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
    assert u == "postgresql+asyncpg://u:p@ep-x.eu-central-1.aws.neon.tech/neondb?ssl=require"
    assert normalize_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_url("sqlite+aiosqlite:///./data/x.db") == "sqlite+aiosqlite:///./data/x.db"
