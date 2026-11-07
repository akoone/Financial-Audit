from decimal import Decimal

import pytest

from app import db, fx

RATES = {"USD": {"USD": 1, "EUR": 0.9, "KRW": 1400, "TRY": 34}, }


async def fake_fetch(base):
    usd = {k: Decimal(str(v)) for k, v in RATES["USD"].items()}
    b = usd[base]
    return {k: v / b for k, v in usd.items()}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(fx, "_fetch", fake_fetch)


@pytest.fixture
async def s():
    db.init_engine("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    async with db.session() as session:
        yield session
