"""AI features via the Claude API: receipt reading, bank alert parsing, categorization,
period summaries with suggestions, and free-form questions about your money.

Only aggregated numbers are sent for summaries and questions, never the full history.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
from collections import defaultdict
from datetime import date

import anthropic
from pydantic import BaseModel, Field

from .config import get_settings

log = logging.getLogger(__name__)
_client: anthropic.AsyncAnthropic | None = None

# Per-user daily AI quota (protects the bot owner's AI bill on a public bot).
# Set by the bot middleware / webhook; unset for scheduled reports, which are not counted.
current_user: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user", default=None)
_usage: dict[tuple[int, date], int] = defaultdict(int)


class AiError(Exception):
    pass


def enabled() -> bool:
    return get_settings().ai_enabled


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


# ---------- schemas ----------

class ReceiptLine(BaseModel):
    name: str
    amount: float = Field(description="Line total actually paid, after line discounts")
    category: str = Field(description="One of the provided category names")


class ReceiptData(BaseModel):
    is_receipt: bool = Field(description="True for a receipt, invoice, or payment/transfer screenshot")
    is_income: bool = Field(False, description="True if money was received (incoming transfer, salary)")
    merchant: str = Field(description="Store, service, or the other party of a transfer")
    date: str | None = Field(None, description="YYYY-MM-DD if printed")
    currency: str | None = Field(None, description="ISO 4217 code, inferred from symbols/country")
    total: float
    items: list[ReceiptLine]


class BankTx(BaseModel):
    is_transaction: bool = Field(description="False for OTPs, ads, balance-only or failed payments")
    type: str = Field(description="expense or income")
    amount: float
    currency: str | None = None
    merchant: str = ""
    date: str | None = Field(None, description="YYYY-MM-DD if present")
    card_or_account_hint: str = Field("", description="Card last digits or account name mentioned")
    category: str = Field(description="One of the provided category names")


class StatementRows(BaseModel):
    rows: list[BankTx]


class CategoryPick(BaseModel):
    category: str


class Insight(BaseModel):
    summary: str = Field(description="2-3 sentences, the headline numbers and what changed")
    observations: list[str] = Field(description="Unusual spikes, recurring costs, trends; max 4")
    suggestions: list[str] = Field(description="Concrete, measurable actions with estimated savings; max 3")


def _check_quota():
    uid = current_user.get()
    limit = get_settings().ai_daily_limit
    if uid is None or limit <= 0:
        return
    key = (uid, date.today())
    if _usage[key] >= limit:
        raise AiError(f"daily AI limit reached ({limit}); it resets tomorrow")
    _usage[key] += 1
    for k in [k for k in _usage if k[1] != key[1]]:
        del _usage[k]


# ---------- calls ----------

async def _parse(schema: type[BaseModel], content, system: str, effort: str = "low", max_tokens=8000):
    _check_quota()
    try:
        resp = await client().messages.parse(
            model=get_settings().ai_model,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": effort},
            messages=[{"role": "user", "content": content}],
            output_format=schema,
        )
    except anthropic.RateLimitError as e:
        raise AiError("AI is rate limited, try again in a minute") from e
    except anthropic.APIStatusError as e:
        raise AiError(f"AI request failed ({e.status_code})") from e
    except anthropic.APIConnectionError as e:
        raise AiError("Could not reach the AI service") from e
    if resp.stop_reason == "refusal":
        raise AiError("AI declined this request")
    if resp.parsed_output is None:
        raise AiError("AI returned no usable result")
    return resp.parsed_output


def _media_block(data: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    kind = "document" if media_type == "application/pdf" else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": b64}}


async def read_receipt(data: bytes, media_type: str, categories: list[str],
                       base_currency: str) -> ReceiptData:
    system = ("You extract money movements from images for a personal finance app: paper receipts, "
              "invoices, and screenshots of bank or payment apps (e.g. Click, Payme, Uzum, card apps, "
              "transfer confirmations, SMS screenshots). For receipts read every purchased line and "
              "merge discount lines into their item. For a screenshot of a single payment or transfer, "
              "return one item describing it. Amounts are positive numbers. Ignore fees shown separately "
              "unless they were paid. If the image shows no completed payment, set is_receipt false.")
    text = (f"Categories: {', '.join(categories)}.\n"
            f"Use the receipt's currency; if unclear assume {base_currency}. Today is {date.today()}.")
    return await _parse(ReceiptData, [_media_block(data, media_type), {"type": "text", "text": text}],
                        system, effort="low", max_tokens=12000)


async def parse_bank_message(text: str, categories: list[str], base_currency: str) -> BankTx:
    system = ("You read bank SMS, push and email alerts, or a user's casual sentence in any language "
              "(e.g. 'spent 50k on lunch', 'tushlikka 50 ming sarfladim'), for a personal finance app "
              "and extract the single transaction described. 'k' means thousand, 'm'/'mln' million, "
              "'ming' thousand. Card purchases, withdrawals and outgoing transfers are expenses; salary, "
              "incoming transfers and refunds are income. For a casual sentence, put what was bought "
              "in merchant.")
    prompt = (f"Categories: {', '.join(categories)}.\nDefault currency: {base_currency}. "
              f"Today is {date.today()}.\n\nAlert:\n<alert>\n{text[:2000]}\n</alert>")
    return await _parse(BankTx, prompt, system)


async def parse_statement(data: bytes, media_type: str, categories: list[str],
                          base_currency: str) -> list[BankTx]:
    system = "You extract every transaction row from a bank statement for a personal finance app."
    text = f"Categories: {', '.join(categories)}. Default currency: {base_currency}."
    content = ([_media_block(data, media_type)] if media_type in ("application/pdf",) or
               media_type.startswith("image/") else [{"type": "text", "text": data.decode(errors="ignore")[:150000]}])
    res = await _parse(StatementRows, content + [{"type": "text", "text": text}], system,
                       effort="low", max_tokens=32000)
    return [r for r in res.rows if r.is_transaction]


async def pick_category(text: str, categories: list[str]) -> str:
    res = await _parse(CategoryPick, f"Transaction: {text}\nCategories: {', '.join(categories)}",
                       "Pick the single best category name for a personal finance transaction.",
                       max_tokens=1000)
    return res.category


async def insights(stats: dict, period_label: str) -> Insight:
    system = ("You are a friendly, direct personal finance coach inside a Telegram bot. "
              "Use only the numbers given. Compare with the previous period. Suggestions must be "
              "specific and quantified in the user's currency (e.g. 'cutting delivery to twice a "
              "week saves about 120 a month'). If income is zero, focus on spending. "
              "No generic advice, no disclaimers.")
    prompt = f"{period_label} report data:\n{json.dumps(stats, ensure_ascii=False, default=str)}"
    return await _parse(Insight, prompt, system, effort="medium", max_tokens=8000)


async def answer(question: str, context: dict) -> str:
    system = ("You answer questions about the user's own finances inside a Telegram bot, using only "
              "the data provided. Be concise (under 120 words), give numbers with currency, "
              "and say plainly when the data doesn't contain the answer.")
    _check_quota()
    try:
        resp = await client().messages.create(
            model=get_settings().ai_model, max_tokens=4000, system=system,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content":
                       f"Data:\n{json.dumps(context, ensure_ascii=False, default=str)}\n\nQuestion: {question}"}],
        )
    except anthropic.APIError as e:
        raise AiError("AI request failed") from e
    if resp.stop_reason == "refusal":
        raise AiError("AI declined this request")
    return "".join(b.text for b in resp.content if b.type == "text").strip()
