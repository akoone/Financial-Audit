from datetime import date
from decimal import Decimal

import pytest

from app import ocr

RECEIPT = """KORZINKA SUPERMARKET
Toshkent sh., Chilonzor
05.11.2026 18:42
Sut 1L            14 500.00
Non                4 000.00
Shampun          32 900.00
QQS 12%            5 520.00
JAMI:            51 400.00 so'm
Karta: 8600 **** **** 1234"""

PAYME = """Payme
To'lov muvaffaqiyatli
Qabul qiluvchi:
Uzmobile
Summa: 50 000 so'm
07.11.2026 09:15"""

CLICK_IN = """Click
Pul qabul qilindi
+1 500 000 so'm
Kimdan: Aziz K.
06.11.2026"""

RU = """ООО МАГНИТ
Итого к оплате: 1 234,50 руб
12.10.2026"""


def test_receipt():
    r = ocr.parse_text(RECEIPT)
    assert r.total == Decimal("51400.00") and r.currency == "UZS" and r.on == date(2026, 11, 5)
    assert r.merchant == "KORZINKA SUPERMARKET" and not r.is_income
    assert [i.amount for i in r.items] == [Decimal("14500.00"), Decimal("4000.00"), Decimal("32900.00")]


def test_payment_screenshot():
    r = ocr.parse_text(PAYME)
    assert r.total == Decimal(50000) and r.merchant == "Uzmobile" and r.currency == "UZS"


def test_incoming_transfer():
    r = ocr.parse_text(CLICK_IN)
    assert r.is_income and r.total == Decimal(1500000) and r.merchant == "Aziz K."


def test_russian():
    r = ocr.parse_text(RU)
    assert r.total == Decimal("1234.50") and r.currency == "RUB" and r.on == date(2026, 10, 12)


@pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")
async def test_real_ocr_on_image():
    from PIL import Image, ImageDraw, ImageFont
    import io
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", 26)
    except OSError:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 26)
    im = Image.new("RGB", (820, 420), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(RECEIPT.splitlines()):
        d.text((30, 30 + i * 40), line, font=font, fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    r = await ocr.read_image(buf.getvalue())
    assert r.total == Decimal("51400.00") and r.currency == "UZS"
