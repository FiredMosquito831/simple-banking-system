"""SQLAlchemy ORM models for the banking demo.

Money is stored in integer minor units (cents) to avoid floating point error.
Balances are never stored as a single mutable number alone: every movement of
money writes a `Transaction` plus one `LedgerEntry` per account touched, and the
entry records the running balance. Transfers therefore stay auditable.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class Role(str, enum.Enum):
    customer = "customer"
    admin = "admin"


class KycStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"


class CustomerStatus(str, enum.Enum):
    active = "active"
    locked = "locked"


class AccountType(str, enum.Enum):
    checking = "checking"
    savings = "savings"


class AccountStatus(str, enum.Enum):
    active = "active"
    frozen = "frozen"
    closed = "closed"


class CardStatus(str, enum.Enum):
    active = "active"
    frozen = "frozen"
    cancelled = "cancelled"


class TxType(str, enum.Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"
    transfer = "transfer"
    card_payment = "card_payment"
    fee = "fee"
    interest = "interest"
    loan_disbursement = "loan_disbursement"
    loan_repayment = "loan_repayment"
    adjustment = "adjustment"
    reversal = "reversal"


class TxStatus(str, enum.Enum):
    completed = "completed"
    pending = "pending"
    failed = "failed"
    reversed = "reversed"


class LoanStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    active = "active"
    paid_off = "paid_off"


class InstallmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    paid = "paid"
    overdue = "overdue"


class Frequency(str, enum.Enum):
    once = "once"
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class ScheduledStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.customer.value)
    kyc_status: Mapped[str] = mapped_column(String(16), default=KycStatus.pending.value)
    status: Mapped[str] = mapped_column(String(16), default=CustomerStatus.active.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    loans: Mapped[list["Loan"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    beneficiaries: Mapped[list["Beneficiary"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    notifications: Mapped[list["Notification"]] = relationship(back_populates="customer", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + self.last_name[:1]).upper()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    account_number: Mapped[str] = mapped_column(String(34), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(60), default="Main account")
    type: Mapped[str] = mapped_column(String(16), default=AccountType.checking.value)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    overdraft_limit_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    interest_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=AccountStatus.active.value)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="accounts")
    cards: Mapped[list["Card"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    @property
    def available_cents(self) -> int:
        return self.balance_cents + self.overdraft_limit_cents

    @property
    def is_active(self) -> bool:
        return self.status == AccountStatus.active.value


class Beneficiary(Base):
    __tablename__ = "beneficiaries"
    __table_args__ = (UniqueConstraint("customer_id", "account_number", name="uq_beneficiary_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    account_number: Mapped[str] = mapped_column(String(34))
    bank_name: Mapped[str] = mapped_column(String(80), default="Danube Bank")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    customer: Mapped["Customer"] = relationship(back_populates="beneficiaries")


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    pan_encrypted: Mapped[str] = mapped_column(String(255))
    last4: Mapped[str] = mapped_column(String(4))
    brand: Mapped[str] = mapped_column(String(16), default="Visa")
    exp_month: Mapped[int] = mapped_column(Integer)
    exp_year: Mapped[int] = mapped_column(Integer)
    pin_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=CardStatus.active.value)
    daily_limit_cents: Mapped[int] = mapped_column(BigInteger, default=250_000)
    spent_today_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    spent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped["Account"] = relationship(back_populates="cards")

    @property
    def is_active(self) -> bool:
        return self.status == CardStatus.active.value

    @property
    def expiry(self) -> str:
        return f"{self.exp_month:02d}/{self.exp_year % 100:02d}"

    @property
    def masked(self) -> str:
        return f"•••• •••• •••• {self.last4}"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: new_ref("TXN"))
    type: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default=TxStatus.completed.value, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    initiated_by: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", order_by="LedgerEntry.id"
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_account_created", "account_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)  # signed: negative = money out
    balance_after_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    transaction: Mapped["Transaction"] = relationship(back_populates="entries")
    account: Mapped["Account"] = relationship()


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    principal_cents: Mapped[int] = mapped_column(BigInteger)
    outstanding_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    annual_rate_bps: Mapped[int] = mapped_column(Integer)
    term_months: Mapped[int] = mapped_column(Integer)
    monthly_payment_cents: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(16), default=LoanStatus.pending.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="loans")
    installments: Mapped[list["LoanInstallment"]] = relationship(
        back_populates="loan", cascade="all, delete-orphan", order_by="LoanInstallment.number"
    )

    @property
    def paid_cents(self) -> int:
        return self.principal_cents - self.outstanding_cents


class LoanInstallment(Base):
    __tablename__ = "loan_installments"
    __table_args__ = (UniqueConstraint("loan_id", "number", name="uq_loan_installment_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    principal_cents: Mapped[int] = mapped_column(BigInteger)
    interest_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=InstallmentStatus.scheduled.value)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    loan: Mapped["Loan"] = relationship(back_populates="installments")

    @property
    def total_cents(self) -> int:
        return self.principal_cents + self.interest_cents


class ScheduledTransfer(Base):
    __tablename__ = "scheduled_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    from_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    to_account_number: Mapped[str] = mapped_column(String(34))
    to_name: Mapped[str] = mapped_column(String(120))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(String(160), default="")
    frequency: Mapped[str] = mapped_column(String(16), default=Frequency.monthly.value)
    next_run_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=ScheduledStatus.active.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(140))
    body: Mapped[str] = mapped_column(Text, default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    customer: Mapped["Customer"] = relationship(back_populates="notifications")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(40), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Setting(Base):
    """Runtime configuration editable from the admin app (no redeploy needed)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    value_type: Mapped[str] = mapped_column(String(16), default="string")  # string | int | bool | email
    label: Mapped[str] = mapped_column(String(140), default="")
    category: Mapped[str] = mapped_column(String(60), default="General")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
