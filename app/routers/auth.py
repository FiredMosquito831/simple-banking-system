"""Authentication, registration and account settings."""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user, render, verify_csrf
from ..models import Customer, KycStatus, Role
from ..security import hash_secret, verify_secret
from ..services.banking import audit, create_account, notify
from ..services import settings as rules
from ..utils import flash

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/")
def index(request: Request):
    if request.session.get("customer_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


def _login_context(db: Session, request: Request, email: str = "") -> dict:
    demo_users = []
    show_demo = rules.get_bool(db, "ui.show_demo_logins", True)
    if show_demo:
        demo_users = db.scalars(
            select(Customer).where(Customer.email.like("%@danube.demo")).order_by(Customer.role.desc(), Customer.email)
        ).all()
    return {
        "title": "Sign in",
        "email": email,
        "demo_users": demo_users,
        "demo_password": settings.demo_password if show_demo else "",
    }


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("customer_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", **_login_context(db, request))


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    attempts = request.session.get("login_attempts", 0)
    if attempts >= 8:
        flash(request, "error", "Too many failed attempts. Please wait a moment and try again.")
        return render(request, "login.html", **_login_context(db, request, email))

    customer = db.scalar(select(Customer).where(Customer.email == email))
    if not customer or not verify_secret(password, customer.password_hash):
        request.session["login_attempts"] = attempts + 1
        flash(request, "error", "That email and password combination doesn't match our records.")
        return render(request, "login.html", **_login_context(db, request, email))

    if customer.status == "locked":
        flash(request, "error", "This account is locked. Please contact support.")
        return render(request, "login.html", **_login_context(db, request, email))

    request.session.pop("login_attempts", None)
    request.session["customer_id"] = customer.id
    db.add(customer)
    audit(db, customer, "auth.login", "customer", customer.id, ip=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/register")
def register_page(request: Request):
    if request.session.get("customer_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "register.html", title="Open an account")


@router.post("/register")
def register(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    date_of_birth: str = Form(""),
    address: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    form = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": email.strip().lower(),
        "phone": phone.strip(),
        "date_of_birth": date_of_birth,
        "address": address.strip(),
    }
    errors: list[str] = []
    if not form["first_name"] or not form["last_name"]:
        errors.append("Please enter your first and last name.")
    if not EMAIL_RE.match(form["email"]):
        errors.append("Please enter a valid email address.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != password_confirm:
        errors.append("Passwords do not match.")
    if form["email"] and db.scalar(select(Customer.id).where(Customer.email == form["email"])):
        errors.append("An account with this email already exists.")

    dob: date | None = None
    if form["date_of_birth"]:
        try:
            dob = date.fromisoformat(form["date_of_birth"])
        except ValueError:
            errors.append("Date of birth must be a valid date.")

    if errors:
        for message in errors:
            flash(request, "error", message)
        return render(request, "register.html", title="Open an account", form=form)

    customer = Customer(
        email=form["email"],
        password_hash=hash_secret(password),
        first_name=form["first_name"],
        last_name=form["last_name"],
        phone=form["phone"] or None,
        address=form["address"] or None,
        date_of_birth=dob,
        role=Role.customer.value,
        kyc_status=KycStatus.pending.value,
    )
    db.add(customer)
    db.flush()

    create_account(db, customer, "checking", "Everyday checking",
                   opening_cents=rules.get_int(db, "accounts.opening_credit_cents", 50_000),
                   overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000))
    create_account(db, customer, "savings", "Savings", opening_cents=0,
                   interest_bps=rules.get_int(db, "accounts.savings_interest_bps", 350))
    notify(db, customer.id, "Welcome to Danube Bank", "We added a $500 opening credit to your checking account. "
                                                     "Complete KYC to unlock higher limits.")
    audit(db, customer, "customer.register", "customer", customer.id,
          ip=request.client.host if request.client else None)
    db.commit()

    request.session["customer_id"] = customer.id
    flash(request, "success", "Your accounts are ready. Welcome aboard!")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(verify_csrf)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Settings --------------------------------------------------------------
@router.get("/settings")
def settings_page(request: Request, user: Customer = Depends(current_user)):
    return render(request, "settings.html", title="Settings")


@router.post("/settings/profile")
def update_profile(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user: Customer = Depends(current_user),
    db: Session = Depends(get_db),
):
    user.first_name = first_name.strip() or user.first_name
    user.last_name = last_name.strip() or user.last_name
    user.phone = phone.strip() or None
    user.address = address.strip() or None
    audit(db, user, "customer.update_profile", "customer", user.id)
    db.commit()
    flash(request, "success", "Profile updated.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    user: Customer = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_secret(current_password, user.password_hash):
        flash(request, "error", "Current password is incorrect.")
    elif len(new_password) < 8:
        flash(request, "error", "New password must be at least 8 characters.")
    elif new_password != confirm_password:
        flash(request, "error", "New passwords do not match.")
    else:
        user.password_hash = hash_secret(new_password)
        audit(db, user, "customer.change_password", "customer", user.id)
        db.commit()
        flash(request, "success", "Password changed.")
    return RedirectResponse("/settings", status_code=303)


# --- Notifications ---------------------------------------------------------
@router.get("/notifications")
def notifications_page(
    request: Request,
    user: Customer = Depends(current_user),
    db: Session = Depends(get_db),
):
    from ..models import Notification

    items = db.scalars(
        select(Notification).where(Notification.customer_id == user.id).order_by(Notification.created_at.desc()).limit(100)
    ).all()
    return render(request, "notifications.html", title="Notifications", items=items)


@router.post("/notifications/read")
def mark_notifications_read(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user: Customer = Depends(current_user),
    db: Session = Depends(get_db),
):
    from ..models import Notification

    for item in db.scalars(select(Notification).where(Notification.customer_id == user.id, Notification.is_read.is_(False))):
        item.is_read = True
    db.commit()
    return RedirectResponse("/notifications", status_code=303)
