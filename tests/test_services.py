from datetime import date, timedelta
from decimal import Decimal

from app import importers, services as sv
from app.ai import BankTx
from app.reports import build_report


async def setup(s, cur="USD"):
    return await sv.create_user(s, 1, "Test", cur, "UTC")


async def test_balances_transfers_and_networth(s):
    u = await setup(s)
    cash = await sv.find_account(s, u, "Cash")
    bank = await sv.find_account(s, u, "Bank")
    await sv.add_transaction(s, u, bank, "income", Decimal(3000))
    cat, ok = await sv.guess_category(s, u, "coffee", "expense")
    assert ok and cat.name == "Eating out"
    await sv.add_transaction(s, u, cash, "expense", Decimal(5), category=cat)
    await sv.add_transfer(s, u, bank, cash, Decimal(200))
    assert await sv.account_balance(s, bank) == Decimal(2800)
    assert await sv.account_balance(s, cash) == Decimal(195)
    await sv.add_account(s, u, "Loan", "loan", "USD", Decimal(-1000))
    nw = await sv.net_worth(s, u)
    assert nw.assets == Decimal(2995) and nw.liabilities == Decimal(1000) and nw.net == Decimal(1995)
    st = await sv.stats(s, u, date.today(), date.today())
    assert st["income"] == 3000 and st["expense"] == 5  # transfer not counted


async def test_multicurrency(s):
    u = await setup(s)
    card = await sv.find_account(s, u, "Card")
    krw = await sv.add_account(s, u, "Korea", "bank", "KRW")
    t = await sv.add_transaction(s, u, card, "expense", Decimal(14000), "KRW")
    assert t.account_amount == Decimal("-10.0000") and t.base_amount == Decimal("-10.0000")
    await sv.add_transfer(s, u, card, krw, Decimal(100), Decimal(139000))
    assert await sv.account_balance(s, krw) == Decimal(139000)


async def test_learned_rule_wins(s):
    u = await setup(s)
    shopping = await sv.category_by_name(s, u, "Shopping", "expense")
    await sv.learn_rule(s, u, "coffee beans", shopping)
    cat, _ = await sv.guess_category(s, u, "coffee beans 1kg", "expense")
    assert cat.name == "Shopping"


async def test_bank_alert_dedup_and_merge(s):
    u = await setup(s)
    card = await sv.find_account(s, u, "Card")
    cat = await sv.category_by_name(s, u, "Groceries", "expense")
    await sv.add_transaction(s, u, card, "expense", Decimal("42.10"), category=cat, created_via="receipt")
    alert = BankTx(is_transaction=True, type="expense", amount=42.10, currency="USD", merchant="MIGROS",
                   date=str(date.today()), category="Groceries")
    t, status = await importers.save_bank_tx(s, u, alert, "alert", raw="card *1234 42.10 MIGROS")
    assert status == "merged"
    alert2 = BankTx(is_transaction=True, type="expense", amount=9.99, currency="USD", merchant="NETFLIX",
                    category="Subscriptions")
    t, status = await importers.save_bank_tx(s, u, alert2, "alert", raw="netflix 9.99")
    assert status == "added" and t.category.name == "Subscriptions"
    _, status = await importers.save_bank_tx(s, u, alert2, "alert", raw="netflix 9.99")
    assert status == "duplicate"


async def test_csv_statement_import(s):
    u = await setup(s)
    bank = await sv.find_account(s, u, "Bank")
    d = date.today()
    csv_data = (f"Date;Description;Amount\n{d:%d.%m.%Y};STARBUCKS;-4,50\n"
                f"{d:%d.%m.%Y};SALARY ACME;2.500,00\n{d:%d.%m.%Y};UBER TRIP;-12,30\n").encode()
    res = await importers.import_statement(s, u, bank, csv_data, "st.csv", "text/csv")
    assert len(res.added) == 3
    cats = {t.merchant: t.category.name for t in res.added}
    assert cats["STARBUCKS"] == "Eating out" and cats["UBER TRIP"] == "Transport"
    assert cats["SALARY ACME"] == "Salary"
    res2 = await importers.import_statement(s, u, bank, csv_data, "st.csv", "text/csv")
    assert len(res2.added) == 0 and res2.skipped == 3
    assert await sv.account_balance(s, bank) == Decimal("2483.20")


async def test_debit_credit_csv(s):
    rows = importers.parse_csv_statement(
        b"Booking Date,Details,Debit,Credit\n2026-11-01,Rent,800.00,\n2026-11-02,Refund,,15.00\n")
    assert [r["amount"] for r in rows] == [Decimal("-800.00"), Decimal("15.00")]


async def test_report_without_ai(s):
    u = await setup(s)
    card = await sv.find_account(s, u, "Card")
    for i, (amt, what) in enumerate([(12, "coffee"), (80, "groceries"), (30, "taxi")]):
        cat, _ = await sv.guess_category(s, u, what, "expense")
        await sv.add_transaction(s, u, card, "expense", Decimal(amt), category=cat, note=what,
                                 on=date.today())  # same day: always inside this week and month
    rep = await build_report(s, u, "week", with_ai=False)
    assert "Weekly report" in rep.text and "Groceries" in rep.text
    rep = await build_report(s, u, "month", with_ai=False)
    assert rep.chart and rep.chart[:4] == b"\x89PNG"
    csv_bytes = await sv.export_csv(s, u)
    assert b"groceries" in csv_bytes
