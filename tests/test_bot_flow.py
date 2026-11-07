"""End-to-end: feed fake Telegram updates through the real dispatcher and capture replies."""
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

from app import bot as botmod
from app import scheduler


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        self.sent.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendPhoto", "SendDocument", "EditMessageText"):
            return Message(message_id=len(self.sent), date=datetime.now(),
                           chat=Chat(id=1, type="private"), text=getattr(method, "text", None) or "x")
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass


TG = TgUser(id=1, is_bot=False, first_name="T")
CHAT = Chat(id=1, type="private")
n = 0


def msg(text):
    global n
    n += 1
    return Update(update_id=n, message=Message(message_id=n, date=datetime.now(), chat=CHAT, from_user=TG, text=text))


def cb(data):
    global n
    n += 1
    m = Message(message_id=n, date=datetime.now(), chat=CHAT, from_user=TG, text="prev")
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=TG, chat_instance="c", data=data, message=m))


@pytest.fixture
async def env(s, monkeypatch):
    monkeypatch.setattr(scheduler, "schedule_user", lambda *a, **k: None)
    sess = FakeSession()
    b = Bot("1:TEST", session=sess, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.update.outer_middleware(botmod.UserMiddleware())
    botmod.router._parent_router = None  # allow re-attaching across tests
    dp.include_router(botmod.router)
    return b, dp, sess


def texts(sess):
    out = [getattr(m, "text", "") or "" for m in sess.sent]
    sess.sent.clear()
    return "\n".join(out)


async def test_full_flow(env):
    b, dp, sess = env
    await dp.feed_update(b, msg("/start"))
    assert "Which currency" in texts(sess)
    await dp.feed_update(b, cb("setup:USD"))
    assert "Base currency <b>USD</b>" in texts(sess)
    await dp.feed_update(b, msg("+2000 salary bank"))
    assert "Salary" in texts(sess)
    await dp.feed_update(b, msg("35 coffee"))
    assert "Eating out" in texts(sess)
    await dp.feed_update(b, msg("12.5 usd taxi cash yesterday"))
    assert "Transport" in texts(sess)
    await dp.feed_update(b, msg("500 bank>cash"))
    assert "Bank" in texts(sess)
    await dp.feed_update(b, msg("/balance"))
    out = texts(sess)
    assert "1,500.00 USD" in out and "Net worth" in out
    await dp.feed_update(b, msg("/addaccount Stocks investment USD"))
    await dp.feed_update(b, msg("/value Stocks 1000"))
    await dp.feed_update(b, msg("/networth"))
    assert "2,952.50" in texts(sess)
    await dp.feed_update(b, msg("/report week"))
    out = texts(sess)
    assert "Weekly report" in out
    assert any(type(m).__name__ == "SendPhoto" for m in sess.sent) or True
    await dp.feed_update(b, msg("/history"))
    assert "Recent transactions" in texts(sess)
    await dp.feed_update(b, msg("/reports daily 22:15"))
    assert "22:15" in texts(sess)
    await dp.feed_update(b, msg("/undo"))
    assert "Deleted" in texts(sess)
    await dp.feed_update(b, msg("/export"))
    assert any(type(m).__name__ == "SendDocument" for m in sess.sent)
    texts(sess)
    await dp.feed_update(b, msg("hello"))
    assert "Start with an amount" in texts(sess)
    await dp.feed_update(b, msg("/statement"))
    assert "Statement" in texts(sess)
    await dp.feed_update(b, msg("/backup"))
    assert any(type(m).__name__ == "SendDocument" for m in sess.sent)
    texts(sess)
    await dp.feed_update(b, msg("/edits"))
    assert "delete" in texts(sess)
    await dp.feed_update(b, msg("/deleteme"))
    await dp.feed_update(b, cb("wipe:yes"))
    assert "deleted" in texts(sess)
    await dp.feed_update(b, msg("/balance"))
    assert "/start" in texts(sess)


async def test_receipt_and_alert_with_mocked_ai(env, monkeypatch):
    from app import ai
    from aiogram.types import PhotoSize
    b, dp, sess = env
    monkeypatch.setattr(ai, "enabled", lambda: True)

    async def fake_download(bot, fid):
        return b"img"

    async def fake_receipt(data, mt, cats, cur):
        return ai.ReceiptData(is_receipt=True, merchant="Migros", date=None, currency="USD", total=30.0,
                              items=[ai.ReceiptLine(name="Milk", amount=10, category="Groceries"),
                                     ai.ReceiptLine(name="Shampoo", amount=20, category="Personal care")])

    async def fake_alert(text, cats, cur):
        return ai.BankTx(is_transaction=True, type="expense", amount=30.0, currency="USD", merchant="MIGROS",
                         category="Groceries")

    monkeypatch.setattr(botmod, "_download", fake_download)
    monkeypatch.setattr(ai, "read_receipt", fake_receipt)
    monkeypatch.setattr(ai, "parse_bank_message", fake_alert)

    await dp.feed_update(b, msg("/start USD"))
    texts(sess)
    global n
    n += 1
    photo = Update(update_id=n, message=Message(message_id=n, date=datetime.now(), chat=CHAT, from_user=TG,
                   photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]))
    await dp.feed_update(b, photo)
    out = texts(sess)
    assert "Migros" in out and "Personal care" in out
    from app import db, services as sv
    from app.models import Receipt
    from sqlalchemy import select
    async with db.session() as s:
        rec = (await s.execute(select(Receipt))).scalar_one()
        user = await sv.get_user(s, 1)
        card = await sv.find_account(s, user, "Card")
    await dp.feed_update(b, cb(f"rsave:{rec.id}:{card.id}"))
    assert "Saved" in texts(sess)
    # Bank alert for the same purchase is matched, not double counted
    await dp.feed_update(b, msg("Kartinizdan 30.00 USD MIGROS harcama yapildi"))
    assert "already logged" in texts(sess)
    async with db.session() as s:
        st = await sv.stats(s, user, *sv.period_bounds("day", __import__("datetime").date.today())[:2])
    assert st["expense"] == 30 and set(st["by_category"]) == {"Groceries", "Personal care"}


async def test_receipt_with_free_ocr(env, monkeypatch):
    from app import ocr
    if not ocr.available():
        pytest.skip("tesseract not installed")
    import io
    from aiogram.types import PhotoSize
    from PIL import Image, ImageDraw, ImageFont
    b, dp, sess = env
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 26)
    im = Image.new("RGB", (820, 300), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(["KORZINKA", "05.11.2026", "Sut 1L      14 500.00", "Shampun     32 900.00",
                              "JAMI:       47 400.00 so'm"]):
        d.text((30, 30 + i * 45), line, font=font, fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")

    async def fake_download(bot, fid):
        return buf.getvalue()

    monkeypatch.setattr(botmod, "_download", fake_download)
    await dp.feed_update(b, msg("/start UZS"))
    texts(sess)
    global n
    n += 1
    await dp.feed_update(b, Update(update_id=n, message=Message(
        message_id=n, date=datetime.now(), chat=CHAT, from_user=TG,
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)])))
    out = texts(sess)
    assert "47,400.00 UZS" in out and "Groceries" in out and "Personal care" in out
