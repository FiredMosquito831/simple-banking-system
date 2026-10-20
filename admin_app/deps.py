"""Admin app dependencies: admin auth, CSRF and template rendering."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.icons import icon
from app.models import Customer, CustomerStatus, Role
from app.utils import template_ctx

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["icon"] = icon

SESSION_KEY = "ops_customer_id"


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, template_ctx(request, **context))


def admin_user(request: Request, db: Session = Depends(get_db)) -> Customer:
    customer_id = request.session.get(SESSION_KEY)
    if not customer_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user = db.get(Customer, customer_id)
    if not user or user.role != Role.admin.value or user.status != CustomerStatus.active.value:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    request.state.user = user
    return user


def verify_csrf(request: Request, csrf: str = Form("")) -> None:
    expected = request.session.get("csrf")
    if not expected or not csrf or csrf != expected:
        raise HTTPException(status_code=400, detail="Your session expired. Refresh the page and try again.")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
