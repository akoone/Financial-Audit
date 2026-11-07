"""Database schema. Money is stored as Numeric (exact decimals), never floats."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(18, 4)
Rate = Numeric(20, 10)

ACCOUNT_TYPES = [
    "cash", "bank", "card", "savings", "investment", "crypto", "property", "vehicle",
    "credit_card", "loan", "receivable",
]
LIABILITY_TYPES = {"credit_card", "loan"}
VALUED_TYPES = {"investment", "crypto", "property", "vehicle"}  # value comes from valuations


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    language: Mapped[str] = mapped_column(String(8), default="en")
    default_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingest_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(20), default="cash")
    currency: Mapped[str] = mapped_column(String(3))
    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def is_liability(self) -> bool:
        return self.type in LIABILITY_TYPES


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(10))  # income | expense
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    emoji: Mapped[str] = mapped_column(String(8), default="")


class CategoryRule(Base):
    """Learned mapping: text pattern (merchant/item/word) -> category. User corrections win."""
    __tablename__ = "category_rules"
    __table_args__ = (UniqueConstraint("user_id", "pattern"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    pattern: Mapped[str] = mapped_column(String(100))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    source: Mapped[str] = mapped_column(String(10), default="user")  # user | ai
    hits: Mapped[int] = mapped_column(Integer, default=0)


class Transaction(Base):
    """One row per money movement. A transfer is two rows sharing transfer_group.

    amount          signed, in `currency` (expense / transfer-out negative)
    account_amount  signed, in the account's currency (drives balances)
    base_amount     signed, in the user's base currency at the FX rate of that day (drives reports)
    """
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    type: Mapped[str] = mapped_column(String(12))  # income | expense | transfer | adjustment
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    account_amount: Mapped[Decimal] = mapped_column(Money)
    base_amount: Mapped[Decimal] = mapped_column(Money)
    fx_rate_to_base: Mapped[Decimal] = mapped_column(Rate, default=Decimal(1))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    merchant: Mapped[str] = mapped_column(String(120), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    transfer_group: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id"), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_via: Mapped[str] = mapped_column(String(12), default="text")  # text|receipt|alert|import
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    category: Mapped[Category | None] = relationship(lazy="joined")
    account: Mapped[Account] = relationship(lazy="joined")


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(200))
    merchant: Mapped[str] = mapped_column(String(120), default="")
    purchased_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="")
    raw_ai_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|confirmed|discarded


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)


class AssetValuation(Base):
    __tablename__ = "asset_valuations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    value: Mapped[Decimal] = mapped_column(Money)
    valued_on: Mapped[date] = mapped_column(Date)


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("day", "base", "quote"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Rate)


class ReportSchedule(Base):
    __tablename__ = "report_schedules"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # daily | weekly | monthly
    send_time: Mapped[str] = mapped_column(String(5))  # HH:MM in user's timezone
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    include_ai: Mapped[bool] = mapped_column(Boolean, default=True)


class AiInsight(Base):
    __tablename__ = "ai_insights"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Snapshot(Base):
    """Frozen end-of-day picture of everything you own and owe. Never changed afterwards
    (except re-taken for the same day), so years later you can see exactly what you had."""
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    assets: Mapped[Decimal] = mapped_column(Money)
    liabilities: Mapped[Decimal] = mapped_column(Money)
    net_worth: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SnapshotLine(Base):
    __tablename__ = "snapshot_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer)
    account_name: Mapped[str] = mapped_column(String(60))
    account_type: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    balance: Mapped[Decimal] = mapped_column(Money)          # in the account's currency
    value_base: Mapped[Decimal] = mapped_column(Money)       # in base currency that day
    fx_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal(1))


class AuditLog(Base):
    """Every change and deletion of a transaction, with the full record before the change."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(20))  # create | update | delete | recalc
    detail: Mapped[str] = mapped_column(String(255), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
