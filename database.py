"""Database bootstrap for the Document Request Ledger.

* ensure_database()  - creates the PostgreSQL database named in DATABASE_URL if it is missing.
* init_db()          - creates all tables and installs the append-only guards (DB triggers).
* SessionLocal       - session factory used by the web app, services and seeder.

Run `python database.py` to do both steps from the command line.
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).resolve().parent / ".env")


def _database_url() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and put your PostgreSQL password in it."
        )
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if "<password>" in raw:
        raise RuntimeError(
            "DATABASE_URL in .env still contains the <password> placeholder. Replace it with your "
            "PostgreSQL password (URL-encoded) or run setup_and_run.ps1 again."
        )
    return raw


DATABASE_URL = _database_url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def ensure_database() -> bool:
    """Create the target database if it does not exist. Returns True if it was created."""
    url = make_url(DATABASE_URL)
    target = url.database
    if not target:
        raise RuntimeError("DATABASE_URL has no database name.")
    if target == "postgres":
        return False

    try:
        conn = psycopg2.connect(
            dbname="postgres",
            user=url.username,
            password=url.password,
            host=url.host or "localhost",
            port=url.port or 5432,
            connect_timeout=10,
        )
    except psycopg2.OperationalError as exc:
        raise RuntimeError(
            f"Could not connect to PostgreSQL at {url.host or 'localhost'}:{url.port or 5432} "
            f"as user '{url.username}'. Is the server running and the password in .env correct?\n{exc}"
        ) from exc

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
            if cur.fetchone():
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
            return True
    finally:
        conn.close()


# --- Append-only guards -----------------------------------------------------------------------
# The application layer never issues UPDATE/DELETE against request_event (see services.py and the
# ORM listeners in models.py). These triggers make the same promise at the database level so that
# even a stray query, a script or a console session cannot rewrite history.
_GUARD_DDL = [
    """
    CREATE OR REPLACE FUNCTION ledger_block_mutation() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'The request ledger is append-only - update, delete and truncate are not permitted'
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_request_event_append_only ON request_event;",
    """
    CREATE TRIGGER trg_request_event_append_only
        BEFORE UPDATE OR DELETE ON request_event
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
    """,
    "DROP TRIGGER IF EXISTS trg_request_event_no_truncate ON request_event;",
    """
    CREATE TRIGGER trg_request_event_no_truncate
        BEFORE TRUNCATE ON request_event
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_block_mutation();
    """,
    "DROP TRIGGER IF EXISTS trg_request_immutable ON request;",
    """
    CREATE TRIGGER trg_request_immutable
        BEFORE UPDATE OR DELETE ON request
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
    """,
    # Stage 5: a closed engagement is read-only at the database too - nothing new may be inserted
    # against it, and the engagement itself can no longer be changed or deleted.
    """
    CREATE OR REPLACE FUNCTION ledger_block_closed() RETURNS trigger AS $$
    DECLARE eid integer; eng_status text;
    BEGIN
        IF TG_TABLE_NAME = 'request' THEN
            eid := NEW.engagement_id;
        ELSE
            SELECT engagement_id INTO eid FROM request WHERE id = NEW.request_id;
        END IF;
        SELECT status INTO eng_status FROM engagement WHERE id = eid;
        IF eng_status = 'Closed' THEN
            RAISE EXCEPTION 'The engagement is closed - it is read-only'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    *[
        stmt
        for table in ("request", "request_event", "submission")
        for stmt in (
            f"DROP TRIGGER IF EXISTS trg_{table}_closed_readonly ON {table};",
            f"""
            CREATE TRIGGER trg_{table}_closed_readonly
                BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION ledger_block_closed();
            """,
        )
    ],
    """
    CREATE OR REPLACE FUNCTION ledger_block_closed_engagement() RETURNS trigger AS $$
    BEGIN
        IF OLD.status = 'Closed' THEN
            RAISE EXCEPTION 'The engagement is closed - it cannot be changed or reopened'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_engagement_closed_frozen ON engagement;",
    """
    CREATE TRIGGER trg_engagement_closed_frozen
        BEFORE UPDATE OR DELETE ON engagement
        FOR EACH ROW EXECUTE FUNCTION ledger_block_closed_engagement();
    """,
    # Pass 1 completion: a chase row is never deleted and only its delivered / opened flags may
    # change (false -> true).
    """
    CREATE OR REPLACE FUNCTION ledger_chase_guard() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'chase rows cannot be deleted' USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.request_id IS DISTINCT FROM OLD.request_id OR NEW.event_id IS DISTINCT FROM OLD.event_id
           OR NEW.at IS DISTINCT FROM OLD.at OR NEW.channel IS DISTINCT FROM OLD.channel
           OR NEW."by" IS DISTINCT FROM OLD."by" OR NEW.template IS DISTINCT FROM OLD.template
           OR (OLD.delivered AND NOT NEW.delivered) OR (OLD.opened AND NOT NEW.opened) THEN
            RAISE EXCEPTION 'a chase can only be marked delivered or opened' USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "DROP TRIGGER IF EXISTS trg_chase_guard ON chase;",
    """
    CREATE TRIGGER trg_chase_guard
        BEFORE UPDATE OR DELETE ON chase
        FOR EACH ROW EXECUTE FUNCTION ledger_chase_guard();
    """,
    # Stage 2: templates are versioned and immutable; suppression records are append-only.
    *[
        stmt
        for table in ("request_template", "template_item", "request_suppression", "arrival", "outbound_email")
        for stmt in (
            f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table};",
            f"""
            CREATE TRIGGER trg_{table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
            """,
        )
    ],
]

# Stage 1 databases already have `engagement` and `request`; create_all does not add columns to
# existing tables, so the Stage 2 columns are added here (idempotent).
_MIGRATIONS = [
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS entity_profile JSONB NOT NULL DEFAULT CAST('{}' AS jsonb)",
    "ALTER TABLE request ADD COLUMN IF NOT EXISTS template_item_id INTEGER "
    "REFERENCES template_item(id) ON DELETE RESTRICT",
    "CREATE INDEX IF NOT EXISTS ix_request_template_item_id ON request (template_item_id)",
    # Stage 3 adds the ARRIVAL_RECORDED event type, so the CHECK on request_event.event_type is
    # rebuilt from the current list. (DDL only - the append-only triggers guard rows, not schema.)
    # Stage 5: when the engagement was closed. Engagements closed earlier get the time of their
    # last recorded event (or the report date) - this runs before the freeze trigger is installed.
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ",
    "UPDATE engagement SET closed_at = COALESCE("
    "(SELECT max(e.at) FROM request_event e JOIN request r ON r.id = e.request_id WHERE r.engagement_id = engagement.id), "
    "CAST(report_date AS TIMESTAMPTZ), now()) WHERE status = 'Closed' AND closed_at IS NULL",
    # Pass 2 / Screen 4: owner-scoped client links. (The event_type CHECK below is rebuilt from
    # models.ALL_EVENT_TYPES, which now includes CLIENT_REPORTED_UNAVAILABLE.)
    "ALTER TABLE client_token ADD COLUMN IF NOT EXISTS owner_person VARCHAR(200)",
    # Pass 1 completion: engagement settings (the chase / outbound_email / client_token tables are
    # created by create_all).
    "ALTER TABLE request_template ADD COLUMN IF NOT EXISTS published_by VARCHAR(200)",
    "ALTER TABLE request_template ADD COLUMN IF NOT EXISTS change_note TEXT",
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS client_email VARCHAR(200)",
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS partner_email VARCHAR(200)",
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS age_warn_days INTEGER NOT NULL DEFAULT 14",
    "ALTER TABLE engagement ADD COLUMN IF NOT EXISTS age_escalate_days INTEGER NOT NULL DEFAULT 21",
    # Chases recorded before the chase table existed get a row each (delivery was not tracked then).
    "INSERT INTO chase (request_id, event_id, at, channel, \"by\", template, delivered, opened) "
    "SELECT e.request_id, e.id, e.at, COALESCE(e.channel, 'Manual'), e.actor, e.payload->>'template', FALSE, FALSE "
    "FROM request_event e WHERE e.event_type = 'CHASED' "
    "AND NOT EXISTS (SELECT 1 FROM chase c WHERE c.event_id = e.id)",
    "ALTER TABLE request_event DROP CONSTRAINT IF EXISTS ck_request_event_type",
    "ALTER TABLE request_event ADD CONSTRAINT ck_request_event_type CHECK (event_type IN ({event_types}))",
]


def init_db() -> None:
    """Create all tables (idempotent), apply column migrations and install the append-only triggers."""
    import models  # noqa: F401  (registers the tables on Base.metadata)

    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        event_types = ", ".join("'" + t + "'" for t in models.ALL_EVENT_TYPES)
        for statement in _MIGRATIONS:
            conn.execute(text(statement.replace("{event_types}", event_types)))
        for statement in _GUARD_DDL:
            conn.execute(text(statement))


def bootstrap() -> None:
    created = ensure_database()
    url = make_url(DATABASE_URL)
    print(f"[db] database '{url.database}' {'created' if created else 'already exists'}")
    init_db()
    print("[db] tables and append-only guards are in place")


if __name__ == "__main__":
    try:
        bootstrap()
    except Exception as exc:  # noqa: BLE001 - CLI entry point, show a readable message
        print(f"[db] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
