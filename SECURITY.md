# Security policy

## Supported versions

This is a demonstration project. Only the latest commit on the default branch is supported.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through GitHub's
[security advisory](https://github.com/FiredMosquito831/simple-banking-system/security/advisories/new)
feature, or email the maintainers at the address on their GitHub profile.

Include:

- affected route or component,
- reproduction steps,
- expected vs. actual behaviour,
- any suggested fix.

You can expect an acknowledgement within a few days.

## Scope and known limitations

Danube Bank is **demo software** and is intentionally not hardened for production. By design:

- The customer card-reveal endpoint returns the full PAN (toggleable in Settings).
- Login throttling is a per-session counter, not a distributed rate limiter.
- There is no TLS, MFA, email verification, secret rotation or real KYC provider.
- The operator SQL console can write to the database when `ALLOW_ADMIN_SQL_WRITE=1`.

Reports about these documented limitations will be closed as out of scope. We do welcome reports about
broken ownership checks, SQL injection, XSS, CSRF bypass, authentication bypass or ledger corruption.
