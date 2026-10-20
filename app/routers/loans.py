"""Loans: quoting, applications, schedules and repayment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, render, verify_csrf
from ..models import Account, AccountStatus, Customer, KycStatus, Loan, LoanInstallment
from ..services import settings as rules
from ..services.banking import (
    BankingError,
    apply_for_loan,
    quote_loan,
    repay_installment,
    repay_loan_in_full,
)
from ..utils import flash

router = APIRouter()


def rate_for(db: Session, customer: Customer, amount_cents: int) -> int:
    """Risk pricing driven by the settings table: base rate, KYC and size discounts."""
    bps = rules.get_int(db, "loans.base_rate_bps", 1_290)
    if customer.kyc_status == KycStatus.verified.value:
        bps -= rules.get_int(db, "loans.verified_kyc_discount_bps", 290)
    if amount_cents >= rules.get_int(db, "loans.large_threshold_cents", 1_000_000):
        bps -= rules.get_int(db, "loans.large_discount_bps", 100)
    return max(bps, 0)


@router.get("/loans")
def loans_page(
    request: Request,
    amount: float = Query(5000),
    months: int = Query(24),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    loans = db.scalars(select(Loan).where(Loan.customer_id == user.id).order_by(Loan.created_at.desc())).all()
    accounts = [a for a in user.accounts if a.status == AccountStatus.active.value and a.type == "checking"]
    min_cents = rules.get_int(db, "loans.min_cents", 50_000)
    max_cents = rules.get_int(db, "loans.max_cents", 5_000_000)
    amount_cents = int(max(min_cents, min(amount * 100, max_cents)))
    months = max(rules.get_int(db, "loans.min_months", 3), min(months, rules.get_int(db, "loans.max_months", 60)))
    rate = rate_for(db, user, amount_cents)
    return render(
        request,
        "loans.html",
        title="Loans",
        loans=loans,
        accounts=accounts,
        quote=quote_loan(amount_cents, rate, months),
        query={"amount": amount_cents / 100, "months": months, "rate_bps": rate},
    )


@router.post("/loans/apply")
def apply_loan(
    request: Request,
    account_id: int = Form(...),
    amount: str = Form("0"),
    months: int = Form(24),
    purpose: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if not account or account.customer_id != user.id:
        flash(request, "error", "Choose one of your accounts.")
        return RedirectResponse("/loans", status_code=303)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
    except ValueError:
        flash(request, "error", "Enter a valid loan amount.")
        return RedirectResponse("/loans", status_code=303)

    try:
        loan = apply_for_loan(
            db,
            customer=user,
            account=account,
            principal_cents=cents,
            months=months,
            rate_bps=rate_for(db, user, cents),
            purpose=purpose.strip(),
        )
        db.commit()
        flash(request, "success", f"Application #{loan.id} submitted. An advisor will review it shortly.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse("/loans", status_code=303)


@router.get("/loans/{loan_id}")
def loan_detail(loan_id: int, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    loan = db.get(Loan, loan_id)
    if not loan or loan.customer_id != user.id:
        flash(request, "error", "Loan not found.")
        return RedirectResponse("/loans", status_code=303)
    return render(request, "loan_detail.html", title=f"Loan #{loan.id}", loan=loan)


@router.post("/loans/{loan_id}/installments/{installment_id}/pay")
def pay_installment(
    loan_id: int,
    installment_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    loan = db.get(Loan, loan_id)
    inst = db.get(LoanInstallment, installment_id)
    if not loan or loan.customer_id != user.id or not inst or inst.loan_id != loan.id:
        flash(request, "error", "Loan instalment not found.")
        return RedirectResponse("/loans", status_code=303)
    try:
        repay_installment(db, actor=user, loan=loan, installment=inst)
        db.commit()
        flash(request, "success", f"Instalment {inst.number} paid.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/loans/{loan.id}", status_code=303)


@router.post("/loans/{loan_id}/repay-all")
def repay_all(
    loan_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    loan = db.get(Loan, loan_id)
    if not loan or loan.customer_id != user.id:
        flash(request, "error", "Loan not found.")
        return RedirectResponse("/loans", status_code=303)
    try:
        repay_loan_in_full(db, actor=user, loan=loan)
        db.commit()
        flash(request, "success", f"Loan #{loan.id} repaid in full.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse(f"/loans/{loan.id}", status_code=303)
