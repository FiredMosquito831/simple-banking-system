# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Danube Bank: a two-app banking system demo sharing one MySQL 8.4 database.

- `app/` — customer-facing web app (FastAPI + Jinja2), port **8000**.
- `admin_app/` — "Danube Ops", a standalone operator console (its own login, cookie and signing key), port **8001**.
- `docs/` — static presentation site + documentation, published at **https://bankingsystem.danubelabs.net** (GitHub Pages, source `main:/docs`; Cloudflare CNAME `bankingsystem.danubelabs.net → firedmosquito831.github.io`, DNS-only).

The operator app **never mutates balances directly** — it calls the same `app.services.banking` functions as the customer app.

## Commands

```bash
# one-time setup
docker compose up -d            # MySQL 8.4 on :3306
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -m scripts.seed          # schema + settings defaults + demo data (--reset to wipe)

# run both apps
uvicorn app.main:app --reload                     # :8000 customers
uvicorn admin_app.main:app --port 8001            # :8001 operators

# tests (14; ledger tests need MySQL running, otherwise they skip)
pytest                                            # full suite
pytest tests/test_ledger.py::test_transfer_is_balanced_and_updates_both_balances   # single test

# lint/unused-imports check we rely on
.venv\Scripts\python -m ruff check app admin_app scripts --select F821,F811,F401

# full-stack Docker (optional; default compose only starts MySQL)
docker compose --profile full up -d --build
```

Demo logins (password `Password123!`): `alex@danube.demo`, `maria@danube.demo`,
`andrei@danube.demo` (customers) and `admin@danube.demo` (operator for both apps).
After reseeding, delete and recreate no data manually — `python -m scripts.seed --reset` is the reset.

## Architecture that spans files

- **Money is integer cents, always.** No floats anywhere. `Transaction` (header) + `LedgerEntry`
  (signed, per account, with `balance_after_cents`) form the double-entry ledger.
- **`post_transaction()` in `app/services/banking.py` is the ONLY code path that changes balances.**
  It locks accounts with `SELECT … FOR UPDATE` ordered by id, validates status/currency/available funds
  (balance + overdraft), and rejects multi-entry transactions that don't sum to zero. Deposits,
  withdrawals, interest and card payments are intentionally single-sided (the bank's external position).
- **Reversals don't delete**: `reverse_transaction()` posts mirror entries, sets `reversal_of_id`,
  and marks the original `reversed`.
- **Business rules live in the `settings` table, not in code.** `app/services/settings.py`
  provides `get_value/get_int/get_bool` plus a module cache refreshed every ~10s by both apps'
  middleware; operators edit values from Danube Ops → Settings. If you add a new configurable
  number, add it to `DEFAULTS` in that module (the row is auto-created on startup).
- **Branding flows through `template_ctx()`** (`app/utils.py`): `bank_name`/`bank_lead`/`bank_tail`
  come from the settings cache, so templates never hardcode the institution name.
- Both entrypoints run a `lifespan` that does `create_all` + `ensure_defaults` + `warm_cache` —
  schema changes need a `Base.metadata.create_all`-compatible model update and, for existing
  databases, `scripts/seed --reset` (the app does not migrate).
- **Customer-app maintenance gate**: `app/main.py` checks the cached `ops.maintenance_mode`
  setting and returns a 503 page to everyone but `/login`, `/health`, `/static`.
- CSRF: a per-session token hidden in every form; all mutating routes take
  `_csrf: None = Depends(verify_csrf)`. Operator app uses the same pattern with its own cookie.
- `tests/test_ledger.py` runs against the real MySQL and rolls back; it also creates throwaway
  customers, so don't add assertions on absolute ids.

## Working style for this project (standing rules from the owner)

- **No placeholder UI, no hardcoded business rules.** Every visible control must perform a real,
  audited action; new tunables go into the settings table, not constants.
- **Verify before claiming done**: run `pytest`, restart the server(s) — uvicorn `--reload` goes
  stale on Windows — smoke the affected routes over HTTP, and screenshot UI changes in a browser.
- **Operator actions must be audited** with `app.services.banking.audit()` (actor, action, entity,
  IP) before committing.
- Git commits and PR bodies end with the attribution line `by FiredMosquito831`.
- When you change `docs/`, GitHub Pages rebuilds from `main:/docs` — the site only reflects merged
  work; check `gh api repos/…/pages/builds/latest` after pushing.

## Conventions

- Routers are thin; domain logic belongs in `app/services/` (shared) — keep the admin app thin
  too (`admin_app` views/ops call shared services).
- Money display: always `format_money(cents, currency, signed)` from `app.utils` in templates.
- New routes: declare static paths (`/accounts/new`, `/database/backup.sql`) **before** parameterized
  ones — FastAPI matches in declaration order.
- `legacy/` holds the original console scripts; they are historical, do not extend them.
