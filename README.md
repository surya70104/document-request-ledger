# OAK Audit — Document Request Ledger

A dated, append-only record of what an audit firm asked its client for, when, how often, and what came back — built so the person on the other end can answer from a phone. It implements the spec `OAK_Audit_Document_Request_Ledger.docx` (Whhoohh Path · OAK Audit).

## Overview & key features

- **The request is the unit of record**, not the file. Every request has a durable ref `REQ-XXX-NNN`; files come and go against it.
- **Append-only `request_event` log.** No application path updates or deletes an event, and PostgreSQL triggers enforce the same at the database. State, attempt count and chase count are *derived* from events.
- **11 lifecycle states** with a strict transition table: Drafted, Sent, Acknowledged, Submitted, Under review, Accepted, Rejected, Re-requested, Waived, Not applicable, Unfulfilled at report date.
- **Six versioned templates** (statutory audit, tax audit, GST annual return, bank audit, internal audit, certification) with entity-profile conditions. A suppressed item records the condition that suppressed it. Published versions are immutable; the firm maintains templates from the Templates screen.
- **Carry-forward** from the prior year, explicit and tagged with last year's fulfilment history; closed prior-year data is never altered.
- **Rejection with eight reason codes**, each phrased as what the client should do differently; automatic linked re-request and counted attempts.
- **SHA-256 content-hash de-duplication** shared by every channel: a re-send is an `arrival` row, never a second submission.
- **No-login client portal** (`/c/{token}`): whole-engagement or owner-scoped tokenised links, five requests at a time in priority order, multi-page photo capture assembled into one PDF, and "I don't have this" (with undo both ways).
- **Email and WhatsApp channels.** Email goes out through SMTP or a local Outbox; WhatsApp is two-way with a built-in simulator, quiet-window multi-photo assembly and explicit (never guessed) request matching.
- **Outstanding Board** with four cuts (age, owner, significance, chase count); Material items are pinned at the top of every cut. Configurable ageing thresholds and a partner escalation digest.
- **Close-out (Screen 5):** closing freezes open requests as *Unfulfilled at report date* (never deleted, never auto-waived), revokes live client links, and locks the engagement in the database. Includes per-item report impact, SA 705 wording, an SA 580 representations schedule and a tamper-evident exportable thread with SHA-256 fingerprint.
- **SA 230 binder provenance:** every filed document carries a request id or an explicit "obtained by us" basis; there is no third option, enforced by service checks, a CHECK constraint and a trigger.
- **DPDP Rules 2025 retention and purge:** every stored file has a retention basis (`audit_file` 7 years, `statutory` with the obligation named, or `none`) and a purge date; a job erases expired `none` files and logs `FILE_PURGED`.
- **Downstream hand-offs:** Financial Spine, EQR Readiness and the frozen unfulfilled list for the opinion (see [Hand-off endpoints](#downstream-hand-off-endpoints)).

## Project structure

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app: HTML screens, JSON endpoints, client portal, WhatsApp webhook, hand-off APIs, and the background WhatsApp assembler and retention jobs. |
| `services.py` | All business logic: transition engine, submissions and review, de-duplication, board and chase, tokens, batch release, close-out, binder, retention, hand-offs. |
| `database.py` | Engine and session, database creation, idempotent migrations and the PostgreSQL triggers (append-only, closed-engagement lock, binder and retention guards). Running `python database.py` creates the database and tables. |
| `models.py` | SQLAlchemy 2 schema and vocabulary (states, event types, reason codes, retention constants). |
| `conditions.py` | Safe evaluator for template condition expressions against the entity profile. |
| `emailer.py` | SMTP sending and local Outbox mode. |
| `whatsapp.py` | WhatsApp provider adapters (simulator, `meta`, `generic`) and webhook parsing. |
| `template_data.py` | The six starter requirement-list templates. |
| `seed_demo.py` | Seeds the Acme demo engagement plus a closed prior year. Safe to re-run. |
| `setup_and_run.ps1` | One-shot Windows setup and launch. |
| `templates/` | Jinja2 + Tailwind (CDN) pages and partials. |
| `tests/` | `conftest.py` (isolated test database) and `test_invariants.py`. |
| `requirements.txt`, `pytest.ini`, `.env.example` | Dependencies, test config, configuration template. |

## Prerequisites & environment setup

- **Windows with PowerShell** (the scripts are written for it).
- **Python 3.10 or newer** on `PATH`.
- **PostgreSQL** running locally (default `localhost:5432`) with a user that can create databases (normally `postgres`). The app creates `oak_request_ledger` itself if it is missing.
- Internet access in the browser for the Tailwind CDN.

Configuration lives in `.env` (git-ignored). `setup_and_run.ps1` copies `.env.example` to `.env` and asks for your PostgreSQL password. If you edit it by hand, URL-encode special characters in the password (`@` → `%40`, `#` → `%23`, `/` → `%2F`).

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string. Replace `<password>`. | `postgresql://postgres:<password>@localhost:5432/oak_request_ledger` |
| `APP_HOST`, `APP_PORT` | Local bind address and port. | `127.0.0.1`, `8015` |
| `FIRM_NAME` | Firm name printed on the exported audit thread. | `OAK Audit` |
| `SMTP_HOST` | SMTP server. **Empty = local Outbox mode**: emails are stored and shown on `/outbox`, nothing is sent. | empty |
| `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | SMTP port, login and sender address. | `587`, empty |
| `PUBLIC_BASE_URL` | Address clients reach the app on; used in the links inside emails. | `http://APP_HOST:APP_PORT` |
| `WHATSAPP_PROVIDER` | Empty = built-in **simulator** at `/whatsapp` (nothing is sent). `meta` = WhatsApp Business Cloud API. `generic` = any gateway accepting `POST {"to","body"}` with a Bearer token. | empty |
| `WHATSAPP_TOKEN` | API token for the live provider. | empty |
| `WHATSAPP_PHONE_ID` | Phone-number id (`meta`). | empty |
| `WHATSAPP_API_URL` | Gateway URL (`generic`). | empty |
| `WHATSAPP_VERIFY_TOKEN` | Token used when the provider verifies the webhook. | empty |
| `WHATSAPP_APP_SECRET` | Secret used to check the webhook signature. | empty |
| `WHATSAPP_ASSEMBLY_SECONDS` | Seconds of quiet after which a client's photos are assembled into one submission. | `120` |
| `NONE_RETENTION_DAYS` | Days a **rejected** file lingers before it is erased. | `30` |
| `PENDING_RETENTION_DAYS` | Safety window for a file still awaiting a decision. Undecided files are held, not erased. | `180` |
| `RETENTION_JOB_INTERVAL` | Seconds between purge-job runs (`0` disables the background job). | `86400` |

Two further optional variables are read by the code: `UPLOAD_DIR` (where files are stored) and `WHATSAPP_FLUSH_INTERVAL` (seconds between background assembly checks, default 30, `0` disables).

> **Live WhatsApp has not been tested** against a real provider; only the simulator has been exercised. There is also no firm-side authentication yet: "Acting as" in the header is a typed name.

## How to run & seed demo data

From PowerShell in the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_and_run.ps1
```

This creates the `venv`, installs requirements, prepares `.env` (asking for the PostgreSQL password the first time), creates the database and tables, seeds the demo engagement and starts the server. Then open **http://127.0.0.1:8015**.

Options:

```powershell
.\setup_and_run.ps1 -SkipSeed   # do not seed demo data
.\setup_and_run.ps1 -NoRun      # set everything up but do not start the server
```

Manual steps, if you prefer:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env          # then edit DATABASE_URL
.\venv\Scripts\python.exe database.py   # creates the database and tables (idempotent migrations)
.\venv\Scripts\python.exe seed_demo.py  # seeds Acme Tech Solutions FY 2025-26 and a closed FY 2024-25
.\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8015
```

`seed_demo.py` is safe to run repeatedly. After pulling new code, restart the server so migrations run against your existing data.

Useful screens: `/` ledger, `/outbox` emails, `/whatsapp` simulator, `/binder` SA 230 binder, `/retention` Retention & DPDP, `/health` health check.

## Downstream hand-off endpoints

All are read-only `GET` endpoints returning JSON, for `{id}` an engagement id.

| Endpoint | Purpose |
|---|---|
| `/api/engagements/{id}/handoff/financial-spine` | Accepted trial balance / financial statements, each with the request it arrived against, when it was received, from whom, and its SHA-256 — so a restated TB traces to the day it was received. |
| `/api/engagements/{id}/handoff/eqr-readiness` | EQR gate. Open requests flagged Material (or, after close-out, Material unfulfilled ones) are returned as blockers with owner, age, chases and attempts. |
| `/api/engagements/{id}/handoff/unfulfilled` | The frozen list of requests unfulfilled at the report date, with per-item report effect — the evidence base for SA 705 and the SA 580 representations. Before close-out it is a preview (`frozen: false`). |

The earlier `/api/engagements/{id}/eqr-readiness` and `/api/engagements/{id}/unfulfilled` paths remain as aliases.

## Running the automated test suite

```powershell
.\venv\Scripts\Activate.ps1
pytest
```

The suite has **49 tests** covering all ten §12 acceptance tests plus the wider invariants (state transitions, closed-engagement lock, channel de-duplication, carry-forward, WhatsApp, binder provenance, retention and purge, hand-offs).

**Your real data is safe:** `DATABASE_URL` (or `TEST_DATABASE_URL`) is used only to learn the server and credentials. Tests always run against a database named `<name>_test`, which is **dropped and recreated at the start of every run**, with uploads in a temporary folder and SMTP and WhatsApp disabled.

## Known limits

- Audit-file and statutory files are never purged automatically, even after their retention date; only `none`-basis files are erased.
- No firm-side authentication.
- Live WhatsApp providers are untested.
