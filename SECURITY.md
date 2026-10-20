# Security Policy

## Reporting a vulnerability

Please **do not open a public issue or pull request** for security problems.

Email **info@nab.rs** with:

- what is affected (URL, endpoint, bot command or file),
- steps to reproduce,
- the impact you observed.

We acknowledge reports within 3 business days and keep you informed until the fix is deployed.

## Scope

| In scope | Out of scope |
|---|---|
| `app.nab.rs` web app and API | Denial-of-service and load testing |
| Telegram bot [@nabrs_bot](https://t.me/nabrs_bot) | Social engineering of staff or users |
| Code in this repository | Third-party procurement portals we read from |

## How we protect data

- Tenant isolation is enforced by PostgreSQL row-level security (`FORCE RLS`); the web app and bot use a restricted database role.
- Secrets live only on the server (`/etc/<app>/`), never in the repository; templates are in `infra/secrets/` and `deploy/.env.template`.
- Personal data (email, phone) is never written to logs.
- Strict Content Security Policy (`script-src 'self'`), signed sessions, rate limits on sign-in.
