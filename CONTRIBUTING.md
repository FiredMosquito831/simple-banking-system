# Contributing

Thanks for taking a look. This project is meant to be read, forked and extended.

## Development setup

```bash
docker compose up -d                 # MySQL on :3306
python -m venv .venv
.venv\Scripts\activate               # Windows (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
cp .env.example .env
python -m scripts.seed
uvicorn app.main:app --reload        # :8000
uvicorn admin_app.main:app --port 8001
```

Run the tests before opening a pull request:

```bash
pytest
```

If MySQL is not running the ledger tests skip; unit tests still run.

## Ground rules for changes

- **Money only moves through `app/services/banking.py`.** Do not mutate `Account.balance_cents`
  directly — operators use the same service layer as customers.
- **Never edit ledger history.** Corrections are reversals, not deletes.
- **New business rules go in `settings`**, not in hardcoded constants, so operators can change them.
- **Every operator mutation must be audited** with `audit(db, actor, action, entity, …)`.
- **Add tests** for new ledger or pricing behaviour.
- Keep templates accessible: labelled inputs, `alt` text, keyboard-friendly controls.

## Pull requests

1. Fork and branch from `main`.
2. Keep commits focused and describe the *why*.
3. Include screenshots for UI changes.
4. Make sure `pytest` and `python -m compileall app admin_app scripts` pass.

By contributing you agree that your work is licensed under the repository's MIT license.
