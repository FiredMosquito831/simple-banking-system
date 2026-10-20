"""Application configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Danube Bank"
    secret_key: str = "dev-only-insecure-secret-change-me"
    database_url: str = "mysql+pymysql://banker:bankerpass@127.0.0.1:3306/banking?charset=utf8mb4"

    # Demo conveniences
    demo_password: str = "Password123!"
    reveal_card_numbers: bool = True

    # Business rules (all money in minor units / cents)
    default_currency: str = "USD"
    bank_code: str = "DANB"
    debit_daily_limit_cents: int = 250_000
    savings_interest_bps: int = 350  # 3.50% p.a.
    checking_overdraft_limit_cents: int = 50_000
    loan_max_amount_cents: int = 5_000_000
    loan_min_amount_cents: int = 50_000

    session_cookie: str = "banking_session"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
