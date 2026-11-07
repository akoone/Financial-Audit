"""Free receipt and payment-screenshot reading with Tesseract OCR (no AI key needed).

Less accurate than AI: it reliably finds the total, date, currency and store name, and makes a
best effort at item lines. Used automatically when no ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import asyncio
import io
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .config import get_settings

TOTAL_WORDS = ["итого", "всего", "к оплате", "jami", "jami summa", "umumiy", "to'lov", "to‘lov",
               "total", "amount", "сумма", "summa", "оплачено", "paid", "grand total", "sum to pay",
               "toplam", "합계", "결제금액"]
SKIP_LINE_WORDS = ["qqs", "ндс", "vat", "tax", "сдача", "qaytim", "change", "наличн", "naqd", "карта",
                   "card", "karta", "balance", "баланс", "комиссия", "komissiya", "fee", "chegirma", "скидка",
                   "discount", "инн", "stir", "tel", "тел", "фиск", "fiskal", "check", "чек №", "kassir",
                   "кассир"]
INCOME_WORDS = ["зачислен", "поступлен", "received", "incoming", "qabul qilindi", "kirim", "пополнение",
                "to'ldirildi", "top up", "top-up", "credited"]
MERCHANT_LABELS = ["получатель", "qabul qiluvchi", "recipient", "merchant", "продавец", "magazin",
                   "магазин", "do'kon", "store", "payee", "to:", "kimga", "kimdan", "кому", "от кого", "отправитель", "from:"]
CURRENCY_HINTS = [(r"so['‘`ʻ]?m\b|сум\b|\buzs\b|\bsum\b", "UZS"), (r"\$|\busd\b", "USD"),
                  (r"€|\beur\b", "EUR"), (r"₽|руб|\brub\b", "RUB"), (r"₸|\bтг\b|\bkzt\b", "KZT"),
                  (r"₩|\bkrw\b|원", "KRW"), (r"₺|\btl\b|\btry\b", "TRY"), (r"£|\bgbp\b", "GBP")]

_MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:[  '.,]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?![\d])")
_DATE = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})|(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})")


def available() -> bool:
    return shutil.which("tesseract") is not None


@dataclass
class OcrItem:
    name: str
    amount: Decimal


@dataclass
class OcrResult:
    text: str
    total: Decimal | None = None
    currency: str | None = None
    on: date | None = None
    merchant: str = ""
    is_income: bool = False
    items: list[OcrItem] = field(default_factory=list)
    labeled: bool = False  # the total came from a "total" line, not a guess


def _money(s: str) -> Decimal | None:
    m = _MONEY.search(s)
    if not m:
        return None
    whole = re.sub(r"[  '.,]", "", m.group(1))
    try:
        return Decimal(f"{whole}.{m.group(2)}" if m.group(2) else whole)
    except InvalidOperation:
        return None


def _all_money(line: str) -> list[Decimal]:
    out = []
    for m in _MONEY.finditer(line):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) > 11:  # card/phone/account numbers
            continue
        whole = re.sub(r"[  '.,]", "", m.group(1))
        try:
            out.append(Decimal(f"{whole}.{m.group(2)}" if m.group(2) else whole))
        except InvalidOperation:
            pass
    return out


def _strip_dates(line: str) -> str:
    line = _DATE.sub(" ", line)
    return re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", line)  # times


def parse_text(text: str) -> OcrResult:
    res = OcrResult(text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    low_all = text.lower()

    for pat, code in CURRENCY_HINTS:
        if re.search(pat, low_all):
            res.currency = code
            break

    for m in _DATE.finditer(text):
        try:
            if m.group(1):
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                y = int(m.group(6))
                d = date(y + 2000 if y < 100 else y, int(m.group(5)), int(m.group(4)))
            if 2000 < d.year <= date.today().year + 1:
                res.on = d
                break
        except ValueError:
            continue

    res.is_income = any(w in low_all for w in INCOME_WORDS)

    # Total: the amount on a line with a total keyword (prefer the last such line), else the largest
    total = None
    for i, line in enumerate(lines):
        low = line.lower()
        if any(w in low for w in TOTAL_WORDS) and not any(w in low for w in ("qqs", "ндс", "vat", "tax")):
            nums = _all_money(_strip_dates(line))
            if not nums and i + 1 < len(lines):
                nums = _all_money(_strip_dates(lines[i + 1]))
            if nums:
                total = max(nums)
                res.labeled = True
    if total is None:
        # No total line: take the largest amount, ignoring tax IDs, phones and receipt numbers
        nums = [n for l in lines if not any(w in l.lower() for w in SKIP_LINE_WORDS)
                for n in _all_money(_strip_dates(l)) if len(str(int(n))) < 9 or "." in str(n)]
        total = max(nums) if nums else None
    res.total = total if total and total > 0 else None

    # Merchant: value after a label, else the first line that looks like a name
    for i, line in enumerate(lines):
        low = line.lower()
        for lab in MERCHANT_LABELS:
            if lab in low:
                after = line[low.index(lab) + len(lab):].strip(" :-")
                cand = after if len(re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", "", after)) >= 3 else (
                    lines[i + 1] if i + 1 < len(lines) else "")
                if cand:
                    res.merchant = cand[:80]
                    break
        if res.merchant:
            break
    if not res.merchant:
        for line in lines[:6]:
            letters = re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", "", line)
            if len(letters) >= 3 and not _all_money(_strip_dates(line)):
                res.merchant = line[:80]
                break

    # Items: "name ... amount" lines that aren't totals/taxes/payment lines
    for line in lines:
        low = line.lower()
        if any(w in low for w in TOTAL_WORDS + SKIP_LINE_WORDS):
            continue
        clean = _strip_dates(line)
        nums = _all_money(clean)
        name = re.sub(r"[\d., 'xх*=@%:]+", " ", clean)
        name = re.sub(r"\s+", " ", name).strip(" -.")
        letters = re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", "", name)
        if nums and len(letters) >= 3 and nums[-1] > 0 and (res.total is None or nums[-1] <= res.total):
            res.items.append(OcrItem(name=name[:80], amount=nums[-1]))
    if res.total and res.items:
        s = sum(i.amount for i in res.items)
        if s > res.total * Decimal("1.05"):  # unit prices mixed in; don't trust the lines
            res.items = []
    return res


def _prepare(data: bytes):
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("L")
    longest = max(img.size)
    if longest > 2400:  # big camera photos: smaller is faster and reads just as well
        k = 2400 / longest
        img = img.resize((int(img.width * k), int(img.height * k)))
    elif img.width < 1600:  # upscaling helps Tesseract with small screenshot text (e.g. "+" signs)
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    return ImageOps.autocontrast(img, cutoff=1)


def _passes() -> list[str]:
    """Latin and Cyrillic text are read in separate passes: mixing all languages at once makes
    Tesseract confuse letters (JAMI becomes ЗАМТ)."""
    langs = [l for l in get_settings().ocr_langs.split("+") if l]
    latin = [l for l in langs if l in ("eng", "uzb", "tur", "kor")]
    cyr = [l for l in langs if l not in latin] + (["eng"] if "eng" in langs else [])
    out = ["+".join(x) for x in (latin, cyr) if x]
    return list(dict.fromkeys(out)) or ["eng"]


def _ocr_sync(data: bytes, lang: str, psm: int = 6) -> str:
    import pytesseract

    # psm 6 keeps each receipt row ("Sut 1L ... 14 500") on one line
    return pytesseract.image_to_string(_prepare(data), lang=lang, config=f"--psm {psm}")


def _score(r: OcrResult) -> tuple:
    return (r.labeled, r.total is not None, len(r.items), len(r.text))


async def read_image(data: bytes) -> OcrResult:
    best = None
    for lang in _passes():
        r = parse_text(await asyncio.to_thread(_ocr_sync, data, lang))
        if best is None or _score(r) > _score(best):
            best = r
        if best.labeled:
            break
    if best.total is None:  # sparse layouts such as payment screenshots
        r = parse_text(await asyncio.to_thread(_ocr_sync, data, "+".join(_passes()), 4))
        if _score(r) > _score(best):
            best = r
    return best
