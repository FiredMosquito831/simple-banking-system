"""Create the schema and load demo data.

Usage:
    python -m scripts.seed          # create tables + demo data (idempotent-ish)
    python -m scripts.seed --reset  # drop everything first
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Beneficiary,
    Card,
    Customer,
    KycStatus,
    Role,
    TxType,
    utcnow,
)
from app.security import (  # noqa: E402
    encrypt_pan,
    generate_card_number,
    hash_secret,
)
from app.services import settings as rules  # noqa: E402
from app.services.banking import (  # noqa: E402
    approve_loan,
    apply_for_loan,
    create_account,
    notify,
    post_transaction,
    transfer,
)
from app.models import CardStatus  # noqa: E402


def seed(reset: bool = False) -> None:
    if reset:
        print("Dropping all tables…")
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print("Schema ready.")

    db = SessionLocal()
    try:
        rules.ensure_defaults(db)
        db.commit()
        rules.warm_cache(db)

        if db.scalar(select(Customer.id).limit(1)):
            print("Data already present — skipping seed. Use --reset to rebuild.")
            return

        password = hash_secret(settings.demo_password)

        def make_customer(first, last, email, role=Role.customer.value, kyc=KycStatus.verified.value,
                          phone="+40 700 000 000", address="Str. Franceza 12, Bucharest") -> Customer:
            customer = Customer(
                email=email,
                password_hash=password,
                first_name=first,
                last_name=last,
                phone=phone,
                address=address,
                date_of_birth=date(1990, 5, 14),
                role=role,
                kyc_status=kyc,
            )
            db.add(customer)
            db.flush()
            return customer

        admin = make_customer("Dana", "Ionescu", "admin@danube.demo", Role.admin.value)
        alex = make_customer("Alex", "Popescu", "alex@danube.demo")
        maria = make_customer("Maria", "Rusu", "maria@danube.demo", kyc=KycStatus.pending.value)
        andrei = make_customer("Andrei", "Marin", "andrei@danube.demo")
        db.flush()

        alex_checking = create_account(db, alex, "checking", "Everyday checking", opening_cents=320_000,
                                       overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000))
        alex_savings = create_account(db, alex, "savings", "Rainy day fund", opening_cents=1_450_000,
                                      interest_bps=rules.get_int(db, "accounts.savings_interest_bps", 350))
        maria_checking = create_account(db, maria, "checking", "Main account", opening_cents=180_000,
                                        overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000))
        andrei_checking = create_account(db, andrei, "checking", "Main account", opening_cents=95_000,
                                         overdraft_cents=rules.get_int(db, "accounts.checking_overdraft_cents", 50_000))
        db.flush()

        # --- Cards (Luhn-valid PANs, PIN hashed like the original project) ---
        def issue(account: Account, customer: Customer, pin: str, limit: int, brand="Visa") -> Card:
            pan = generate_card_number()
            today = date.today()
            card = Card(
                account_id=account.id,
                customer_id=customer.id,
                pan_encrypted=encrypt_pan(pan),
                last4=pan[-4:],
                brand=brand,
                exp_month=today.month,
                exp_year=today.year + 4,
                pin_hash=hash_secret(pin),
                daily_limit_cents=limit,
                status=CardStatus.active.value,
            )
            db.add(card)
            return card

        alex_card = issue(alex_checking, alex, "1234", 250_000)
        maria_card = issue(maria_checking, maria, "4321", 150_000)
        db.flush()

        # --- Beneficiaries ---
        db.add_all(
            [
                Beneficiary(customer_id=alex.id, name="Maria Rusu", account_number=maria_checking.account_number),
                Beneficiary(customer_id=alex.id, name="City Utilities", account_number=andrei_checking.account_number),
                Beneficiary(customer_id=maria.id, name="Alex Popescu", account_number=alex_checking.account_number),
            ]
        )

        # --- Historic activity (backdated a bit for a realistic chart) ---
        now = utcnow()
        history = [
            (alex_checking, -4_250, "Card purchase — Grocery Market", TxType.card_payment, 3),
            (alex_checking, -1_299, "Card purchase — Streaming Plus", TxType.card_payment, 5),
            (alex_checking, -60_000, "Rent — October", TxType.transfer, 8),
            (alex_checking, 85_000, "Salary — Danube Labs", TxType.deposit, 9),
            (alex_checking, -12_500, "Card purchase — Fuel Station", TxType.card_payment, 11),
            (alex_savings, 50_000, "Monthly savings transfer", TxType.deposit, 12),
            (alex_checking, -2_990, "Card purchase — Corner Coffee", TxType.card_payment, 1),
            (alex_checking, -18_900, "Card purchase — Electronics Hub", TxType.card_payment, 2),
        ]
        for account, amount, description, kind, days_ago in history:
            tx = post_transaction(
                db,
                tx_type=kind,
                amount_cents=abs(amount),
                currency=account.currency,
                description=description,
                entries=[(account, amount)],
                actor=alex,
            )
            tx.created_at = now - timedelta(days=days_ago)

        transfer(db, actor=alex, from_account_id=alex_checking.id,
                 to_account_number=maria_checking.account_number, amount_cents=4_500,
                 description="Dinner split")
        transfer(db, actor=maria, from_account_id=maria_checking.id,
                 to_account_number=alex_checking.account_number, amount_cents=12_000,
                 description="Weekend trip")

        # --- Loans: one pending, one active for Maria ---
        pending = apply_for_loan(
            db, customer=alex, account=alex_checking, principal_cents=800_000, months=24, rate_bps=990,
            purpose="Kitchen renovation",
        )
        maria_loan = apply_for_loan(
            db, customer=maria, account=maria_checking, principal_cents=300_000, months=12, rate_bps=1_090,
            purpose="Laptop upgrade",
        )
        approve_loan(db, loan=maria_loan, approver=admin)

        # --- Notifications ---
        notify(db, alex.id, "Card issued", f"Your debit card ending {alex_card.last4} is ready.")
        notify(db, alex.id, "Statement available", "Your October statement is ready to download.")

        db.commit()
        print("Seed complete.")
        print(f"  admin@danube.demo / {settings.demo_password}  (admin)")
        print(f"  alex@danube.demo  / {settings.demo_password}  (customer)")
        print(f"  maria@danube.demo / {settings.demo_password}  (customer, KYC pending)")
        print(f"  andrei@danube.demo / {settings.demo_password} (customer)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the banking demo database")
    parser.add_argument("--reset", action="store_true", help="Drop all tables first")
    args = parser.parse_args()
    seed(reset=args.reset)
