"""DB-backed institution settings.

Business rules live in the `settings` table so an operator can change them
from the admin app without a redeploy. A module-level cache keeps template
rendering cheap; `warm_cache()` refreshes it after any update or at startup.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS: list[dict[str, str]] = [
    # key, value, type, label, category
    {"key": "bank.name", "value": "Danube Bank", "value_type": "string",
     "label": "Institution name", "category": "Institution"},
    {"key": "bank.legal_name", "value": "Danube Bank S.A. — demo", "value_type": "string",
     "label": "Legal name", "category": "Institution"},
    {"key": "bank.support_email", "value": "support@danubelabs.net", "value_type": "email",
     "label": "Support email", "category": "Institution"},
    {"key": "bank.phone", "value": "+40 21 555 0100", "value_type": "string",
     "label": "Support phone", "category": "Institution"},
    {"key": "bank.address", "value": "Str. Franceza 12, Bucharest, Romania", "value_type": "string",
     "label": "Registered address", "category": "Institution"},
    {"key": "bank.code", "value": "DANB", "value_type": "string",
     "label": "Account number prefix", "category": "Institution"},

    {"key": "money.default_currency", "value": "USD", "value_type": "string",
     "label": "Default currency", "category": "Money"},
    {"key": "money.max_transfer_cents", "value": "10000000", "value_type": "int",
     "label": "Maximum single transfer (cents)", "category": "Money"},
    {"key": "money.max_cash_cents", "value": "10000000", "value_type": "int",
     "label": "Maximum single cash deposit/withdrawal (cents)", "category": "Money"},

    {"key": "accounts.max_per_customer", "value": "6", "value_type": "int",
     "label": "Max open accounts per customer", "category": "Accounts"},
    {"key": "accounts.opening_credit_cents", "value": "50000", "value_type": "int",
     "label": "Opening credit on registration (cents)", "category": "Accounts"},
    {"key": "accounts.checking_overdraft_cents", "value": "50000", "value_type": "int",
     "label": "Checking overdraft facility (cents)", "category": "Accounts"},
    {"key": "accounts.savings_interest_bps", "value": "350", "value_type": "int",
     "label": "Savings interest (basis points p.a.)", "category": "Accounts"},

    {"key": "cards.default_daily_limit_cents", "value": "250000", "value_type": "int",
     "label": "Default card daily limit (cents)", "category": "Cards"},
    {"key": "cards.max_daily_limit_cents", "value": "1000000", "value_type": "int",
     "label": "Maximum card daily limit (cents)", "category": "Cards"},
    {"key": "cards.max_per_account", "value": "3", "value_type": "int",
     "label": "Max active cards per account", "category": "Cards"},
    {"key": "security.reveal_card_numbers", "value": "true", "value_type": "bool",
     "label": "Allow customers to reveal card numbers", "category": "Security"},
    {"key": "ops.maintenance_mode", "value": "false", "value_type": "bool",
     "label": "Maintenance mode (customers only)", "category": "Operations"},
    {"key": "ui.show_demo_logins", "value": "true", "value_type": "bool",
     "label": "Show seeded demo accounts on the login page", "category": "Operations"},

    {"key": "loans.min_cents", "value": "50000", "value_type": "int",
     "label": "Minimum loan amount (cents)", "category": "Loans"},
    {"key": "loans.max_cents", "value": "5000000", "value_type": "int",
     "label": "Maximum loan amount (cents)", "category": "Loans"},
    {"key": "loans.min_months", "value": "3", "value_type": "int",
     "label": "Minimum term (months)", "category": "Loans"},
    {"key": "loans.max_months", "value": "60", "value_type": "int",
     "label": "Maximum term (months)", "category": "Loans"},
    {"key": "loans.base_rate_bps", "value": "1290", "value_type": "int",
     "label": "Base annual rate (basis points)", "category": "Loans"},
    {"key": "loans.verified_kyc_discount_bps", "value": "290", "value_type": "int",
     "label": "Rate discount for verified KYC (bps)", "category": "Loans"},
    {"key": "loans.large_threshold_cents", "value": "1000000", "value_type": "int",
     "label": "Large-loan threshold (cents)", "category": "Loans"},
    {"key": "loans.large_discount_bps", "value": "100", "value_type": "int",
     "label": "Large-loan rate discount (bps)", "category": "Loans"},
]

_DEFAULTS = {row["key"]: row for row in DEFAULTS}
_cache: dict[str, str] = {}


def ensure_defaults(db: Session) -> int:
    """Insert any missing settings rows. Returns how many were created."""
    existing = set(db.scalars(select(Setting.key)).all())
    created = 0
    for row in DEFAULTS:
        if row["key"] not in existing:
            db.add(Setting(**row))
            created += 1
    if created:
        db.flush()
    return created


def warm_cache(db: Session) -> None:
    _cache.clear()
    for row in db.scalars(select(Setting)).all():
        _cache[row.key] = row.value


def cached(key: str, default: str | None = None) -> str:
    if key in _cache:
        return _cache[key]
    return default if default is not None else _DEFAULTS.get(key, {}).get("value", "")


def get_value(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return _DEFAULTS.get(key, {}).get("value", "")


def get_int(db: Session, key: str, default: int = 0) -> int:
    try:
        return int(float(get_value(db, key)))
    except (TypeError, ValueError):
        return default


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    value = get_value(db, key).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        meta = _DEFAULTS.get(key, {"key": key, "value_type": "string", "label": key, "category": "Custom"})
        row = Setting(**meta)
        db.add(row)
    if row.value_type == "int":
        value = str(int(float(value)))
    elif row.value_type == "bool":
        value = "true" if str(value).strip().lower() in ("1", "true", "yes", "on") else "false"
    row.value = value
    db.flush()


def grouped_rows(db: Session) -> dict[str, list[Setting]]:
    rows = db.scalars(select(Setting).order_by(Setting.category, Setting.key)).all()
    groups: dict[str, list[Setting]] = {}
    for row in rows:
        groups.setdefault(row.category, []).append(row)
    return groups
