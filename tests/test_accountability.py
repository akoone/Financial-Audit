import io
import json
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app import services as sv
from app.models import AuditLog


async def test_snapshots_and_statement(s):
    u = await sv.create_user(s, 7, "T", "USD", "UTC")
    bank = await sv.find_account(s, u, "Bank")
    stocks = await sv.add_account(s, u, "Stocks", "investment", "USD")
    today = date.today()
    start = today.replace(day=1)
    # opening record: last day of previous month
    await sv.add_transaction(s, u, bank, "income", Decimal(1000), on=start - timedelta(days=3))
    await sv.set_valuation(s, stocks, Decimal(500), start - timedelta(days=3))
    await sv.take_snapshot(s, u, start - timedelta(days=1))
    # this month: earn 300, spend 100, stocks rise to 650
    await sv.add_transaction(s, u, bank, "income", Decimal(300), on=start)
    await sv.add_transaction(s, u, bank, "expense", Decimal(100), on=start)
    await sv.set_valuation(s, stocks, Decimal(650))
    st = await sv.statement(s, u, start, today)
    assert st.opening.net_worth == Decimal(1500) and st.closing.net_worth == Decimal(1850)
    assert st.change == Decimal(350) and st.saved == Decimal(200) and st.other == Decimal(150)
    assert dict((n, (o, c)) for n, _, o, c in st.lines)["Stocks"] == (Decimal(500), Decimal(650))
    csv_text = sv.statement_csv(st).decode("utf-8-sig")
    assert "NET WORTH" in csv_text and "1850.00" in csv_text
    # re-taking today's snapshot replaces it (one per day)
    await sv.take_snapshot(s, u)
    await sv.take_snapshot(s, u)
    assert len([h for h in await sv.snapshot_history(s, u) if h.day == today]) == 1


async def test_audit_and_backup(s):
    u = await sv.create_user(s, 8, "T", "USD", "UTC")
    cash = await sv.find_account(s, u, "Cash")
    bank = await sv.find_account(s, u, "Bank")
    t = await sv.add_transaction(s, u, cash, "expense", Decimal(12), note="coffee")
    out, _ = await sv.add_transfer(s, u, bank, cash, Decimal(50))
    await sv.delete_transaction(s, u, t.id)
    await sv.delete_transaction(s, u, out.id)
    rows = (await s.execute(select(AuditLog).where(AuditLog.user_id == u.id))).scalars().all()
    actions = [r.action for r in rows]
    assert actions.count("create") == 3 and actions.count("delete") == 3
    deleted = [r for r in rows if r.action == "delete" and r.transaction_id == t.id][0]
    assert deleted.data["note"] == "coffee" and Decimal(deleted.data["amount"]) == Decimal(-12)

    await sv.add_transaction(s, u, cash, "expense", Decimal(7), note="bread")
    await sv.take_snapshot(s, u)
    z = zipfile.ZipFile(io.BytesIO(await sv.backup_zip(s, u)))
    names = set(z.namelist())
    assert {"transactions.csv", "accounts.csv", "net_worth_history.csv", "account_balances_history.csv",
            "edit_history.csv", "backup.json"} <= names
    data = json.loads(z.read("backup.json"))
    assert len(data["transactions"]) == 1 and data["transactions"][0]["note"] == "bread"
    assert "coffee" in z.read("edit_history.csv").decode("utf-8-sig")
