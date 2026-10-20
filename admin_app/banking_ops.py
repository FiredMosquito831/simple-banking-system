"""Transaction control, card operations and loan decisions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import (
    Account,
    AccountStatus,
    Card,
    CardStatus,
    Customer,
    LedgerEntry,
    Loan,
    Transaction,
    TxType,
)
from app.security import decrypt_pan, encrypt_pan, generate_card_number, generate_pin, hash_secret
from app.services import settings as rules
from app.services.banking import (
    BankingError,
    apply_for_loan,
    approve_loan,
    audit,
    reject_loan,
    reverse_transaction,
)
from app.utils import flash

from .deps import admin_user, client_ip, render, verify_csrf

router = APIRouter()
PAGE_SIZE = 25


def _page(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        return 1


# --- Transactions ----------------------------------------------------------
@router.get("/transactions")
def transactions(
    request: Request,
    q: str = Query(""),
    type: str = Query(""),
    status: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Transaction).order_by(Transaction.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Transaction.reference.ilike(like), Transaction.description.ilike(like)))
    if type:
        stmt = stmt.where(Transaction.type == type)
    if status:
        stmt = stmt.where(Transaction.status == status)
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return render(
        request, "transactions.html", title="Transactions", txs=rows, q=q, type=type, status=status,
        page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
        types=[t.value for t in TxType],
    )


@router.get("/transactions/{reference}")
def transaction_detail(reference: str, request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    tx = db.scalar(
        select(Transaction)
        .options(selectinload(Transaction.entries).selectinload(LedgerEntry.account).selectinload(Account.customer))
        .where(Transaction.reference == reference)
    )
    if not tx:
        flash(request, "error", "Transaction not found.")
        return RedirectResponse("/transactions", status_code=303)
    reversal = db.scalar(select(Transaction).where(Transaction.reversal_of_id == tx.id))
    return render(request, "transaction_detail.html", title=tx.reference, tx=tx, reversal=reversal)


@router.post("/transactions/{reference}/reverse")
def reverse(
    reference: str,
    request: Request,
    reason: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    tx = db.scalar(select(Transaction).where(Transaction.reference == reference))
    if not tx:
        flash(request, "error", "Transaction not found.")
        return RedirectResponse("/transactions", status_code=303)
    try:
        reversal = reverse_transaction(db, transaction=tx, actor=admin, reason=reason.strip())
        db.commit()
        flash(request, "success", f"Reversed {reference} with {reversal.reference}.")
        return RedirectResponse(f"/transactions/{reversal.reference}", status_code=303)
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/transactions/{reference}", status_code=303)


# --- Cards -----------------------------------------------------------------
@router.get("/cards")
def cards(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Card)
        .options(selectinload(Card.account).selectinload(Account.customer))
        .order_by(Card.created_at.desc())
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Card.last4.ilike(like), Card.account.has(Account.account_number.ilike(like))))
    if status:
        stmt = stmt.where(Card.status == status)
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    accounts = db.scalars(
        select(Account).options(selectinload(Account.customer))
        .where(Account.status == AccountStatus.active.value).order_by(Account.opened_at.desc()).limit(200)
    ).all()
    return render(
        request, "cards.html", title="Cards", cards=rows, accounts=accounts, q=q, status=status,
        page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
        max_limit=rules.get_int(db, "cards.max_daily_limit_cents", 1_000_000),
    )


@router.post("/cards/issue")
def issue_card(
    request: Request,
    account_id: int = Form(...),
    pin: str = Form(""),
    daily_limit: str = Form("2500"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if not account:
        flash(request, "error", "Account not found.")
        return RedirectResponse("/cards", status_code=303)
    if account.status != AccountStatus.active.value:
        flash(request, "error", "Account is not active.")
        return RedirectResponse("/cards", status_code=303)

    if pin and not (pin.isdigit() and len(pin) == 4):
        flash(request, "error", "PIN must be 4 digits.")
        return RedirectResponse("/cards", status_code=303)
    pin = pin or generate_pin()
    try:
        limit_cents = int(round(float(daily_limit.replace(",", "")) * 100))
    except ValueError:
        limit_cents = rules.get_int(db, "cards.default_daily_limit_cents", 250_000)
    limit_cents = max(1_000, min(limit_cents, rules.get_int(db, "cards.max_daily_limit_cents", 1_000_000)))

    pan = generate_card_number()
    from datetime import date

    today = date.today()
    card = Card(
        account_id=account.id,
        customer_id=account.customer_id,
        pan_encrypted=encrypt_pan(pan),
        last4=pan[-4:],
        brand="Visa",
        exp_month=today.month,
        exp_year=today.year + 4,
        pin_hash=hash_secret(pin),
        daily_limit_cents=limit_cents,
    )
    db.add(card)
    audit(db, admin, "admin.card_issue", "card", card.last4, account.account_number, client_ip(request))
    db.commit()
    flash(request, "success", f"Card issued ending {pan[-4:]} — PIN {pin}.")
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/{action}")
def card_action(
    card_id: int,
    action: str,
    request: Request,
    daily_limit: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    card = db.get(Card, card_id)
    if not card:
        flash(request, "error", "Card not found.")
        return RedirectResponse("/cards", status_code=303)
    if action == "freeze":
        card.status = CardStatus.frozen.value
    elif action == "activate":
        card.status = CardStatus.active.value
    elif action == "cancel":
        card.status = CardStatus.cancelled.value
    elif action == "limit" and daily_limit:
        try:
            cents = int(round(float(daily_limit.replace(",", "")) * 100))
            card.daily_limit_cents = max(1_000, min(cents, rules.get_int(db, "cards.max_daily_limit_cents", 1_000_000)))
        except ValueError:
            flash(request, "error", "Enter a valid limit.")
            return RedirectResponse("/cards", status_code=303)
    elif action == "reset-pin":
        pin = generate_pin()
        card.pin_hash = hash_secret(pin)
        audit(db, admin, "admin.card_pin_reset", "card", card.last4, "", client_ip(request))
        db.commit()
        flash(request, "success", f"New PIN for card {card.last4}: {pin}")
        return RedirectResponse("/cards", status_code=303)
    audit(db, admin, f"admin.card_{action}", "card", card.last4, "", client_ip(request))
    db.commit()
    flash(request, "success", f"Card {card.last4} updated.")
    return RedirectResponse("/cards", status_code=303)


@router.get("/cards/{card_id}/reveal")
def reveal_card(card_id: int, request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    card = db.get(Card, card_id)
    if not card:
        return JSONResponse({"detail": "Card not found."}, status_code=404)
    audit(db, admin, "admin.card_reveal", "card", card.last4, "", client_ip(request))
    db.commit()
    return JSONResponse({"pan": decrypt_pan(card.pan_encrypted), "expiry": card.expiry, "last4": card.last4})


# --- Loans -----------------------------------------------------------------
@router.get("/loans")
def loans(
    request: Request,
    status: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Loan).options(selectinload(Loan.customer)).order_by(Loan.created_at.desc())
    if status:
        stmt = stmt.where(Loan.status == status)
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    customers = db.scalars(select(Customer).order_by(Customer.first_name).limit(300)).all()
    accounts = db.scalars(
        select(Account).options(selectinload(Account.customer))
        .where(Account.status == AccountStatus.active.value, Account.type == "checking")
        .order_by(Account.opened_at.desc()).limit(300)
    ).all()
    return render(
        request, "loans.html", title="Loans", loans=rows, status=status, customers=customers,
        accounts=accounts, page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
        default_rate=rules.get_int(db, "loans.base_rate_bps", 1_290),
        min_amount=rules.get_int(db, "loans.min_cents", 50_000),
        max_amount=rules.get_int(db, "loans.max_cents", 5_000_000),
    )


@router.post("/loans/{loan_id}/{decision}")
def loan_decision(
    loan_id: int,
    decision: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    loan = db.get(Loan, loan_id)
    if not loan:
        flash(request, "error", "Loan not found.")
        return RedirectResponse("/loans", status_code=303)
    try:
        if decision == "approve":
            approve_loan(db, loan=loan, approver=admin)
            flash(request, "success", f"Loan #{loan.id} approved and disbursed.")
        elif decision == "reject":
            reject_loan(db, loan=loan, approver=admin)
            flash(request, "success", f"Loan #{loan.id} rejected.")
        else:
            flash(request, "error", "Unknown decision.")
            return RedirectResponse("/loans", status_code=303)
        db.commit()
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse("/loans", status_code=303)


@router.post("/loans/create")
def create_loan(
    request: Request,
    customer_id: int = Form(...),
    account_id: int = Form(...),
    amount: str = Form("0"),
    months: int = Form(24),
    rate_bps: int = Form(0),
    purpose: str = Form(""),
    auto_approve: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    account = db.get(Account, account_id)
    if not customer or not account or account.customer_id != customer.id:
        flash(request, "error", "Choose a customer and one of their accounts.")
        return RedirectResponse("/loans", status_code=303)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
    except ValueError:
        flash(request, "error", "Enter a valid amount.")
        return RedirectResponse("/loans", status_code=303)
    rate = rate_bps or rules.get_int(db, "loans.base_rate_bps", 1_290)
    try:
        loan = apply_for_loan(
            db, customer=customer, account=account, principal_cents=cents,
            months=months, rate_bps=rate, purpose=purpose.strip() or "Created by operator",
        )
        if auto_approve:
            approve_loan(db, loan=loan, approver=admin)
        db.commit()
        flash(request, "success", f"Loan #{loan.id} created{' and disbursed' if auto_approve else ' (pending)'}.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse("/loans", status_code=303)
