"""Administrator console: customers, loan approvals, audit trail, batch jobs."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import admin_user, render, verify_csrf
from ..models import (
    Account,
    AuditLog,
    Customer,
    CustomerStatus,
    KycStatus,
    Loan,
    LoanStatus,
    Transaction,
)
from ..services.banking import BankingError, approve_loan, audit, reject_loan, run_daily_jobs
from ..utils import flash

router = APIRouter()


@router.get("/admin")
def admin_home(
    request: Request,
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    customers = db.scalar(select(func.count(Customer.id))) or 0
    accounts = db.scalar(select(func.count(Account.id))) or 0
    deposits = db.scalar(select(func.coalesce(func.sum(Account.balance_cents), 0))) or 0
    tx_today = db.scalar(
        select(func.count(Transaction.id)).where(Transaction.created_at >= datetime.utcnow().date())
    ) or 0
    pending_loans = db.scalars(
        select(Loan).where(Loan.status == LoanStatus.pending.value).order_by(Loan.created_at)
    ).all()
    pending_kyc = db.scalars(
        select(Customer).where(Customer.kyc_status == KycStatus.pending.value).limit(10)
    ).all()
    recent_audit = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(12)).all()
    return render(
        request,
        "admin.html",
        title="Admin console",
        stats={"customers": customers, "accounts": accounts, "deposits": deposits, "tx_today": tx_today},
        pending_loans=pending_loans,
        pending_kyc=pending_kyc,
        recent_audit=recent_audit,
    )


@router.get("/admin/customers")
def admin_customers(
    request: Request,
    q: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Customer).order_by(Customer.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Customer.email.ilike(like), Customer.first_name.ilike(like), Customer.last_name.ilike(like)))
    customers = db.scalars(stmt.limit(100)).all()
    return render(request, "admin_customers.html", title="Customers", customers=customers, q=q)


@router.post("/admin/customers/{customer_id}/status")
def set_customer_status(
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
        audit(db, admin, f"admin.customer_{action}", "customer", customer.id)
        db.commit()
        flash(request, "success", f"{customer.full_name} {'locked' if action == 'lock' else 'unlocked'}.")
    return RedirectResponse("/admin/customers", status_code=303)


@router.post("/admin/customers/{customer_id}/kyc")
def set_kyc(
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
    elif decision not in (KycStatus.verified.value, KycStatus.rejected.value):
        flash(request, "error", "Unknown decision.")
    else:
        customer.kyc_status = decision
        audit(db, admin, f"admin.kyc_{decision}", "customer", customer.id)
        db.commit()
        flash(request, "success", f"KYC for {customer.full_name} set to {decision}.")
    return RedirectResponse("/admin/customers", status_code=303)


@router.get("/admin/loans")
def admin_loans(request: Request, status: str = Query(""), admin=Depends(admin_user), db: Session = Depends(get_db)):
    stmt = select(Loan).order_by(Loan.created_at.desc())
    if status:
        stmt = stmt.where(Loan.status == status)
    loans = db.scalars(stmt.limit(200)).all()
    return render(request, "admin_loans.html", title="Loan applications", loans=loans, status=status)


@router.post("/admin/loans/{loan_id}/{decision}")
def decide_loan(
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
        return RedirectResponse("/admin/loans", status_code=303)
    try:
        if decision == "approve":
            approve_loan(db, loan=loan, approver=admin)
            flash(request, "success", f"Loan #{loan.id} approved and disbursed.")
        elif decision == "reject":
            reject_loan(db, loan=loan, approver=admin)
            flash(request, "success", f"Loan #{loan.id} rejected.")
        else:
            flash(request, "error", "Unknown action.")
            return RedirectResponse("/admin/loans", status_code=303)
        db.commit()
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse("/admin/loans", status_code=303)


@router.get("/admin/transactions")
def admin_transactions(
    request: Request,
    q: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(Transaction).order_by(Transaction.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Transaction.reference.ilike(like), Transaction.description.ilike(like)))
    txs = db.scalars(stmt.limit(200)).all()
    return render(request, "admin_transactions.html", title="Transactions", txs=txs, q=q)


@router.get("/admin/audit")
def admin_audit(request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    logs = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(300)).all()
    return render(request, "admin_audit.html", title="Audit trail", logs=logs)


@router.post("/admin/run-jobs")
def run_jobs(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stats = run_daily_jobs(db, actor=admin)
    db.commit()
    flash(
        request,
        "success",
        f"Batch run complete — {stats['interest_paid']} interest postings, "
        f"{stats['overdue']} instalments marked overdue, {stats['scheduled_run']} scheduled transfers executed.",
    )
    return RedirectResponse("/admin", status_code=303)
