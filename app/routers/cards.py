"""Debit cards: issue, freeze, limits, PIN, reveals and simulated purchases."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user, owned_account, render, verify_csrf
from ..models import Account, AccountStatus, Card, CardStatus, Customer
from ..security import (
    decrypt_pan,
    encrypt_pan,
    generate_card_number,
    generate_pin,
    hash_secret,
    verify_secret,
)
from ..services.banking import BankingError, card_payment, notify
from ..utils import flash

router = APIRouter()


def _own_card(card_id: int, user: Customer, db: Session) -> Card:
    card = db.get(Card, card_id)
    if not card or card.customer_id != user.id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Card not found.")
    return card


@router.get("/cards")
def cards_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    cards = db.scalars(
        select(Card).where(Card.customer_id == user.id, Card.status != CardStatus.cancelled.value)
        .order_by(Card.created_at)
    ).all()
    accounts = [a for a in user.accounts if a.status == AccountStatus.active.value]
    return render(
        request,
        "cards.html",
        title="Cards",
        cards=cards,
        accounts=accounts,
        reveal_allowed=settings.reveal_card_numbers,
    )


@router.post("/cards")
def issue_card(
    request: Request,
    account_id: int = Form(...),
    pin: str = Form(""),
    daily_limit: str = Form("2500"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    account = owned_account(account_id, user, db)
    if account.status != AccountStatus.active.value:
        flash(request, "error", "That account is not active.")
        return RedirectResponse("/cards", status_code=303)

    existing = db.scalars(select(Card).where(Card.account_id == account.id, Card.status != CardStatus.cancelled.value)).all()
    if len(existing) >= 3:
        flash(request, "error", "This account already has the maximum of 3 cards.")
        return RedirectResponse("/cards", status_code=303)

    if pin and not (pin.isdigit() and len(pin) == 4):
        flash(request, "error", "PIN must be exactly 4 digits.")
        return RedirectResponse("/cards", status_code=303)
    pin = pin or generate_pin()
    try:
        limit_cents = int(round(float(daily_limit.replace(",", "")) * 100))
        limit_cents = max(1_000, min(limit_cents, 1_000_000))
    except ValueError:
        limit_cents = 250_000

    pan = generate_card_number()
    today = date.today()
    card = Card(
        account_id=account.id,
        customer_id=user.id,
        pan_encrypted=encrypt_pan(pan),
        last4=pan[-4:],
        brand="Visa",
        exp_month=today.month,
        exp_year=today.year + 4,
        pin_hash=hash_secret(pin),
        daily_limit_cents=limit_cents,
    )
    db.add(card)
    notify(db, user.id, "New card issued", f"A debit card ending {pan[-4:]} was added to {account.label}.")
    db.commit()
    flash(request, "success", f"Card issued — ending {pan[-4:]}, PIN {pin}. Keep it safe!")
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/freeze")
def toggle_freeze(
    card_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    card = _own_card(card_id, user, db)
    if card.status == CardStatus.active.value:
        card.status = CardStatus.frozen.value
        flash(request, "success", f"Card {card.last4} frozen.")
    elif card.status == CardStatus.frozen.value:
        card.status = CardStatus.active.value
        flash(request, "success", f"Card {card.last4} unfrozen.")
    db.commit()
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/cancel")
def cancel_card(
    card_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    card = _own_card(card_id, user, db)
    card.status = CardStatus.cancelled.value
    notify(db, user.id, "Card cancelled", f"Card ending {card.last4} has been cancelled.")
    db.commit()
    flash(request, "success", f"Card {card.last4} cancelled.")
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/limit")
def set_limit(
    card_id: int,
    request: Request,
    daily_limit: str = Form("2500"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    card = _own_card(card_id, user, db)
    try:
        cents = int(round(float(daily_limit.replace(",", "")) * 100))
    except ValueError:
        flash(request, "error", "Enter a valid daily limit.")
        return RedirectResponse("/cards", status_code=303)
    card.daily_limit_cents = max(1_000, min(cents, 1_000_000))
    db.commit()
    flash(request, "success", f"Daily limit set to {card.daily_limit_cents / 100:,.2f}.")
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/pin")
def change_pin(
    card_id: int,
    request: Request,
    current_pin: str = Form(""),
    new_pin: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    card = _own_card(card_id, user, db)
    if not verify_secret(current_pin, card.pin_hash):
        flash(request, "error", "Current PIN is incorrect.")
    elif not (new_pin.isdigit() and len(new_pin) == 4):
        flash(request, "error", "New PIN must be exactly 4 digits.")
    else:
        card.pin_hash = hash_secret(new_pin)
        db.commit()
        flash(request, "success", "PIN updated.")
    return RedirectResponse("/cards", status_code=303)


@router.post("/cards/{card_id}/pay")
def simulate_purchase(
    card_id: int,
    request: Request,
    amount: str = Form("0"),
    merchant: str = Form("Demo Store"),
    _csrf: None = Depends(verify_csrf),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    card = _own_card(card_id, user, db)
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
    except ValueError:
        flash(request, "error", "Enter a valid amount.")
        return RedirectResponse("/cards", status_code=303)
    try:
        card_payment(db, actor=user, card=card, amount_cents=cents, merchant=merchant.strip() or "Demo Store")
        db.commit()
        flash(request, "success", f"Card payment of {cents / 100:,.2f} approved at {merchant}.")
    except BankingError as exc:
        db.rollback()
        flash(request, "error", str(exc))
    return RedirectResponse("/cards", status_code=303)


@router.get("/cards/{card_id}/details")
def card_details(
    card_id: int,
    request: Request,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    """Demo-only convenience: reveal the full PAN over an authenticated request."""
    card = _own_card(card_id, user, db)
    if not settings.reveal_card_numbers:
        return JSONResponse({"detail": "Reveal is disabled."}, status_code=403)
    return JSONResponse(
        {
            "pan": decrypt_pan(card.pan_encrypted),
            "expiry": card.expiry,
            "last4": card.last4,
            "status": card.status,
        }
    )
