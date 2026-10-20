"""Dashboard, account details, statements and exports."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_account, render, verify_csrf
from ..models import Account, AccountStatus, AccountType, Card, CardStatus, Loan, LoanStatus, TxType
from ..services.banking import BankingError, create_account
from ..services import settings as rules
from ..services.queries import (
    account_transactions,
    balance_series,
    monthly_flow,
    spending_by_type,
    total_balance,
)
from ..utils import flash

router = APIRouter()


@router.get("/dashboard")
def dashboard(
    request: Request,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    accounts = db.scalars(
        select(Account).where(Account.customer_id == user.id, Account.status != AccountStatus.closed.value)
        .order_by(Account.opened_at)
    ).all()
    ids = [a.id for a in accounts]
    inflow, outflow = monthly_flow(db, ids) if ids else (0, 0)
    cards = db.scalars(
        select(Card).where(Card.customer_id == user.id, Card.status != CardStatus.cancelled.value)
    ).all()
    loans = db.scalars(
        select(Loan).where(
            Loan.customer_id == user.id,
            Loan.status.in_([LoanStatus.active.value, LoanStatus.pending.value]),
        )
    ).all()

    recent: list = []
    for account in accounts:
        rows, _ = account_transactions(db, account, limit=6)
        for row in rows:
            row.account = account  # type: ignore[attr-defined]
            recent.append(row)
    recent.sort(key=lambda r: r.transaction.created_at, reverse=True)

    return render(
        request,
        "dashboard.html",
        title="Dashboard",
        accounts=accounts,
        total=total_balance(db, user.id),
        inflow=inflow,
        outflow=outflow,
        series=balance_series(db, ids, 14) if ids else [],
        spending=spending_by_type(db, ids) if ids else [],
        recent=recent[:8],
        cards=cards,
        loans=loans,
        currency=rules.get_value(db, "money.default_currency") or "USD",
    )


@router.get("/accounts/new")
def new_account_page(request: Request, user=Depends(current_user)):
    return render(request, "account_new.html", title="Open an account")


@router.post("/accounts/new")
def new_account(
    request: Request,
    type: str = Form("savings"),
    label: str = Form(""),
    currency: str = Form("USD"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    if type not in (AccountType.checking.value, AccountType.savings.value):
        flash(request, "error", "Unknown account type.")
        return RedirectResponse("/accounts/new", status_code=303)

    count = len([a for a in user.accounts if a.status != AccountStatus.closed.value])
    max_accounts = rules.get_int(db, "accounts.max_per_customer", 6)
    if count >= max_accounts:
        flash(request, "error", f"You can hold at most {max_accounts} open accounts.")
        return RedirectResponse("/dashboard", status_code=303)

    account = create_account(
        db,
        user,
        account_type=type,
        label=label.strip() or ("Savings" if type == "savings" else "Checking"),
        currency=currency if currency in ("USD", "EUR", "GBP") else "USD",
        interest_bps=rules.get_int(db, "accounts.savings_interest_bps", 350) if type == "savings" else 0,
        overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000) if type == "checking" else 0,
    )
    db.commit()
    flash(request, "success", f"New {type} account opened — {account.account_number}.")
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@router.get("/accounts/{account_id}")
def account_detail(
    account_id: int,
    request: Request,
    q: str = Query(""),
    kind: str = Query(""),
    page: int = Query(1, ge=1),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(account_id, user, db)
    page_size = 15
    rows, total = account_transactions(
        db, account, limit=page_size, offset=(page - 1) * page_size, search=q, kind=kind
    )
    cards = db.scalars(select(Card).where(Card.account_id == account.id, Card.status != CardStatus.cancelled.value)).all()
    return render(
        request,
        "account.html",
        title=account.label,
        account=account,
        rows=rows,
        total=total,
        page=page,
        pages=max(1, (total + page_size - 1) // page_size),
        q=q,
        kind=kind,
        cards=cards,
        types=[t.value for t in TxType],
    )


@router.get("/accounts/{account_id}/export.csv")
def export_csv(
    account_id: int,
    request: Request,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(account_id, user, db)
    rows, _ = account_transactions(db, account, limit=10_000)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Reference", "Type", "Counterparty", "Description", "Amount", "Balance", "Currency"])
    for row in rows:
        writer.writerow(
            [
                row.transaction.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                row.transaction.reference,
                row.transaction.type,
                row.counterparty,
                row.transaction.description,
                f"{row.signed / 100:.2f}",
                f"{row.entry.balance_after_cents / 100:.2f}",
                row.transaction.currency,
            ]
        )
    buffer.seek(0)
    filename = f"statement-{account.account_number}-{datetime.utcnow():%Y%m%d}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/accounts/{account_id}/statement")
def statement(
    account_id: int,
    request: Request,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(account_id, user, db)
    rows, _ = account_transactions(db, account, limit=500)
    opening = rows[-1].entry.balance_after_cents if rows else account.balance_cents
    return render(
        request,
        "statement.html",
        title=f"Statement {account.label}",
        account=account,
        rows=rows,
        opening=opening,
        closing=account.balance_cents,
        generated=datetime.utcnow(),
    )


@router.post("/accounts/{account_id}/deposit")
def deposit_money(
    account_id: int,
    request: Request,
    amount: str = Form("0"),
    note: str = Form("Cash deposit"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    from ..services.banking import deposit

    account = owned_account(account_id, user, db)
    max_cents = rules.get_int(db, "money.max_cash_cents", 10_000_000)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
        if not 1 <= cents <= max_cents:
            raise ValueError
    except ValueError:
        flash(request, "error", f"Enter a valid amount between 0.01 and {max_cents / 100:,.2f}.")
        return RedirectResponse(f"/accounts/{account.id}", status_code=303)
    try:
        deposit(db, actor=user, account=account, amount_cents=cents, note=note.strip() or "Cash deposit")
        db.commit()
        flash(request, "success", f"Deposited {cents / 100:,.2f} {account.currency}.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@router.post("/accounts/{account_id}/withdraw")
def withdraw_money(
    account_id: int,
    request: Request,
    amount: str = Form("0"),
    note: str = Form("ATM withdrawal"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    from ..services.banking import withdraw

    account = owned_account(account_id, user, db)
    max_cents = rules.get_int(db, "money.max_cash_cents", 10_000_000)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
        if not 1 <= cents <= max_cents:
            raise ValueError
    except ValueError:
        flash(request, "error", f"Enter a valid amount between 0.01 and {max_cents / 100:,.2f}.")
        return RedirectResponse(f"/accounts/{account.id}", status_code=303)
    try:
        withdraw(db, actor=user, account=account, amount_cents=cents, note=note.strip() or "ATM withdrawal")
        db.commit()
        flash(request, "success", f"Withdrew {cents / 100:,.2f} {account.currency}.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)
