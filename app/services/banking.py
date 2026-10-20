"""Core banking operations.

All balance changes go through `post_transaction`, which:
  * locks the accounts involved (`SELECT ... FOR UPDATE`) in a deterministic order,
  * validates account state and available funds,
  * writes one Transaction and one signed LedgerEntry per account, and
  * records the running balance on each entry.

That keeps concurrent transfers safe and every balance reconstructable.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Account,
    AccountStatus,
    AuditLog,
    Card,
    CardStatus,
    Customer,
    InstallmentStatus,
    LedgerEntry,
    Loan,
    LoanInstallment,
    LoanStatus,
    Notification,
    ScheduledStatus,
    ScheduledTransfer,
    Transaction,
    TxStatus,
    TxType,
    utcnow,
)
from ..security import generate_account_number


class BankingError(Exception):
    """A business-rule failure safe to show to the user."""


def audit(
    db: Session,
    actor: Customer | None,
    action: str,
    entity_type: str = "",
    entity_id: str | int = "",
    details: str = "",
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            actor_label=(actor.email if actor else "system"),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=details,
            ip=ip,
        )
    )


def notify(db: Session, customer_id: int, title: str, body: str = "") -> None:
    db.add(Notification(customer_id=customer_id, title=title, body=body))


def create_account(
    db: Session,
    customer: Customer,
    account_type: str = "checking",
    label: str = "Main account",
    opening_cents: int = 0,
    currency: str = "USD",
    interest_bps: int = 0,
    overdraft_cents: int = 0,
) -> Account:
    for _ in range(5):
        number = generate_account_number()
        if not db.scalar(select(Account.id).where(Account.account_number == number)):
            break
    else:  # pragma: no cover
        raise BankingError("Could not allocate an account number, please retry.")

    account = Account(
        customer_id=customer.id,
        account_number=number,
        label=label,
        type=account_type,
        currency=currency,
        interest_rate_bps=interest_bps,
        overdraft_limit_cents=overdraft_cents,
        balance_cents=0,
    )
    db.add(account)
    db.flush()
    if opening_cents:
        post_transaction(
            db,
            tx_type=TxType.deposit,
            amount_cents=opening_cents,
            currency=currency,
            description="Opening deposit",
            entries=[(account, opening_cents)],
            actor=customer,
        )
    return account


def _lock_accounts(db: Session, account_ids: list[int]) -> dict[int, Account]:
    ids = sorted(set(account_ids))
    rows = db.scalars(select(Account).where(Account.id.in_(ids)).with_for_update()).all()
    found = {a.id: a for a in rows}
    missing = set(ids) - set(found)
    if missing:
        raise BankingError("One of the accounts no longer exists.")
    return found


def post_transaction(
    db: Session,
    *,
    tx_type: TxType | str,
    amount_cents: int,
    entries: list[tuple[Account, int]],
    currency: str = "USD",
    description: str = "",
    actor: Customer | None = None,
    idempotency_key: str | None = None,
    status: TxStatus = TxStatus.completed,
) -> Transaction:
    """Post one balanced transaction. `entries` are (account, signed minor units)."""
    if amount_cents <= 0:
        raise BankingError("Amount must be greater than zero.")
    if not entries:
        raise BankingError("A transaction needs at least one account.")

    if idempotency_key:
        existing = db.scalar(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
        if existing:
            return existing

    locked = _lock_accounts(db, [a.id for a, _ in entries])

    for account, delta in entries:
        account = locked[account.id]
        if not account.is_active:
            raise BankingError(f"Account {account.account_number} is not active.")
        if account.currency != currency:
            raise BankingError("Currency mismatch between accounts.")
        new_balance = account.balance_cents + delta
        if new_balance < -account.overdraft_limit_cents:
            raise BankingError("Insufficient funds for this transaction.")
    if len(entries) > 1 and sum(delta for _, delta in entries) != 0:
        raise BankingError("Internal error: transaction does not balance.")

    tx = Transaction(
        type=tx_type.value if isinstance(tx_type, TxType) else tx_type,
        status=status.value if isinstance(status, TxStatus) else status,
        description=description,
        amount_cents=amount_cents,
        currency=currency,
        initiated_by=actor.id if actor else None,
        idempotency_key=idempotency_key,
    )
    db.add(tx)
    db.flush()

    for account_ref, delta in entries:
        account = locked[account_ref.id]
        account.balance_cents += delta
        db.add(
            LedgerEntry(
                transaction_id=tx.id,
                account_id=account.id,
                amount_cents=delta,
                balance_after_cents=account.balance_cents,
            )
        )
    db.flush()
    audit(
        db,
        actor,
        f"transaction.{tx.type}",
        "transaction",
        tx.reference,
        f"{amount_cents / 100:.2f} {currency} — {description}",
    )
    return tx


def get_account_by_number(db: Session, number: str) -> Account | None:
    return db.scalar(select(Account).where(Account.account_number == number.strip().replace(" ", "")))


def transfer(
    db: Session,
    *,
    actor: Customer | None,
    from_account_id: int,
    to_account_number: str,
    amount_cents: int,
    description: str = "",
    idempotency_key: str | None = None,
) -> Transaction:
    from_account = db.get(Account, from_account_id)
    if not from_account or (actor is not None and from_account.customer_id != actor.id):
        raise BankingError("You can only transfer from your own account.")
    if from_account.status != AccountStatus.active.value:
        raise BankingError("This account is not available for transfers.")

    target = get_account_by_number(db, to_account_number)
    if not target:
        raise BankingError("Recipient account was not found in this bank.")
    if target.id == from_account.id:
        raise BankingError("Choose a different destination account.")
    if target.currency != from_account.currency:
        raise BankingError("Cross-currency transfers are not supported in this demo.")

    tx = post_transaction(
        db,
        tx_type=TxType.transfer,
        amount_cents=amount_cents,
        currency=from_account.currency,
        description=description or f"Transfer to {target.customer.full_name}",
        entries=[(from_account, -amount_cents), (target, amount_cents)],
        actor=actor,
        idempotency_key=idempotency_key,
    )
    notify(
        db,
        target.customer_id,
        f"Received {amount_cents / 100:,.2f} {target.currency}",
        f"{actor.full_name if actor else 'A scheduled transfer'} sent you money. Ref {tx.reference}",
    )
    return tx


def deposit(db: Session, *, actor: Customer, account: Account, amount_cents: int, note: str = "") -> Transaction:
    if account.customer_id != actor.id:
        raise BankingError("You can only deposit into your own account.")
    return post_transaction(
        db,
        tx_type=TxType.deposit,
        amount_cents=amount_cents,
        currency=account.currency,
        description=note or "Cash deposit",
        entries=[(account, amount_cents)],
        actor=actor,
    )


def withdraw(db: Session, *, actor: Customer, account: Account, amount_cents: int, note: str = "") -> Transaction:
    if account.customer_id != actor.id:
        raise BankingError("You can only withdraw from your own account.")
    return post_transaction(
        db,
        tx_type=TxType.withdrawal,
        amount_cents=amount_cents,
        currency=account.currency,
        description=note or "ATM withdrawal",
        entries=[(account, -amount_cents)],
        actor=actor,
    )


def card_payment(
    db: Session, *, actor: Customer, card: Card, amount_cents: int, merchant: str
) -> Transaction:
    if card.customer_id != actor.id:
        raise BankingError("This card does not belong to you.")
    if card.status != CardStatus.active.value:
        raise BankingError("This card is not active.")
    if amount_cents <= 0:
        raise BankingError("Amount must be greater than zero.")

    today = utcnow().date()
    if card.spent_on != today:
        card.spent_on = today
        card.spent_today_cents = 0
    if card.spent_today_cents + amount_cents > card.daily_limit_cents:
        raise BankingError(
            f"Daily card limit exceeded (limit {card.daily_limit_cents / 100:,.2f})."
        )

    tx = post_transaction(
        db,
        tx_type=TxType.card_payment,
        amount_cents=amount_cents,
        currency=card.account.currency,
        description=f"Card purchase — {merchant}",
        entries=[(card.account, -amount_cents)],
        actor=actor,
    )
    card.spent_today_cents += amount_cents
    return tx


# --- Loans -----------------------------------------------------------------
def annuity_payment(principal_cents: int, annual_rate_bps: int, months: int) -> int:
    """Fixed monthly payment (annuity), rounded to the cent."""
    r = annual_rate_bps / 10_000 / 12
    if r == 0:
        return math.ceil(principal_cents / months)
    factor = (1 + r) ** months
    return math.ceil(principal_cents * r * factor / (factor - 1))


def quote_loan(principal_cents: int, annual_rate_bps: int, months: int) -> dict[str, int]:
    payment = annuity_payment(principal_cents, annual_rate_bps, months)
    return {
        "monthly_payment_cents": payment,
        "total_repayable_cents": payment * months,
        "total_interest_cents": payment * months - principal_cents,
    }


def build_schedule(principal_cents: int, annual_rate_bps: int, months: int, start: datetime | None = None) -> list[dict]:
    r = annual_rate_bps / 10_000 / 12
    payment = annuity_payment(principal_cents, annual_rate_bps, months)
    outstanding = principal_cents
    start = start or utcnow()
    rows: list[dict] = []
    for n in range(1, months + 1):
        interest = round(outstanding * r)
        principal = payment - interest
        if n == months:
            principal = outstanding
            payment_n = principal + interest
        else:
            payment_n = payment
        outstanding -= principal
        due = (start + timedelta(days=30 * n)).date()
        rows.append(
            {
                "number": n,
                "due_date": due,
                "principal_cents": principal,
                "interest_cents": interest,
            }
        )
    return rows


def apply_for_loan(
    db: Session,
    *,
    customer: Customer,
    account: Account,
    principal_cents: int,
    months: int,
    rate_bps: int,
    purpose: str,
) -> Loan:
    if account.customer_id != customer.id:
        raise BankingError("Disbursement account must belong to you.")
    if not 50_000 <= principal_cents <= 5_000_000:
        raise BankingError("Loan amount must be between 500 and 50,000.")
    if not 3 <= months <= 60:
        raise BankingError("Term must be between 3 and 60 months.")

    open_loans = db.scalar(
        select(Loan).where(
            Loan.customer_id == customer.id,
            Loan.status.in_([LoanStatus.pending.value, LoanStatus.active.value]),
        )
    )
    if open_loans:
        raise BankingError("You already have an open loan application or active loan.")

    quote = quote_loan(principal_cents, rate_bps, months)
    loan = Loan(
        customer_id=customer.id,
        account_id=account.id,
        principal_cents=principal_cents,
        outstanding_cents=0,
        annual_rate_bps=rate_bps,
        term_months=months,
        monthly_payment_cents=quote["monthly_payment_cents"],
        purpose=purpose,
        status=LoanStatus.pending.value,
    )
    db.add(loan)
    db.flush()
    notify(db, customer.id, "Loan application received", "We will review your application shortly.")
    audit(db, customer, "loan.apply", "loan", loan.id, f"{principal_cents / 100:.2f} over {months}m")
    return loan


def approve_loan(db: Session, *, loan: Loan, approver: Customer | None = None) -> Loan:
    if loan.status != LoanStatus.pending.value:
        raise BankingError("This loan is not pending approval.")
    account = db.get(Account, loan.account_id)
    if not account or not account.is_active:
        raise BankingError("Disbursement account is not active.")

    loan.status = LoanStatus.active.value
    loan.outstanding_cents = loan.principal_cents
    loan.approved_at = utcnow()

    start = utcnow()
    for row in build_schedule(loan.principal_cents, loan.annual_rate_bps, loan.term_months, start):
        db.add(LoanInstallment(loan_id=loan.id, **row))

    post_transaction(
        db,
        tx_type=TxType.loan_disbursement,
        amount_cents=loan.principal_cents,
        currency=account.currency,
        description=f"Loan #{loan.id} disbursement",
        entries=[(account, loan.principal_cents)],
        actor=approver,
    )
    notify(db, loan.customer_id, "Loan approved 🎉", f"Loan #{loan.id} was approved and disbursed to your account.")
    audit(db, approver, "loan.approve", "loan", loan.id, "disbursed")
    return loan


def reject_loan(db: Session, *, loan: Loan, approver: Customer | None = None) -> Loan:
    if loan.status != LoanStatus.pending.value:
        raise BankingError("This loan is not pending approval.")
    loan.status = LoanStatus.rejected.value
    notify(db, loan.customer_id, "Loan application declined", f"Loan #{loan.id} was not approved.")
    audit(db, approver, "loan.reject", "loan", loan.id)
    return loan


def repay_installment(db: Session, *, actor: Customer, loan: Loan, installment: LoanInstallment) -> Transaction:
    if loan.customer_id != actor.id:
        raise BankingError("This loan does not belong to you.")
    if loan.status != LoanStatus.active.value:
        raise BankingError("This loan is not active.")
    if installment.status == InstallmentStatus.paid.value:
        raise BankingError("This installment is already paid.")

    account = db.get(Account, loan.account_id)
    total = installment.total_cents
    tx = post_transaction(
        db,
        tx_type=TxType.loan_repayment,
        amount_cents=total,
        currency=account.currency,
        description=f"Loan #{loan.id} instalment {installment.number}",
        entries=[(account, -total)],
        actor=actor,
    )
    installment.status = InstallmentStatus.paid.value
    installment.paid_at = utcnow()
    loan.outstanding_cents = max(0, loan.outstanding_cents - installment.principal_cents)
    if loan.outstanding_cents == 0:
        loan.status = LoanStatus.paid_off.value
        notify(db, actor.id, "Loan paid off", f"Congratulations — loan #{loan.id} is fully repaid.")
    return tx


def repay_loan_in_full(db: Session, *, actor: Customer, loan: Loan) -> Transaction:
    if loan.customer_id != actor.id:
        raise BankingError("This loan does not belong to you.")
    if loan.status != LoanStatus.active.value:
        raise BankingError("This loan is not active.")
    account = db.get(Account, loan.account_id)
    amount = loan.outstanding_cents
    tx = post_transaction(
        db,
        tx_type=TxType.loan_repayment,
        amount_cents=amount,
        currency=account.currency,
        description=f"Early repayment — loan #{loan.id}",
        entries=[(account, -amount)],
        actor=actor,
    )
    loan.outstanding_cents = 0
    loan.status = LoanStatus.paid_off.value
    for inst in loan.installments:
        inst.status = InstallmentStatus.paid.value
    return tx


# --- Batch jobs ------------------------------------------------------------
def run_daily_jobs(db: Session, *, actor: Customer | None = None) -> dict[str, int]:
    """Apply monthly interest and mark missed instalments. Safe to run repeatedly per day."""
    today = utcnow()
    stats = {"interest_paid": 0, "overdue": 0, "scheduled_run": 0}

    savings = db.scalars(
        select(Account).where(
            Account.type == "savings",
            Account.status == AccountStatus.active.value,
            Account.interest_rate_bps > 0,
        )
    ).all()
    for account in savings:
        daily = account.balance_cents * (account.interest_rate_bps / 10_000) / 365
        cents = int(daily)
        if cents > 0:
            post_transaction(
                db,
                tx_type=TxType.interest,
                amount_cents=cents,
                currency=account.currency,
                description="Daily interest accrual",
                entries=[(account, cents)],
                actor=actor,
            )
            stats["interest_paid"] += 1

    overdue = db.scalars(
        select(LoanInstallment).where(
            LoanInstallment.status == InstallmentStatus.scheduled.value,
            LoanInstallment.due_date < today.date(),
        )
    ).all()
    for inst in overdue:
        inst.status = InstallmentStatus.overdue.value
        stats["overdue"] += 1

    due = db.scalars(
        select(ScheduledTransfer).where(
            ScheduledTransfer.status == ScheduledStatus.active.value,
            ScheduledTransfer.next_run_at <= today,
        )
    ).all()
    for plan in due:
        try:
            transfer(
                db,
                actor=None,
                from_account_id=plan.from_account_id,
                to_account_number=plan.to_account_number,
                amount_cents=plan.amount_cents,
                description=f"Scheduled transfer — {plan.to_name}",
            )
            stats["scheduled_run"] += 1
        except BankingError:
            plan.status = ScheduledStatus.paused.value
            notify(db, plan.customer_id, "Scheduled transfer paused", "It could not be executed (insufficient funds).")
            continue
        delta = {
            "daily": timedelta(days=1),
            "weekly": timedelta(days=7),
            "monthly": timedelta(days=30),
            "once": timedelta(days=1),
        }[plan.frequency]
        plan.next_run_at = plan.next_run_at + delta
        if plan.frequency == "once":
            plan.status = ScheduledStatus.completed.value

    db.flush()
    return stats
