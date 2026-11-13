# OAK Audit — Document Request Ledger

Audit document request management module covering Stages 1–8 of the specification: request-level tracking (`REQ-XXX-NNN`), append-only audit trail, versioned templates, no-login client portal, email & WhatsApp channels, SA 230 binder provenance, DPDP Rules 2025 retention/purge policies, and downstream handoff APIs (Financial Spine & EQR readiness).

## Setup & Run (Windows PowerShell)

1. Copy `.env.example` to `.env` and set your PostgreSQL `DATABASE_URL`.
2. Start the application and run database migrations:

       powershell -ExecutionPolicy Bypass -File .\setup_and_run.ps1

3. Open `http://127.0.0.1:8015` in your browser.

## Running Automated Tests

    .\venv\Scripts\Activate.ps1
    pytest

All 49 invariant and acceptance tests pass.
