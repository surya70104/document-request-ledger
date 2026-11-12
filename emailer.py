"""Outbound email for the Document Request Ledger.

Every email is recorded in the `outbound_email` table (the Outbox page shows it), whether or not an
SMTP server is configured:

  * SMTP_HOST empty  -> status 'logged': nothing leaves the machine, the email is only in the Outbox.
                        Links in it work on localhost, so the whole client flow can be tried offline.
  * SMTP_HOST set    -> the email is handed to the SMTP server first; 'sent' on success. If the server
                        refuses it, EmailDeliveryError is raised so the caller can roll back (a request
                        is never marked Sent when the client was not actually told).

Settings (.env): SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_STARTTLS (true),
PUBLIC_BASE_URL (the address clients can reach; default http://APP_HOST:APP_PORT).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.orm import Session

from models import OutboundEmail, utcnow


class EmailDeliveryError(Exception):
    """The SMTP server did not accept the message."""


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip())


def from_address() -> str:
    return (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "requests@ledger.local").strip()


def public_base_url() -> str:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"http://{os.getenv('APP_HOST', '127.0.0.1')}:{os.getenv('APP_PORT', '8015')}"


def client_link_url(token: str) -> str:
    return f"{public_base_url()}/c/{token}"


def _smtp_send(msg: EmailMessage) -> None:
    """Hand one message to the configured SMTP server. Separate function so tests can replace it."""
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "")
    starttls = os.getenv("SMTP_STARTTLS", "true").strip().lower() not in ("0", "false", "no", "off")
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        server.ehlo()
        if port != 465 and starttls and server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        if user:
            server.login(user, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - closing a dead connection is not an error worth reporting
            pass


def dispatch(
    session: Session,
    *,
    kind: str,
    to_address: str,
    subject: str,
    body: str,
    created_by: str,
    engagement_id: Optional[int] = None,
    request_id: Optional[int] = None,
    token_id: Optional[int] = None,
) -> OutboundEmail:
    """Send (or log) one email and add its Outbox row to the session (the caller commits).
    Raises EmailDeliveryError when SMTP is configured and refuses the message."""
    row = OutboundEmail(
        created_at=utcnow(),
        kind=kind,
        engagement_id=engagement_id,
        request_id=request_id,
        token_id=token_id,
        to_address=to_address,
        from_address=from_address(),
        subject=subject,
        body=body,
        status="logged",
        created_by=created_by,
    )
    if smtp_configured():
        msg = EmailMessage()
        msg["From"] = row.from_address
        msg["To"] = to_address
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            _smtp_send(msg)
        except Exception as exc:  # noqa: BLE001 - any SMTP/network failure
            raise EmailDeliveryError(f"The email to {to_address} could not be sent: {exc}") from exc
        row.status = "sent"
    session.add(row)
    session.flush()
    return row


def record_failure(session: Session, *, kind: str, to_address: str, subject: str, body: str,
                   created_by: str, error: str, engagement_id: Optional[int] = None) -> None:
    """Keep a trace of an email that SMTP refused (called after the failed transaction rolled back)."""
    session.add(OutboundEmail(
        created_at=utcnow(), kind=kind, engagement_id=engagement_id, to_address=to_address,
        from_address=from_address(), subject=subject, body=body, status="failed",
        error=error[:2000], created_by=created_by,
    ))
    session.commit()
