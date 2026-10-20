"""Integration tests for the ledger. Require the MySQL container from docker-compose."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db import Base, SessionLocal, engine
from app.models import Customer, LedgerEntry
from app.security import hash_secret
from app.services.banking import (
    BankingError,
    create_account,
    deposit,
    transfer,
    withdraw,
)


@pytest.fixture()
def db():
    try:
        Base.metadata.create_all(engine)
        session = SessionLocal()
        session.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("MySQL is not running — start it with `docker compose up -d`")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_customer(db, first="Test", last="Customer") -> Customer:
    customer = Customer(
        email=f"{uuid.uuid4().hex[:10]}@test.local",
        password_hash=hash_secret("Password123!"),
        first_name=first,
        last_name=last,
    )
    db.add(customer)
    db.flush()
    return customer


def test_transfer_is_balanced_and_updates_both_balances(db):
    alice, bob = make_customer(db, "Alice"), make_customer(db, "Bob")
    a = create_account(db, alice, "checking", opening_cents=100_000)
    b = create_account(db, bob, "checking", opening_cents=0)

    tx = transfer(
        db, actor=alice, from_account_id=a.id, to_account_number=b.account_number,
        amount_cents=25_000, description="Rent share",
    )

    assert a.balance_cents == 75_000
    assert b.balance_cents == 25_000
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_id == tx.id)).all()
    assert len(entries) == 2
    assert sum(e.amount_cents for e in entries) == 0
    balances = {e.account_id: e.balance_after_cents for e in entries}
    assert balances[a.id] == 75_000 and balances[b.id] == 25_000
    assert tx.status == "completed" and tx.reference.startswith("TXN-")


def test_transfer_rejects_insufficient_funds(db):
    alice, bob = make_customer(db, "Alice"), make_customer(db, "Bob")
    a = create_account(db, alice, "checking", opening_cents=10_000, overdraft_cents=0)
    b = create_account(db, bob, "checking", opening_cents=0)
    with pytest.raises(BankingError):
        transfer(db, actor=alice, from_account_id=a.id, to_account_number=b.account_number, amount_cents=10_001)
    assert a.balance_cents == 10_000


def test_overdraft_facility_is_respected(db):
    alice = make_customer(db)
    a = create_account(db, alice, "checking", opening_cents=1_000, overdraft_cents=5_000)
    withdraw(db, actor=alice, account=a, amount_cents=6_000)
    assert a.balance_cents == -5_000
    with pytest.raises(BankingError):
        withdraw(db, actor=alice, account=a, amount_cents=1)


def test_cannot_transfer_from_someone_elses_account(db):
    alice, bob = make_customer(db, "Alice"), make_customer(db, "Bob")
    a = create_account(db, alice, "checking", opening_cents=5_000)
    b = create_account(db, bob, "checking", opening_cents=5_000)
    with pytest.raises(BankingError):
        transfer(db, actor=bob, from_account_id=a.id, to_account_number=b.account_number, amount_cents=100)


def test_idempotency_key_returns_same_transaction(db):
    alice, bob = make_customer(db, "Alice"), make_customer(db, "Bob")
    a = create_account(db, alice, "checking", opening_cents=50_000)
    b = create_account(db, bob, "checking", opening_cents=0)
    key = uuid.uuid4().hex
    tx1 = transfer(db, actor=alice, from_account_id=a.id, to_account_number=b.account_number,
                   amount_cents=1_000, idempotency_key=key)
    tx2 = transfer(db, actor=alice, from_account_id=a.id, to_account_number=b.account_number,
                   amount_cents=1_000, idempotency_key=key)
    assert tx1.id == tx2.id
    assert a.balance_cents == 49_000


def test_deposit_and_withdraw_record_entries(db):
    alice = make_customer(db)
    a = create_account(db, alice, "checking", opening_cents=0)
    deposit(db, actor=alice, account=a, amount_cents=20_000)
    withdraw(db, actor=alice, account=a, amount_cents=5_000)
    assert a.balance_cents == 15_000
    entries = db.scalars(
        select(LedgerEntry).where(LedgerEntry.account_id == a.id).order_by(LedgerEntry.id)
    ).all()
    assert [e.amount_cents for e in entries] == [20_000, -5_000]
    assert [e.balance_after_cents for e in entries] == [20_000, 15_000]
