"""Test harness for the Document Request Ledger invariants.

SAFETY: the tests never touch your real database. DATABASE_URL (or TEST_DATABASE_URL) is read only
to learn the server and credentials; the database actually used is always named `<name>_test`, and
it is DROPPED AND RECREATED at the start of every test run. Uploads go to a temporary folder.

Run:  pytest            (from the project folder, inside the venv)
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

_base = (os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
if not _base or "<password>" in _base:
    pytest.exit(
        "Set TEST_DATABASE_URL (or DATABASE_URL in .env) so the tests know which PostgreSQL server to use.",
        returncode=2,
    )
_url = make_url(_base.replace("postgres://", "postgresql://", 1))
if not (_url.database or "").endswith("_test"):
    _url = _url.set(database=f"{_url.database or 'oak_request_ledger'}_test")
TEST_DB_NAME = _url.database
# Must be set BEFORE database.py / services.py are imported.
os.environ["DATABASE_URL"] = _url.render_as_string(hide_password=False)
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="ledger_test_uploads_")
# Never send real email from the tests (even if .env has SMTP settings): local-outbox mode.
os.environ["SMTP_HOST"] = ""
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
# WhatsApp: simulator mode, and no background assembly task (tests assemble explicitly).
os.environ["WHATSAPP_PROVIDER"] = ""
os.environ["WHATSAPP_TOKEN"] = ""
os.environ["WHATSAPP_FLUSH_INTERVAL"] = "0"


@pytest.fixture(scope="session", autouse=True)
def _fresh_database():
    import psycopg2
    from psycopg2 import sql

    assert TEST_DB_NAME.endswith("_test"), "refusing to reset a database that is not a *_test database"
    conn = psycopg2.connect(
        dbname="postgres", user=_url.username, password=_url.password,
        host=_url.host or "localhost", port=_url.port or 5432,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB_NAME)))
    conn.close()

    import database
    import services
    from template_data import TEMPLATES

    database.ensure_database()
    database.init_db()
    with database.SessionLocal() as s:
        services.seed_templates(s, TEMPLATES)
    yield


@pytest.fixture()
def session(_fresh_database):
    from database import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def engine_db(_fresh_database):
    from database import engine

    return engine


@pytest.fixture()
def make_engagement(session):
    """Factory: a fresh, uniquely named Active engagement (tests never share rows)."""
    import services
    from models import Engagement

    def _make(profile=None, **kw):
        eng = Engagement(
            name=f"Test engagement {uuid.uuid4().hex[:10]}",
            client_name=kw.pop("client_name", "Test Client Pvt Ltd"),
            engagement_type=kw.pop("engagement_type", "Statutory audit"),
            period_from=kw.pop("period_from", None) or __import__("datetime").date(2025, 4, 1),
            period_to=kw.pop("period_to", None) or __import__("datetime").date(2026, 3, 31),
            status="Active",
            entity_profile=services.normalise_profile(profile or {}),
            **kw,
        )
        session.add(eng)
        session.commit()
        return eng

    return _make


@pytest.fixture()
def make_request(session):
    """Factory: a request in the engagement, optionally released to the client (Sent)."""
    import services

    def _make(eng, document=None, *, significance="Routine", owner="Owner One (Accountant)", send=True, **kw):
        req = services.create_request(
            session, engagement_id=eng.id, document=document or f"Doc {uuid.uuid4().hex[:8]}",
            owner_person=owner, actor="Tester", significance=significance, **kw,
        )
        if send:
            services.append_event(session, req.id, "SENT", "Tester", channel="Email")
        return req

    return _make


@pytest.fixture()
def client(session):
    """A TestClient for the real app. App start-up runs DDL, so this test's session lets go of its
    locks first."""
    from fastapi.testclient import TestClient

    import main

    session.rollback()
    with TestClient(main.app) as c:
        yield c
