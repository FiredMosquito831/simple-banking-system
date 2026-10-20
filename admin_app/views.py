"""Dashboard, customer management and account operations."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import (
    Account,
    AccountStatus,
    AccountType,
    AuditLog,
    Card,
    CardStatus,
    Customer,
    CustomerStatus,
    KycStatus,
    Loan,
    LoanStatus,
    Role,
    Transaction,
    TxType,
)
from app.security import hash_secret
from app.services import settings as rules
from app.services.banking import BankingError, audit, create_account, notify, post_transaction
from app.utils import flash

from .deps import admin_user, client_ip, render, verify_csrf

router = APIRouter()

PAGE_SIZE = 25


def _page(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        return 1


@router.get("/")
def dashboard(request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    stats = {
        "customers": db.scalar(select(func.count(Customer.id))) or 0,
        "accounts": db.scalar(select(func.count(Account.id))) or 0,
        "deposits": db.scalar(select(func.coalesce(func.sum(Account.balance_cents), 0))) or 0,
        "tx_today": db.scalar(
            select(func.count(Transaction.id)).where(Transaction.created_at >= datetime.utcnow().date())
        ) or 0,
        "pending_loans": db.scalar(select(func.count(Loan.id)).where(Loan.status == LoanStatus.pending.value)) or 0,
        "pending_kyc": db.scalar(
            select(func.count(Customer.id)).where(Customer.kyc_status == KycStatus.pending.value)
        ) or 0,
        "active_cards": db.scalar(select(func.count(Card.id)).where(Card.status == CardStatus.active.value)) or 0,
        "frozen_accounts": db.scalar(
            select(func.count(Account.id)).where(Account.status != AccountStatus.active.value)
        ) or 0,
    }
    pending_loans = db.scalars(
        select(Loan).where(Loan.status == LoanStatus.pending.value).order_by(Loan.created_at).limit(6)
    ).all()
    recent_tx = db.scalars(select(Transaction).order_by(Transaction.created_at.desc()).limit(10)).all()
    recent_audit = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10)).all()
    maintenance = rules.get_bool(db, "ops.maintenance_mode", False)
    return render(
        request,
        "dashboard.html",
        title="Operations overview",
        stats=stats,
        pending_loans=pending_loans,
        recent_tx=recent_tx,
        recent_audit=recent_audit,
        maintenance=maintenance,
    )


# --- Customers -------------------------------------------------------------
@router.get("/customers")
def customers(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Customer).options(selectinload(Customer.accounts)).order_by(Customer.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Customer.email.ilike(like), Customer.first_name.ilike(like), Customer.last_name.ilike(like))
        )
    if status:
        stmt = stmt.where(Customer.status == status)
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return render(
        request,
        "customers.html",
        title="Customers",
        customers=rows,
        q=q,
        status=status,
        page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        total=total,
    )


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: int, request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
        return RedirectResponse("/customers", status_code=303)
    accounts = db.scalars(
        select(Account).where(Account.customer_id == customer.id).order_by(Account.opened_at)
    ).all()
    account_ids = [a.id for a in accounts]
    cards = db.scalars(select(Card).where(Card.customer_id == customer.id).order_by(Card.created_at.desc())).all()
    loans = db.scalars(select(Loan).where(Loan.customer_id == customer.id).order_by(Loan.created_at.desc())).all()
    entries = []
    if account_ids:
        from app.models import LedgerEntry

        entries = db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.account_id.in_(account_ids))
            .order_by(LedgerEntry.created_at.desc())
            .limit(12)
        ).all()
    return render(
        request,
        "customer_detail.html",
        title=customer.full_name,
        customer=customer,
        accounts=accounts,
        cards=cards,
        loans=loans,
        entries=entries,
    )


@router.post("/customers/create")
def create_customer(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    password: str = Form(""),
    kyc: str = Form("pending"),
    role: str = Form("customer"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if not email or db.scalar(select(Customer.id).where(Customer.email == email)):
        flash(request, "error", "A customer with that email already exists.")
        return RedirectResponse("/customers", status_code=303)
    if len(password) < 8:
        flash(request, "error", "Password must be at least 8 characters.")
        return RedirectResponse("/customers", status_code=303)
    if not first_name.strip() or not last_name.strip():
        flash(request, "error", "First and last name are required.")
        return RedirectResponse("/customers", status_code=303)

    customer = Customer(
        email=email,
        password_hash=hash_secret(password),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip() or None,
        address=address.strip() or None,
        kyc_status=kyc if kyc in [k.value for k in KycStatus] else KycStatus.pending.value,
        role=role if role in [r.value for r in Role] else Role.customer.value,
    )
    db.add(customer)
    db.flush()
    create_account(
        db, customer, "checking", "Everyday checking",
        opening_cents=rules.get_int(db, "accounts.opening_credit_cents", 50_000),
        overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000),
    )
    audit(db, admin, "admin.customer_create", "customer", customer.id, email, client_ip(request))
    db.commit()
    flash(request, "success", f"Customer {customer.full_name} created.")
    return RedirectResponse(f"/customers/{customer.id}", status_code=303)


@router.post("/customers/{customer_id}/update")
def update_customer(
    customer_id: int,
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    date_of_birth: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
        return RedirectResponse("/customers", status_code=303)

    changes: list[str] = []
    new_email = email.strip().lower()
    if new_email and new_email != customer.email:
        local, _, domain = new_email.partition("@")
        if not local or "." not in domain:
            flash(request, "error", "Enter a valid email address.")
            return RedirectResponse(f"/customers/{customer.id}", status_code=303)
        if db.scalar(select(Customer.id).where(Customer.email == new_email, Customer.id != customer.id)):
            flash(request, "error", "Another customer already uses that email.")
            return RedirectResponse(f"/customers/{customer.id}", status_code=303)
        changes.append(f"email: {customer.email} → {new_email}")
        customer.email = new_email

    if date_of_birth.strip():
        try:
            customer.date_of_birth = date.fromisoformat(date_of_birth.strip())
        except ValueError:
            flash(request, "error", "Date of birth must be a valid date.")
            return RedirectResponse(f"/customers/{customer.id}", status_code=303)

    customer.first_name = first_name.strip() or customer.first_name
    customer.last_name = last_name.strip() or customer.last_name
    customer.phone = phone.strip() or None
    customer.address = address.strip() or None
    audit(db, admin, "admin.customer_update", "customer", customer.id, "; ".join(changes), client_ip(request))
    db.commit()
    flash(request, "success", "Customer profile updated.")
    return RedirectResponse(f"/customers/{customer.id}", status_code=303)


@router.post("/customers/{customer_id}/status")
def customer_status(
    customer_id: int,
    request: Request,
    action: str = Form("lock"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
    elif customer.id == admin.id:
        flash(request, "error", "You cannot lock your own account.")
    else:
        customer.status = CustomerStatus.locked.value if action == "lock" else CustomerStatus.active.value
        audit(db, admin, f"admin.customer_{action}", "customer", customer.id, "", client_ip(request))
        db.commit()
        flash(request, "success", f"{customer.full_name} {'locked' if action == 'lock' else 'reactivated'}.")
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@router.post("/customers/{customer_id}/kyc")
def customer_kyc(
    customer_id: int,
    request: Request,
    decision: str = Form("verified"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
    elif decision not in [k.value for k in KycStatus]:
        flash(request, "error", "Unknown KYC decision.")
    else:
        customer.kyc_status = decision
        audit(db, admin, f"admin.kyc_{decision}", "customer", customer.id, "", client_ip(request))
        db.commit()
        flash(request, "success", f"KYC set to {decision}.")
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@router.post("/customers/{customer_id}/role")
def customer_role(
    customer_id: int,
    request: Request,
    role: str = Form("customer"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
    elif customer.id == admin.id:
        flash(request, "error", "You cannot change your own role.")
    elif role not in [r.value for r in Role]:
        flash(request, "error", "Unknown role.")
    else:
        customer.role = role
        audit(db, admin, "admin.role_change", "customer", customer.id, f"role={role}", client_ip(request))
        db.commit()
        flash(request, "success", f"Role set to {role}.")
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@router.post("/customers/{customer_id}/password")
def customer_password(
    customer_id: int,
    request: Request,
    new_password: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
    elif len(new_password) < 8:
        flash(request, "error", "Password must be at least 8 characters.")
    else:
        customer.password_hash = hash_secret(new_password)
        notify(db, customer.id, "Password reset by support", "An operator reset your online banking password.")
        audit(db, admin, "admin.password_reset", "customer", customer.id, "", client_ip(request))
        db.commit()
        flash(request, "success", f"Password reset for {customer.email}.")
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


# --- Accounts --------------------------------------------------------------
@router.get("/accounts")
def accounts(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Account).options(selectinload(Account.customer)).order_by(Account.opened_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.join(Customer).where(
            or_(Account.account_number.ilike(like), Account.label.ilike(like), Customer.email.ilike(like))
        )
    if status:
        stmt = stmt.where(Account.status == status)
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return render(
        request, "accounts.html", title="Accounts", accounts=rows, q=q, status=status,
        page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
    )


@router.get("/accounts/{account_id}")
def account_detail(account_id: int, request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        flash(request, "error", "Account not found.")
        return RedirectResponse("/accounts", status_code=303)
    from app.models import LedgerEntry

    entries = db.scalars(
        select(LedgerEntry).where(LedgerEntry.account_id == account.id)
        .order_by(LedgerEntry.created_at.desc()).limit(50)
    ).all()
    cards = db.scalars(select(Card).where(Card.account_id == account.id)).all()
    return render(request, "account_detail.html", title=f"Account {account.account_number}",
                  account=account, entries=entries, cards=cards)


@router.post("/accounts/create")
def account_create(
    request: Request,
    customer_id: int = Form(...),
    type: str = Form("checking"),
    label: str = Form(""),
    currency: str = Form("USD"),
    opening: str = Form("0"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if not customer:
        flash(request, "error", "Customer not found.")
        return RedirectResponse("/accounts", status_code=303)
    try:
        opening_cents = max(0, int(round(float(opening.replace(",", "")) * 100)))
    except ValueError:
        opening_cents = 0
    account = create_account(
        db, customer, type if type in [t.value for t in AccountType] else "checking",
        label.strip() or ("Savings" if type == "savings" else "Checking"),
        opening_cents=opening_cents,
        currency=currency if currency in ("USD", "EUR", "GBP") else "USD",
        interest_bps=rules.get_int(db, "accounts.savings_interest_bps", 350) if type == "savings" else 0,
        overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000) if type == "checking" else 0,
    )
    audit(db, admin, "admin.account_create", "account", account.id, account.account_number, client_ip(request))
    db.commit()
    flash(request, "success", f"Account {account.account_number} opened.")
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@router.post("/accounts/{account_id}/status")
def account_status(
    account_id: int,
    request: Request,
    action: str = Form("freeze"),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if not account:
        flash(request, "error", "Account not found.")
        return RedirectResponse("/accounts", status_code=303)
    if action == "freeze":
        account.status = AccountStatus.frozen.value
    elif action == "activate":
        account.status = AccountStatus.active.value
    elif action == "close":
        if account.balance_cents != 0:
            flash(request, "error", "Move the remaining balance before closing the account.")
            return RedirectResponse(f"/accounts/{account.id}", status_code=303)
        account.status = AccountStatus.closed.value
        account.closed_at = datetime.utcnow()
    audit(db, admin, f"admin.account_{action}", "account", account.id, account.account_number, client_ip(request))
    db.commit()
    flash(request, "success", f"Account {account.account_number} set to {account.status}.")
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@router.post("/accounts/{account_id}/adjust")
def account_adjust(
    account_id: int,
    request: Request,
    direction: str = Form("credit"),
    amount: str = Form("0"),
    reason: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if not account:
        flash(request, "error", "Account not found.")
        return RedirectResponse("/accounts", status_code=303)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
        if cents <= 0:
            raise ValueError
    except ValueError:
        flash(request, "error", "Enter a positive amount.")
        return RedirectResponse(f"/accounts/{account.id}", status_code=303)
    signed = cents if direction == "credit" else -cents
    try:
        post_transaction(
            db,
            tx_type=TxType.adjustment,
            amount_cents=cents,
            currency=account.currency,
            description=f"Operator {direction} — {reason.strip() or 'manual adjustment'}",
            entries=[(account, signed)],
            actor=admin,
        )
        notify(db, account.customer_id,
               f"Balance adjustment on {account.label}",
               f"{'+' if signed > 0 else '−'}{cents / 100:,.2f} {account.currency} — {reason.strip() or 'manual adjustment'}")
        db.commit()
        flash(request, "success", f"Adjusted {account.account_number} by {signed / 100:,.2f} {account.currency}.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)
