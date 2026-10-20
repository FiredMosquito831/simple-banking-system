"""Password/PIN hashing, card number generation (Luhn) and card data encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet

from .config import settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _derive_key(secret: str) -> tuple[bytes, bytes]:
    salt = os.urandom(16)
    key = hashlib.scrypt(secret.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return salt, key


def hash_secret(secret: str) -> str:
    salt, key = _derive_key(secret)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(
            secret.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(key_hex) // 2
        )
        return hmac.compare_digest(key.hex(), key_hex)
    except (ValueError, AttributeError):
        return False


# --- Card data -------------------------------------------------------------
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt_pan(pan: str) -> str:
    return _fernet.encrypt(pan.encode()).decode()


def decrypt_pan(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def luhn_check_digit(partial: str) -> str:
    """Return the check digit that makes `partial + digit` pass the Luhn test."""
    total = 0
    for i, ch in enumerate(reversed(partial)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def generate_card_number() -> str:
    """16-digit Luhn-valid number with the demo BIN 400000."""
    body = "400000" + "".join(str(secrets.randbelow(10)) for _ in range(9))
    return body + luhn_check_digit(body)


def generate_pin() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def generate_account_number() -> str:
    """IBAN-like local account number: DANB + branch (4) + serial (10)."""
    branch = "4000"
    serial = f"{secrets.randbelow(10**10):010d}"
    return f"{settings.bank_code}{branch}{serial}"


def csrf_token() -> str:
    return secrets.token_urlsafe(24)
