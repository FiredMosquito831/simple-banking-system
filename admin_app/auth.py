"""Operator sign-in for the admin app."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Customer, CustomerStatus, Role
from app.security import verify_secret
from app.services.banking import audit

from .deps import SESSION_KEY, render, verify_csrf

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    if request.session.get(SESSION_KEY):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", title="Operator sign-in")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    customer = db.scalar(select(Customer).where(Customer.email == email))
    if (
        not customer
        or customer.role != Role.admin.value
        or customer.status != CustomerStatus.active.value
        or not verify_secret(password, customer.password_hash)
    ):
        return render(request, "login.html", title="Operator sign-in",
                      email=email, error="Invalid operator credentials.")

    request.session[SESSION_KEY] = customer.id
    audit(db, customer, "admin.login", "customer", customer.id,
          ip=request.client.host if request.client else None)
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(verify_csrf)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
