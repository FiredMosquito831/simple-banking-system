"""Money transfers, beneficiaries and scheduled payments."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_account, render, verify_csrf
from ..models import (
    AccountStatus,
    Beneficiary,
    Frequency,
    ScheduledStatus,
    ScheduledTransfer,
)
from ..services import settings as rules
from ..services.banking import BankingError, get_account_by_number, transfer
from ..utils import flash

router = APIRouter()


def _parse_amount(raw: str, max_cents: int) -> int:
    cents = int(round(float(raw.replace(",", "").replace("$", "")) * 100))
    if not 1 <= cents <= max_cents:
        raise ValueError("amount out of range")
    return cents


@router.get("/transfer")
def transfer_page(
    request: Request,
    user=Depends(current_user),
    db: Session = Depends(get_db),
    to: str = "",
    account: int | None = None,
):
    accounts = [a for a in user.accounts if a.status == AccountStatus.active.value]
    payees = db.scalars(select(Beneficiary).where(Beneficiary.customer_id == user.id).order_by(Beneficiary.name)).all()
    return render(
        request,
        "transfer.html",
        title="Send money",
        accounts=accounts,
        payees=payees,
        preselected=account,
        prefill_to=to,
    )


@router.post("/transfer")
def do_transfer(
    request: Request,
    from_account_id: int = Form(...),
    to_account_number: str = Form(""),
    amount: str = Form("0"),
    description: str = Form(""),
    save_beneficiary: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(from_account_id, user, db)
    clean_to = to_account_number.replace(" ", "").strip().upper()

    try:
        cents = _parse_amount(amount, rules.get_int(db, "money.max_transfer_cents", 10_000_000))
    except (ValueError, TypeError):
        flash(request, "error", "Enter a valid amount between 0.01 and 100,000.")
        return RedirectResponse("/transfer", status_code=303)

    if clean_to == account.account_number:
        flash(request, "error", "You cannot transfer to the account you're sending from.")
        return RedirectResponse("/transfer", status_code=303)

    target = get_account_by_number(db, clean_to)
    if not target:
        flash(request, "error", "That account number does not exist at Danube Bank.")
        return RedirectResponse("/transfer", status_code=303)

    try:
        tx = transfer(
            db,
            actor=user,
            from_account_id=account.id,
            to_account_number=clean_to,
            amount_cents=cents,
            description=description.strip() or f"Transfer to {target.customer.full_name}",
        )
        if save_beneficiary and not db.scalar(
            select(Beneficiary).where(
                Beneficiary.customer_id == user.id, Beneficiary.account_number == clean_to
            )
        ):
            db.add(
                Beneficiary(
                    customer_id=user.id,
                    name=target.customer.full_name,
                    account_number=clean_to,
                    bank_name="Danube Bank",
                )
            )
        db.commit()
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
        return RedirectResponse("/transfer", status_code=303)

    flash(request, "success", f"Sent {cents / 100:,.2f} {account.currency} to {target.customer.full_name}.")
    return RedirectResponse(f"/receipt/{tx.reference}", status_code=303)


@router.get("/receipt/{reference}")
def receipt(reference: str, request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    from ..models import Transaction

    tx = db.scalar(select(Transaction).where(Transaction.reference == reference))
    if not tx:
        flash(request, "error", "Receipt not found.")
        return RedirectResponse("/dashboard", status_code=303)
    entry = next(
        (e for e in tx.entries if e.account.customer_id == user.id),
        None,
    )
    if not entry and user.role != "admin":
        flash(request, "error", "That receipt belongs to another customer.")
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "receipt.html", title="Transfer receipt", tx=tx, entry=entry)


# --- Beneficiaries ---------------------------------------------------------
@router.get("/beneficiaries")
def beneficiaries_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    payees = db.scalars(select(Beneficiary).where(Beneficiary.customer_id == user.id).order_by(Beneficiary.name)).all()
    return render(request, "beneficiaries.html", title="Beneficiaries", payees=payees)


@router.post("/beneficiaries")
def add_beneficiary(
    request: Request,
    name: str = Form(""),
    account_number: str = Form(""),
    bank_name: str = Form("Danube Bank"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    clean = account_number.replace(" ", "").strip().upper()
    if not name.strip() or not clean:
        flash(request, "error", "Name and account number are required.")
    elif get_account_by_number(db, clean) is None:
        flash(request, "error", "No account with that number exists at Danube Bank.")
    elif db.scalar(select(Beneficiary.id).where(Beneficiary.customer_id == user.id, Beneficiary.account_number == clean)):
        flash(request, "error", "That beneficiary is already saved.")
    else:
        db.add(Beneficiary(customer_id=user.id, name=name.strip(), account_number=clean, bank_name=bank_name.strip() or "Danube Bank"))
        db.commit()
        flash(request, "success", "Beneficiary saved.")
    return RedirectResponse("/beneficiaries", status_code=303)


@router.post("/beneficiaries/{beneficiary_id}/delete")
def delete_beneficiary(
    beneficiary_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    payee = db.get(Beneficiary, beneficiary_id)
    if not payee or payee.customer_id != user.id:
        flash(request, "error", "Beneficiary not found.")
    else:
        db.delete(payee)
        db.commit()
        flash(request, "success", "Beneficiary removed.")
    return RedirectResponse("/beneficiaries", status_code=303)


# --- Scheduled transfers ---------------------------------------------------
@router.get("/scheduled")
def scheduled_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    plans = db.scalars(
        select(ScheduledTransfer).where(ScheduledTransfer.customer_id == user.id).order_by(ScheduledTransfer.next_run_at)
    ).all()
    accounts = [a for a in user.accounts if a.status == AccountStatus.active.value]
    payees = db.scalars(select(Beneficiary).where(Beneficiary.customer_id == user.id).order_by(Beneficiary.name)).all()
    return render(
        request,
        "scheduled.html",
        title="Scheduled payments",
        plans=plans,
        accounts=accounts,
        payees=payees,
        frequencies=[f.value for f in Frequency],
    )


@router.post("/scheduled")
def create_scheduled(
    request: Request,
    from_account_id: int = Form(...),
    to_account_number: str = Form(""),
    to_name: str = Form(""),
    amount: str = Form("0"),
    frequency: str = Form("monthly"),
    description: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(from_account_id, user, db)
    clean = to_account_number.replace(" ", "").strip().upper()
    target = get_account_by_number(db, clean)
    try:
        cents = _parse_amount(amount, rules.get_int(db, "money.max_transfer_cents", 10_000_000))
    except (ValueError, TypeError):
        flash(request, "error", "Enter a valid amount.")
        return RedirectResponse("/scheduled", status_code=303)

    if not target:
        flash(request, "error", "Destination account not found.")
    elif target.id == account.id:
        flash(request, "error", "Choose a different destination account.")
    elif frequency not in [f.value for f in Frequency]:
        flash(request, "error", "Unknown frequency.")
    else:
        db.add(
            ScheduledTransfer(
                customer_id=user.id,
                from_account_id=account.id,
                to_account_number=clean,
                to_name=to_name.strip() or target.customer.full_name,
                amount_cents=cents,
                frequency=frequency,
                description=description.strip(),
                next_run_at=datetime.utcnow() + timedelta(minutes=1),
            )
        )
        db.commit()
        flash(request, "success", "Scheduled payment created — first run in a minute.")
    return RedirectResponse("/scheduled", status_code=303)


@router.post("/scheduled/{plan_id}/{action}")
def scheduled_action(
    plan_id: int,
    action: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    plan = db.get(ScheduledTransfer, plan_id)
    if not plan or plan.customer_id != user.id:
        flash(request, "error", "Scheduled payment not found.")
        return RedirectResponse("/scheduled", status_code=303)
    if action == "cancel":
        plan.status = ScheduledStatus.cancelled.value
        flash(request, "success", "Scheduled payment cancelled.")
    elif action == "pause":
        plan.status = ScheduledStatus.paused.value
        flash(request, "success", "Scheduled payment paused.")
    elif action == "resume":
        plan.status = ScheduledStatus.active.value
        plan.next_run_at = datetime.utcnow() + timedelta(minutes=1)
        flash(request, "success", "Scheduled payment resumed.")
    db.commit()
    return RedirectResponse("/scheduled", status_code=303)
