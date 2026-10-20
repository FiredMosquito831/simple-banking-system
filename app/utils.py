"""Formatting helpers and small request utilities."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Request


def money(cents: int, currency: str = "USD", signed: bool = False) -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "RON": "lei "}.get(currency, currency + " ")
    amount = abs(cents) / 100
    sign = ""
    if cents < 0:
        sign = "−"
    elif signed and cents > 0:
        sign = "+"
    return f"{sign}{sym}{amount:,.2f}"


def money_plain(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def fmt_date(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def fmt_datetime(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M") if value else "—"


def fmt_account(number: str) -> str:
    if len(number) > 8:
        return " ".join([number[:4], number[4:8], number[8:12], number[12:16], number[16:]])
    return number


def ago(value: datetime | None) -> str:
    if not value:
        return ""
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 7:
        return f"{seconds // 86400}d ago"
    return value.strftime("%d %b %Y")


def flash(request: Request, kind: str, message: str) -> None:
    request.session.setdefault("flash", []).append([kind, message])


def pop_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("flash", [])


def spark_points(values: list[int], width: int = 600, height: int = 150, pad: int = 8) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1)
    step = (width - pad * 2) / max(len(values) - 1, 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = height - pad - ((v - lo) / span) * (height - pad * 2)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def spark_area(values: list[int], width: int = 600, height: int = 150, pad: int = 8) -> str:
    pts = spark_points(values, width, height, pad)
    if not pts:
        return ""
    first_x = pad
    last_x = width - pad
    return f"M{first_x},{height - pad} L{pts.replace(' ', ' L')} L{last_x},{height - pad} Z"


def template_ctx(request: Request, **extra: Any) -> dict[str, Any]:
    from .models import Customer
    from .services import settings as rules

    user: Customer | None = getattr(request.state, "user", None)
    bank_name = rules.cached("bank.name", "Danube Bank")
    bank_lead, _, bank_tail = bank_name.rpartition(" ")
    if not bank_lead:
        bank_lead, bank_tail = bank_name, ""
    return {
        "request": request,
        "user": user,
        "unread": getattr(request.state, "unread", 0),
        "flashes": pop_flashes(request),
        "bank_name": bank_name,
        "bank_lead": bank_lead,
        "bank_tail": bank_tail,
        "bank_support_email": rules.cached("bank.support_email", ""),
        "format_money": money,
        "format_date": fmt_date,
        "format_datetime": fmt_datetime,
        "format_account": fmt_account,
        "ago": ago,
        "spark_points": spark_points,
        "spark_area": spark_area,
        **extra,
    }
