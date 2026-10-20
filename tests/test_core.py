"""Unit tests for card numbering, hashing and loan maths."""
from __future__ import annotations

import pytest

from app.security import generate_card_number, hash_secret, luhn_check_digit, luhn_valid, verify_secret
from app.services.banking import annuity_payment, build_schedule, quote_loan
from app.utils import fmt_account, money


@pytest.mark.parametrize("partial", ["40000012345678", "40000000000000", "41111111111111"])
def test_luhn_check_digit_produces_valid_number(partial):
    number = partial + luhn_check_digit(partial)
    assert luhn_valid(number)
    # changing any digit should break the checksum
    broken = str((int(number[-1]) + 1) % 10)
    assert not luhn_valid(number[:-1] + broken)


def test_generated_card_numbers_are_luhn_valid_and_unique():
    numbers = {generate_card_number() for _ in range(200)}
    assert len(numbers) > 190
    for number in numbers:
        assert len(number) == 16
        assert number.startswith("400000")
        assert luhn_valid(number)


def test_secret_hashing_roundtrip():
    stored = hash_secret("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert verify_secret("correct horse battery staple", stored)
    assert not verify_secret("wrong password", stored)
    assert not verify_secret("", "not-a-valid-hash")


def test_annuity_payment_matches_quote():
    payment = annuity_payment(1_000_000, 990, 24)
    quote = quote_loan(1_000_000, 990, 24)
    assert quote["monthly_payment_cents"] == payment
    assert quote["total_repayable_cents"] == payment * 24
    assert quote["total_interest_cents"] > 0
    # zero-interest loan splits the principal evenly
    assert annuity_payment(1_200_000, 0, 12) == 100_000


def test_amortisation_schedule_repays_principal_exactly():
    principal, rate, months = 800_000, 1090, 12
    rows = build_schedule(principal, rate, months)
    assert len(rows) == months
    assert sum(r["principal_cents"] for r in rows) == principal
    assert [r["number"] for r in rows] == list(range(1, months + 1))
    assert rows[0]["interest_cents"] > rows[-1]["interest_cents"]  # interest front-loaded
    assert all(r["principal_cents"] > 0 for r in rows)


def test_money_formatting():
    assert money(123456) == "$1,234.56"
    assert money(-5000) == "−$50.00"
    assert money(5000, signed=True) == "+$50.00"
    assert fmt_account("DANB40001234567890") == "DANB 4000 1234 5678 90"
