"""Shared request dependencies: auth, CSRF, template rendering."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .db import get_db
from .icons import icon
from .models import Account, Customer, CustomerStatus, Role
from .utils import template_ctx

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["icon"] = icon


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, template_ctx(request, **context))


def current_user(request: Request, db: Session = Depends(get_db)) -> Customer:
    customer_id = request.session.get("customer_id")
    if not customer_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user = db.get(Customer, customer_id)
    if not user or user.status != CustomerStatus.active.value:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    request.state.user = user
    from sqlalchemy import func, select

    from .models import Notification

    request.state.unread = db.scalar(
        select(func.count(Notification.id)).where(
            Notification.customer_id == user.id, Notification.is_read.is_(False)
        )
    ) or 0
    return user


def admin_user(user: Customer = Depends(current_user)) -> Customer:
    if user.role != Role.admin.value:
        raise HTTPException(status_code=403, detail="Administrator access only.")
    return user


def verify_csrf(request: Request, csrf: str = Form("")) -> None:
    expected = request.session.get("csrf")
    if not expected or not csrf or csrf != expected:
        raise HTTPException(status_code=400, detail="Your session expired. Please refresh the page and try again.")


def owned_account(account_id: int, user: Customer, db: Session) -> Account:
    account = db.get(Account, account_id)
    if not account or account.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Account not found.")
    return account
