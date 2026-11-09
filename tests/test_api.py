"""Mini App API: setup, add/edit/delete, transfers, accounts, corrections, statements, settings, webhook."""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx
import pytest

from app import services as sv
from app.config import get_settings
from app.webapp import create_app, parse_number

TOKEN = "T:1"


def init_data(user_id=42, first_name="Ali"):
    fields = {"auth_date": str(int(time.time())), "query_id": "x",
              "user": json.dumps({"id": user_id, "first_name": first_name})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def settings(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "bot_token", TOKEN)
    monkeypatch.setattr(st, "allowed_user_ids", "")
    monkeypatch.setattr(st, "anthropic_api_key", "")
    return st


@pytest.fixture
async def client(s, settings):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 headers={"X-Telegram-Init-Data": init_data()}) as c:
        yield c


def test_parse_number():
    assert parse_number("50k") == Decimal("50000")
    assert parse_number("1.2m") == Decimal("1200000")
    assert parse_number("2 mln") == Decimal("2000000")
    assert parse_number("1 250 000") == Decimal("1250000")
    assert parse_number("1,250,000.50") == Decimal("1250000.50")
    assert parse_number("12,5") == Decimal("12.5")
    assert parse_number("-300", positive=False) == Decimal("-300")
    assert parse_number("0", positive=False) == 0
    for bad in ("", "abc", "-5", "0"):
        with pytest.raises(Exception):
            parse_number(bad)


async def test_setup_and_profile(client, s):
    r = await client.get("/api/me")
    assert r.status_code == 200 and r.json()["exists"] is False
    assert (await client.get("/api/transactions")).status_code == 404  # not set up yet
    r = await client.post("/api/setup", json={"currency": "UZS", "timezone": "Asia/Tashkent"})
    assert r.status_code == 200
    me = (await client.get("/api/me")).json()
    assert me["exists"] and me["user"]["base_currency"] == "UZS" and me["user"]["timezone"] == "Asia/Tashkent"
    assert [a["name"] for a in me["accounts"]] == ["Cash", "Bank", "Card"]
    assert me["categories"]["expense"] and me["categories"]["income"]
    assert [r["kind"] for r in me["reports"]] == ["daily", "weekly", "monthly"]
    assert me["storage"] == "SQLite file"
    # no initData / forged initData
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as anon:
        assert (await anon.get("/api/me")).status_code == 401
        assert (await anon.get("/api/me", headers={"X-Telegram-Init-Data": init_data().replace("Ali", "Bob")})).status_code == 401


async def test_add_edit_delete(client, s):
    await client.post("/api/setup", json={"currency": "USD", "timezone": "UTC"})
    me = (await client.get("/api/me")).json()
    card = next(a for a in me["accounts"] if a["name"] == "Card")
    cash = next(a for a in me["accounts"] if a["name"] == "Cash")
    food = next(c for c in me["categories"]["expense"] if c["name"] == "Eating out")

    # guess from the note, no category given
    r = await client.post("/api/transactions", json={"type": "expense", "amount": "12.5", "note": "coffee"})
    assert r.status_code == 200, r.text
    t = r.json()["transaction"]
    assert t["amount"] == -12.5 and t["category"] == "Eating out" and t["account_id"] == card["id"] and t["source"] == "app"

    assert (await client.get("/api/guess", params={"text": "taxi home", "type": "expense"})).json()["category_id"]

    # explicit category, account, date and "k" amount
    r = await client.post("/api/transactions", json={"type": "income", "amount": "3k", "note": "freelance",
                                                       "account_id": cash["id"], "date": "2026-01-15"})
    inc = r.json()["transaction"]
    assert inc["amount"] == 3000 and inc["date"] == "2026-01-15" and inc["account"] == "Cash"
    bad = await client.post("/api/transactions", json={"type": "expense", "amount": "5", "category_id": inc["category_id"]})
    assert bad.status_code == 422  # income category on an expense

    # edit: amount, category, account; the old version is kept in the history
    r = await client.patch(f"/api/transactions/{t['id']}", json={"amount": "14", "category_id": food["id"],
                                                                  "account_id": cash["id"], "note": "coffee & cake"})
    assert r.status_code == 200 and "amount" in r.json()["changed"]
    full = (await client.get(f"/api/transactions/{t['id']}")).json()
    assert full["amount"] == -14 and full["account"] == "Cash" and full["note"] == "coffee & cake"
    assert [h["action"] for h in full["history"]] == ["create", "update"]
    # change expense -> income: category moves to an income category
    r = await client.patch(f"/api/transactions/{t['id']}", json={"type": "income"})
    assert r.json()["transaction"]["amount"] == 14 and r.json()["transaction"]["category"] == "Other income"

    lst = (await client.get("/api/transactions", params={"q": "cake"})).json()
    assert [x["id"] for x in lst["items"]] == [t["id"]]
    assert (await client.get("/api/transactions", params={"month": "2026-01"})).json()["income"] == 3000

    assert (await client.delete(f"/api/transactions/{t['id']}")).status_code == 200
    assert (await client.delete(f"/api/transactions/{t['id']}")).status_code == 404
    edits = (await client.get(f"/api/transactions/{inc['id']}")).json()
    assert edits["id"] == inc["id"]

    # another user can't see or touch these entries
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t",
                              headers={"X-Telegram-Init-Data": init_data(user_id=7)})
    async with other:
        await other.post("/api/setup", json={"currency": "USD"})
        assert (await other.get(f"/api/transactions/{inc['id']}")).status_code == 404
        assert (await other.delete(f"/api/transactions/{inc['id']}")).status_code == 404
        assert (await other.get("/api/transactions")).json()["items"] == []


async def test_transfers_accounts_and_statement(client, s):
    await client.post("/api/setup", json={"currency": "USD", "timezone": "UTC"})
    me = (await client.get("/api/me")).json()
    bank = next(a for a in me["accounts"] if a["name"] == "Bank")
    cash = next(a for a in me["accounts"] if a["name"] == "Cash")

    await client.post("/api/transactions", json={"type": "income", "amount": "1000", "account_id": bank["id"], "note": "salary"})
    r = await client.post("/api/transactions", json={"type": "transfer", "amount": "200", "account_id": bank["id"],
                                                       "to_account_id": cash["id"]})
    tr = r.json()["transaction"]
    assert tr["to_account"] == "Cash" and tr["amount"] == -200
    same = await client.post("/api/transactions", json={"type": "transfer", "amount": "1", "account_id": bank["id"],
                                                          "to_account_id": bank["id"]})
    assert same.status_code == 422
    items = (await client.get("/api/transactions")).json()["items"]
    assert sum(1 for x in items if x["type"] == "transfer") == 1  # listed once
    assert len((await client.get("/api/transactions", params={"account_id": cash["id"]})).json()["items"]) == 1

    # counted cash is 150, not 200: a correction of -50 (not spending)
    r = await client.post(f"/api/accounts/{cash['id']}/balance", json={"balance": "150"})
    assert r.json()["adjustment"]["amount"] == -50 and r.json()["balance"] == 150
    dash = (await client.get("/api/dashboard", params={"period": "month"})).json()
    assert dash["expense"] == 0 and dash["income"] == 1000 and dash["net_worth"] == 950
    assert dash["other"]["transfers"] == 200 and dash["other"]["corrections"] == -50
    from datetime import date, timedelta
    t = date.today()
    rng = (await client.get("/api/dashboard", params={"period": "custom", "start": t.isoformat(),
                                                        "end": (t - timedelta(days=9)).isoformat()})).json()
    assert rng["period"] == "custom" and rng["start"] == (t - timedelta(days=9)).isoformat() and rng["end"] == t.isoformat()
    assert rng["income"] == 1000 and rng["other"]["transfers"] == 200
    far = await client.get("/api/dashboard", params={"period": "custom", "start": "2020-01-01", "end": t.isoformat()})
    assert far.status_code == 422

    # new accounts: a loan is stored as a negative balance; an investment uses its value
    r = await client.post("/api/accounts", json={"name": "Car loan", "type": "loan", "currency": "USD", "opening": "400"})
    loan_id = r.json()["id"]
    assert (await client.post("/api/accounts", json={"name": "car LOAN", "type": "loan"})).status_code == 409
    r = await client.post("/api/accounts", json={"name": "Stocks", "type": "investment", "currency": "USD"})
    stocks_id = r.json()["id"]
    await client.post(f"/api/accounts/{stocks_id}/balance", json={"balance": "500"})
    me = (await client.get("/api/me")).json()
    assert me["liabilities"] == 400 and me["net_worth"] == 950 - 400 + 500

    # rename, default, archive
    assert (await client.patch(f"/api/accounts/{cash['id']}", json={"name": "Wallet"})).status_code == 200
    assert (await client.patch(f"/api/accounts/{cash['id']}", json={"name": "Bank"})).status_code == 409
    await client.patch(f"/api/accounts/{cash['id']}", json={"default": True})
    assert (await client.get("/api/me")).json()["user"]["default_account_id"] == cash["id"]
    await client.patch(f"/api/accounts/{cash['id']}", json={"archived": True})
    me = (await client.get("/api/me")).json()
    assert "Wallet" not in [a["name"] for a in me["accounts"]] and me["user"]["default_account_id"] != cash["id"]
    assert loan_id

    st = (await client.get("/api/statement")).json()
    assert st["income"] == 1000 and st["closing"]["net_worth"] == me["net_worth"]
    assert any(line["account"] == "Stocks" for line in st["lines"])
    assert (await client.get("/api/statement", params={"month": "2999-01"})).status_code == 422

    # edit a transfer's date: both sides move
    r = await client.patch(f"/api/transactions/{tr['id']}", json={"date": "2026-02-01", "note": "to wallet"})
    assert r.status_code == 200
    legs = (await client.get("/api/transactions", params={"type": "transfer", "account_id": bank["id"]})).json()["items"]
    assert legs[0]["date"] == "2026-02-01"


async def test_settings_and_files(client, s):
    await client.post("/api/setup", json={"currency": "USD"})
    r = await client.post("/api/settings", json={"timezone": "Asia/Tashkent",
                                                  "reports": [{"kind": "daily", "time": "22:30", "enabled": False}]})
    assert r.status_code == 200
    me = (await client.get("/api/me")).json()
    assert me["user"]["timezone"] == "Asia/Tashkent"
    assert me["reports"][0] == {"kind": "daily", "time": "22:30", "enabled": False, "next": None}
    r = await client.post("/api/settings", json={"reports": [{"kind": "weekly", "time": "07:05", "enabled": True}]})
    wk = next(x for x in r.json()["reports"] if x["kind"] == "weekly")
    assert wk["time"] == "07:05" and wk["next"].endswith("T07:05")
    assert (await client.post("/api/settings", json={"timezone": "Mars/Base"})).status_code == 422
    assert (await client.post("/api/settings", json={"reports": [{"kind": "daily", "time": "25:00"}]})).status_code == 422
    assert (await client.post("/api/send", json={"what": "backup"})).status_code == 503  # no bot in this test
    assert (await client.get("/")).status_code == 200 and (await client.get("/health")).json()["ok"] is True
    assert (await client.get("/app")).headers["cache-control"] == "no-cache"


async def test_categories_create_edit_delete(client, s):
    await client.post("/api/setup", json={"currency": "USD", "timezone": "UTC"})
    r = await client.post("/api/categories", json={"name": " Gym ", "kind": "expense", "emoji": "🏋️"})
    assert r.status_code == 200, r.text
    gym = r.json()
    assert gym["name"] == "Gym" and gym["kind"] == "expense"
    assert (await client.post("/api/categories", json={"name": "gym", "kind": "expense"})).status_code == 409
    assert (await client.post("/api/categories", json={"name": "", "kind": "expense"})).status_code == 422
    assert (await client.post("/api/categories", json={"name": "X", "kind": "transfer"})).status_code == 422
    me = (await client.get("/api/me")).json()
    assert any(c["id"] == gym["id"] for c in me["categories"]["expense"])

    t = (await client.post("/api/transactions", json={"type": "expense", "amount": "30", "note": "monthly pass",
                                                        "category_id": gym["id"]})).json()["transaction"]
    assert t["category"] == "Gym"

    r = await client.patch(f"/api/categories/{gym['id']}", json={"name": "Sport", "emoji": "⚽"})
    assert r.status_code == 200 and r.json()["name"] == "Sport"
    assert (await client.get(f"/api/transactions/{t['id']}")).json()["category"] == "Sport"
    other = next(c for c in me["categories"]["expense"] if c["name"] == "Other")
    food = next(c for c in me["categories"]["expense"] if c["name"] == "Eating out")
    assert (await client.patch(f"/api/categories/{other['id']}", json={"name": "Misc"})).status_code == 409
    assert (await client.patch(f"/api/categories/{other['id']}", json={"emoji": "🧺"})).status_code == 200
    assert (await client.patch(f"/api/categories/{gym['id']}", json={"name": "Eating out"})).status_code == 409
    assert (await client.delete(f"/api/categories/{other['id']}")).status_code == 409
    income = me["categories"]["income"][0]
    assert (await client.delete(f"/api/categories/{gym['id']}", params={"move_to": income["id"]})).status_code == 422

    r = await client.delete(f"/api/categories/{gym['id']}", params={"move_to": food["id"]})
    assert r.json() == {"ok": True, "moved": 1, "to": "Eating out"}
    assert (await client.get(f"/api/transactions/{t['id']}")).json()["category"] == "Eating out"
    me = (await client.get("/api/me")).json()
    assert not any(c["id"] == gym["id"] for c in me["categories"]["expense"])
    # default target is "Other"
    tmp = (await client.post("/api/categories", json={"name": "Tmp", "kind": "expense"})).json()
    await client.post("/api/transactions", json={"type": "expense", "amount": "1", "category_id": tmp["id"]})
    assert (await client.delete(f"/api/categories/{tmp['id']}")).json()["to"] == "Other"


async def test_debts_and_archive(client, s):
    await client.post("/api/setup", json={"currency": "USD", "timezone": "UTC"})
    me = (await client.get("/api/me")).json()
    cash = next(a for a in me["accounts"] if a["name"] == "Cash")
    card = next(a for a in me["accounts"] if a["name"] == "Card")
    nw0 = me["net_worth"]

    # a plain record (no account): nothing else changes, kept out of net worth
    r = await client.post("/api/debts", json={"direction": "lent", "person": "Ali", "amount": "100",
                                              "date": "2026-11-01", "due": "2026-12-01", "note": "for rent"})
    assert r.status_code == 200, r.text
    ali = r.json()["id"]
    # borrowed 50 and it really came into the Card
    vali = (await client.post("/api/debts", json={"direction": "borrowed", "person": "Vali", "amount": "50",
                                                  "account_id": card["id"]})).json()["id"]
    me = (await client.get("/api/me")).json()
    acc = {a["id"]: a for a in me["accounts"]}
    assert acc[ali]["type"] == "receivable" and acc[ali]["balance"] == 100 and acc[ali]["due"] == "2026-12-01"
    assert acc[ali]["note"] == "for rent" and not acc[ali]["in_net_worth"]
    assert acc[vali]["type"] == "debt" and acc[vali]["balance"] == -50
    assert acc[cash["id"]]["balance"] == 0 and acc[card["id"]]["balance"] == 50
    assert me["net_worth"] == nw0 + 50 and me["liabilities"] == 0  # debts don't count
    assert me["debts"] == {"owed_to_me": 100.0, "i_owe": 50.0}

    # lending more to the same person adds to the same debt
    assert (await client.post("/api/debts", json={"direction": "lent", "person": "ali", "amount": "20"})).json()["id"] == ali
    assert (await client.post("/api/debts", json={"direction": "lent", "person": "", "amount": "5"})).status_code == 422
    assert (await client.post("/api/debts", json={"direction": "lent", "person": "X", "amount": "5",
                                                  "account_id": vali})).status_code == 422

    # partial return, too much, edit, delete an entry
    r = await client.post(f"/api/debts/{ali}/repay", json={"amount": "70"})
    assert r.json() == {"ok": True, "left": 50.0, "closed": False}
    assert (await client.post(f"/api/debts/{ali}/repay", json={"amount": "60"})).status_code == 422
    d = (await client.get(f"/api/debts/{ali}")).json()
    assert d["left"] == 50 and [e["kind"] for e in d["entries"]] == ["return", "add", "add"]
    first = d["entries"][-1]
    r = await client.patch(f"/api/debts/{ali}/entries/{first['id']}", json={"amount": "150", "date": "2026-10-30"})
    assert r.json()["left"] == 100
    assert (await client.get(f"/api/debts/{ali}")).json()["entries"][-1]["date"] == "2026-10-30"
    ret = d["entries"][0]
    assert (await client.delete(f"/api/debts/{ali}/entries/{ret['id']}")).json()["left"] == 170
    # edit the debt itself
    assert (await client.patch(f"/api/debts/{ali}", json={"name": "Ali aka", "due": None, "note": ""})).status_code == 200
    d = (await client.get(f"/api/debts/{ali}")).json()
    assert d["name"] == "Ali aka" and d["due"] is None and d["note"] == ""
    assert (await client.patch(f"/api/debts/{ali}", json={"name": "Cash"})).status_code == 409
    # editing a transfer-based entry keeps the account side in step
    v_entry = (await client.get(f"/api/debts/{vali}")).json()["entries"][0]
    await client.patch(f"/api/debts/{vali}/entries/{v_entry['id']}", json={"amount": "80"})
    me = (await client.get("/api/me")).json()
    assert next(a for a in me["accounts"] if a["id"] == card["id"])["balance"] == 80

    # paid off -> archived; restore works
    r = await client.post(f"/api/debts/{vali}/repay", json={"amount": "80", "account_id": card["id"]})
    assert r.json()["closed"] is True
    me = (await client.get("/api/me")).json()
    assert vali in {a["id"] for a in me["archived"]}
    assert next(a for a in me["accounts"] if a["id"] == card["id"])["balance"] == 0
    assert (await client.post(f"/api/debts/{cash['id']}/repay", json={"amount": "1"})).status_code == 422
    assert (await client.patch(f"/api/accounts/{vali}", json={"archived": False})).status_code == 200
    assert vali in {a["id"] for a in (await client.get("/api/me")).json()["accounts"]}

    # delete a debt entered by mistake: its entries and the card moves go too, all logged
    assert (await client.delete(f"/api/debts/{vali}")).json() == {"ok": True, "deleted_entries": 2}
    me = (await client.get("/api/me")).json()
    assert vali not in {a["id"] for a in me["accounts"] + me["archived"]}
    assert (await client.get("/api/transactions", params={"account_id": card["id"]})).json()["items"] == []
    assert (await client.delete(f"/api/debts/{card['id']}")).status_code == 422


async def test_delete_archived_account(client, s):
    await client.post("/api/setup", json={"currency": "USD", "timezone": "UTC"})
    me = (await client.get("/api/me")).json()
    cash = next(a for a in me["accounts"] if a["name"] == "Cash")
    bank = next(a for a in me["accounts"] if a["name"] == "Bank")
    await client.post("/api/transactions", json={"type": "expense", "amount": "5", "account_id": bank["id"]})
    await client.post("/api/transactions", json={"type": "transfer", "amount": "10", "account_id": bank["id"],
                                                 "to_account_id": cash["id"]})
    assert (await client.delete(f"/api/accounts/{bank['id']}")).status_code == 409  # archive first
    await client.patch(f"/api/accounts/{bank['id']}", json={"archived": True})
    r = await client.delete(f"/api/accounts/{bank['id']}")
    assert r.json() == {"ok": True, "deleted_entries": 2}
    me = (await client.get("/api/me")).json()
    assert bank["id"] not in {a["id"] for a in me["accounts"] + me["archived"]}
    assert next(a for a in me["accounts"] if a["id"] == cash["id"])["balance"] == 0  # transfer removed both sides

    # deleting a return through History reopens a paid-off debt
    d = (await client.post("/api/debts", json={"direction": "lent", "person": "Ali", "amount": "10"})).json()["id"]
    await client.post(f"/api/debts/{d}/repay", json={"amount": "10"})
    assert d in {a["id"] for a in (await client.get("/api/me")).json()["archived"]}
    ret = (await client.get(f"/api/debts/{d}")).json()["entries"][0]
    assert (await client.delete(f"/api/transactions/{ret['id']}")).status_code == 200
    assert d in {a["id"] for a in (await client.get("/api/me")).json()["accounts"]}


async def test_scan_without_ocr(client, s, monkeypatch):
    from app import ocr
    monkeypatch.setattr(ocr, "available", lambda: False)
    await client.post("/api/setup", json={"currency": "USD"})
    r = await client.post("/api/scan", content=b"\xff\xd8fake", headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 503


async def test_telegram_webhook(s, settings, monkeypatch):
    fed = []

    class FakeDp:
        async def feed_raw_update(self, bot, update):
            fed.append(update)

    app = create_app(bot=object(), dp=FakeDp())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/telegram/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
        assert r.status_code == 401
        r = await c.post("/telegram/webhook", json={"update_id": 2},
                         headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_token})
        assert r.status_code == 200
    import asyncio
    await asyncio.sleep(0.05)
    assert fed == [{"update_id": 2}]
    # no dispatcher (polling mode): the route doesn't exist
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        assert (await c.post("/telegram/webhook", json={})).status_code in (404, 405)


async def test_local_today(s, monkeypatch):
    from datetime import datetime as real_dt, timezone

    class Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 7, 21, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(sv, "datetime", Frozen)
    u = await sv.create_user(s, 5, "U", "UZS", "Asia/Tashkent")
    assert str(sv.local_today(u)) == "2026-11-08"  # 02:30 next day in Tashkent
    u.timezone = "UTC"
    assert str(sv.local_today(u)) == "2026-11-07"
    u.timezone = "Not/AZone"
    assert sv.local_today(u)  # falls back instead of failing
