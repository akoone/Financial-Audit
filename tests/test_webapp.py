import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.webapp import validate_init_data


def make(token, user_id=42, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id}), "query_id": "x"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid():
    assert validate_init_data(make("T:1"), "T:1")["id"] == 42


def test_wrong_token_and_expired():
    with pytest.raises(ValueError):
        validate_init_data(make("T:1"), "T:2")
    with pytest.raises(ValueError):
        validate_init_data(make("T:1", age=200000), "T:1")


async def test_dashboard_api(s, monkeypatch):
    import httpx
    from app import services as sv
    from app.config import get_settings
    from app.webapp import create_app
    from decimal import Decimal
    monkeypatch.setattr(get_settings(), "bot_token", "T:1")
    monkeypatch.setattr(get_settings(), "allowed_user_ids", "")
    u = await sv.create_user(s, 42, "T", "USD", "UTC")
    card = await sv.find_account(s, u, "Card")
    cat, _ = await sv.guess_category(s, u, "coffee", "expense")
    await sv.add_transaction(s, u, card, "expense", Decimal(4), category=cat, note="coffee")
    await s.commit()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/app")).status_code == 200
        assert (await c.get("/api/dashboard")).status_code == 401
        r = await c.get("/api/dashboard?period=week", headers={"X-Telegram-Init-Data": make("T:1")})
        assert r.status_code == 200
        d = r.json()
        assert d["expense"] == 4 and d["by_category"][0]["name"] == "Eating out" and len(d["months"]) == 12
        assert (await c.post("/ingest/wrong", content="hi")).status_code == 404
