# Danube Bank — full-stack banking demo

A complete, runnable banking system built on **FastAPI + MySQL**, grown from the original
[`simple-banking-system`](legacy/) console scripts that started this repository.

![Dashboard](docs/screenshots/dashboard.png)

Everything runs locally: MySQL in Docker, the app in Uvicorn, seed data included.

---

## What's inside

**Customer experience**

| Area | Features |
| --- | --- |
| Onboarding | Register, open extra checking/savings accounts, KYC status, profile & password management |
| Accounts | Balances, overdraft facility, savings interest, account numbers (`DANB…`), per-account statement & CSV export |
| Payments | Instant transfers to any local account number, saved beneficiaries, receipts, ownership checks |
| Cards | Virtual Visa cards with Luhn-valid numbers, hashed PIN, freeze/unfreeze, cancel, daily limits, PIN change, simulated purchases, demo reveal |
| Loans | Rate quotes, applications, admin approval, amortisation schedules, pay an instalment or repay in full |
| Automation | Standing orders (daily/weekly/monthly), daily interest accrual, overdue instalment detection |
| Insights | 14-day balance chart, 30-day inflow/outflow, spending breakdown, notifications |
| Admin | Customer search, lock/unlock, KYC review, loan approval, global transactions, audit trail, batch runner |

**Engineering**

- Double-entry ledger: every movement writes a `Transaction` plus signed `LedgerEntry` rows recording the running balance.
- Transfers lock both accounts (`SELECT … FOR UPDATE`, ordered by id to avoid deadlocks) inside a single DB transaction.
- Idempotency keys supported at the ledger level; balances are integers (cents), never floats.
- Passwords and PINs hashed with scrypt; card PANs encrypted with Fernet, only the last four digits stored in the clear.
- CSRF tokens on every mutating form, signed cookie sessions, role-based access control.
- 14 tests covering Luhn, hashing, loan maths and ledger invariants.

---

## Quick start

**Prerequisites:** Docker Desktop, Python 3.11+.

```bash
git clone https://github.com/FiredMosquito831/simple-banking-system.git
cd simple-banking-system

# 1. MySQL 8.4 in Docker (port 3306)
docker compose up -d

# 2. Python environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt

# 3. Config + schema + demo data
cp .env.example .env
python -m scripts.seed

# 4. Run
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000**.

### Demo accounts

| Email | Password | Role |
| --- | --- | --- |
| `alex@danube.demo` | `Password123!` | Customer (2 accounts, card, active loan) |
| `maria@danube.demo` | `Password123!` | Customer (KYC pending, active loan) |
| `andrei@danube.demo` | `Password123!` | Customer (empty account, useful as a transfer target) |
| `admin@danube.demo` | `Password123!` | Administrator |

> The login page has one-click chips for each demo user.

To wipe and reseed at any time: `python -m scripts.seed --reset`.

---

## How the ledger works

```
Transaction (reference, type, amount, status)
 ├── LedgerEntry  account A  −250.00   balance_after 1,750.00
 └── LedgerEntry  account B  +250.00   balance_after   250.00
```

- Transfers create two entries that must sum to zero.
- Deposits, withdrawals, card payments, interest and loan disbursements are single-sided entries: the other side is the bank's external position, which this demo does not model as an account.
- `post_transaction()` is the only function that changes a balance. It validates account status, currency and available funds (balance + overdraft) before writing anything.
- Every entry keeps `balance_after_cents`, so statements can be rebuilt even if an account balance were ever corrupted.

## Project layout

```
app/
  main.py            FastAPI app, middleware, error handling
  config.py          Settings from .env
  db.py              Engine + sessions
  models.py          Customers, accounts, cards, ledger, loans, audit
  security.py        scrypt hashing, Luhn, Fernet-encrypted PANs
  deps.py            Auth, CSRF, template rendering
  routers/           auth, accounts, transfers, cards, loans, admin
  services/          banking.py (money movement), queries.py (read models)
  templates/         Jinja2 pages
  static/            app.css, app.js
scripts/seed.py      Schema creation + demo data
tests/               Unit + ledger integration tests
legacy/              The original CLI scripts, preserved
docker-compose.yml   MySQL 8.4
```

## Business rules (demo values)

| Rule | Value |
| --- | --- |
| Opening credit on registration | $500 |
| Checking overdraft | $500 |
| Savings interest | 3.50% p.a. (daily accrual) |
| Loan range / term | $500 – $50,000 / 3 – 60 months |
| Loan rate | 12.90% base, 10.00% with verified KYC, −1% above $10k |
| Debit card default daily limit | $2,500 (max $10,000) |
| Accounts per customer | 6 |

## Tests

```bash
pytest
```

Unit tests run anywhere; ledger tests need MySQL up (they skip otherwise) and roll back their data.

## Configuration

Copy `.env.example` → `.env`. Important values:

- `SECRET_KEY` — signs session cookies and derives the card-encryption key. Change it for any shared deployment.
- `DATABASE_URL` — defaults to `mysql+pymysql://banker:bankerpass@127.0.0.1:3306/banking`.
- `DEMO_PASSWORD` — password assigned to seeded users.
- `REVEAL_CARD_NUMBERS` — set `false` to disable the demo reveal endpoint.

## Security notes

This is a **demo**. It intentionally includes conveniences that a real bank would not:

- The full card number can be revealed in the UI (demo feature, toggleable).
- Login throttling is a simple per-session counter, not a distributed rate limiter.
- No TLS, email verification, MFA, 2FA for transfers, or real KYC integration.
- Card numbers are generated locally with the `400000` BIN and are not connected to any card network.

There is a real double-entry ledger, real row locking and real password hashing underneath, but no money moves. Use it to learn, demo, or build on — not to bank.

## License

MIT — see [LICENSE](LICENSE).
