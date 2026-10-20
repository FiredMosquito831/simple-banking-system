# Changelog

All notable changes to this project are documented here.

## 2.0.0 — 2026-10-20

The console scripts became a complete two-application banking platform.

### Added

- Customer web app (FastAPI + Jinja2) covering registration, accounts, transfers, beneficiaries,
  standing orders, cards, loans, statements, notifications and settings.
- **Danube Ops**, a standalone operator app on port 8001: customers, KYC, accounts, adjustments,
  transaction reversal, card operations, loan decisions, runtime settings, database explorer,
  CSV export, SQL backup, audited SQL console and audit trail.
- Double-entry ledger with row-locked transfers, signed entries, running balances, idempotency keys
  and reversal support.
- MySQL 8.4 schema (12 tables) managed with SQLAlchemy 2.0, plus seed script and demo data.
- DB-backed settings table so rates, limits and branding change without a redeploy.
- Maintenance mode, operator audit logging, Luhn-valid encrypted card numbers and scrypt PINs.
- 14 unit and ledger integration tests, GitHub Actions workflow.
- Presentation site and documentation published via GitHub Pages.
- Optional full-stack Docker profile (`docker compose --profile full up -d --build`).

### Changed

- Business rules moved from Python constants to the `settings` table.
- Demo login shortcuts are now generated from seeded customers instead of being hardcoded.
- Bank name and branding are read from settings across both apps.

### Preserved

- The original CLI exercises now live in `legacy/`.

## 1.0.0 — 2024-10-06

Initial upload: five standalone Python scripts exploring card creation, Luhn checks and an
in-memory login flow.
