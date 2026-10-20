"""Read-side queries used by the UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import Account, LedgerEntry, Transaction, TxStatus


@dataclass
class TxRow:
    transaction: Transaction
    entry: LedgerEntry
    counterparty: str
    counterparty_account: str

    @property
    def direction(self) -> str:
        return "in" if self.entry.amount_cents > 0 else "out"

    @property
    def signed(self) -> int:
        return self.entry.amount_cents


def _tx_query():
    return select(Transaction).options(
        selectinload(Transaction.entries).selectinload(LedgerEntry.account).selectinload(Account.customer)
    )


def account_transactions(
    db: Session,
    account: Account,
    *,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    kind: str = "",
) -> tuple[list[TxRow], int]:
    base = (
        select(Transaction.id)
        .join(LedgerEntry, LedgerEntry.transaction_id == Transaction.id)
        .where(LedgerEntry.account_id == account.id)
    )
    if search:
        base = base.where(
            or_(
                Transaction.description.ilike(f"%{search}%"),
                Transaction.reference.ilike(f"%{search}%"),
            )
        )
    if kind:
        base = base.where(Transaction.type == kind)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    ids = db.scalars(
        base.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(limit).offset(offset)
    ).all()
    if not ids:
        return [], total

    txs = db.scalars(_tx_query().where(Transaction.id.in_(ids))).all()
    order = {tx_id: i for i, tx_id in enumerate(ids)}
    txs.sort(key=lambda t: order[t.id])

    rows: list[TxRow] = []
    for tx in txs:
        entry = next((e for e in tx.entries if e.account_id == account.id), None)
        if not entry:
            continue
        others = [e for e in tx.entries if e.account_id != account.id]
        if others:
            other = others[0].account
            name = other.customer.full_name if other.customer else other.label
            number = other.account_number
        elif tx.type in ("deposit", "interest", "loan_disbursement"):
            name, number = {
                "deposit": ("Cash deposit", "—"),
                "interest": ("Interest", "—"),
                "loan_disbursement": ("Loan disbursement", "—"),
            }[tx.type]
        elif tx.type == "card_payment":
            name, number = tx.description.replace("Card purchase — ", ""), "Card"
        elif tx.type == "loan_repayment":
            name, number = "Loan repayment", "—"
        else:
            name, number = tx.description or tx.type.replace("_", " ").title(), "—"
        rows.append(TxRow(transaction=tx, entry=entry, counterparty=name, counterparty_account=number))
    return rows, total


def monthly_flow(db: Session, account_ids: list[int], days: int = 30) -> tuple[int, int]:
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(
            func.sum(func.greatest(LedgerEntry.amount_cents, 0)),
            func.sum(func.least(LedgerEntry.amount_cents, 0)),
        )
        .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
        .where(LedgerEntry.account_id.in_(account_ids), Transaction.created_at >= since,
               Transaction.status == TxStatus.completed.value)
    ).one()
    return int(rows[0] or 0), int(-(rows[1] or 0))


def balance_series(db: Session, account_ids: list[int], days: int = 14) -> list[dict]:
    """End-of-day total balance for the last `days` days (from ledger snapshots)."""
    end = datetime.utcnow().date()
    start = end - timedelta(days=days - 1)
    current_total = db.scalar(
        select(func.coalesce(func.sum(Account.balance_cents), 0)).where(Account.id.in_(account_ids))
    ) or 0

    # walk backwards from today's total using each day's net movement
    day = end
    series: list[dict] = []
    total = int(current_total)
    while day >= start:
        series.append({"date": day, "value": total})
        movements = db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            .where(
                LedgerEntry.account_id.in_(account_ids),
                func.date(LedgerEntry.created_at) == day,
            )
        ).scalar() or 0
        total -= int(movements)
        day -= timedelta(days=1)
    series.reverse()
    return series


def spending_by_type(db: Session, account_ids: list[int], days: int = 30) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.execute(
        select(Transaction.type, func.sum(-LedgerEntry.amount_cents))
        .join(LedgerEntry, LedgerEntry.transaction_id == Transaction.id)
        .where(
            LedgerEntry.account_id.in_(account_ids),
            LedgerEntry.amount_cents < 0,
            Transaction.created_at >= since,
        )
        .group_by(Transaction.type)
        .order_by(func.sum(-LedgerEntry.amount_cents).desc())
    ).all()
    labels = {
        "transfer": "Transfers",
        "card_payment": "Card payments",
        "withdrawal": "Withdrawals",
        "loan_repayment": "Loan repayments",
        "fee": "Fees",
    }
    total = sum(int(v) for _, v in rows) or 1
    return [
        {"type": t, "label": labels.get(t, t.replace("_", " ").title()), "cents": int(v), "pct": round(int(v) / total * 100)}
        for t, v in rows
    ]


def total_balance(db: Session, customer_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(Account.balance_cents), 0)).where(
                Account.customer_id == customer_id, Account.status != "closed"
            )
        )
        or 0
    )
