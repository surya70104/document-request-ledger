"""Business logic for the Document Request Ledger.

Everything that changes a request goes through this module, and the only thing it ever does to
history is INSERT a `request_event` row. There is no update or delete of events anywhere.
"""
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

import emailer
from conditions import (
    CRORE,
    PROFILE_FIELDS,
    ConditionError,
    evaluate,
    explain,
    normalise_profile,
)
from models import (
    Arrival,
    Chase,
    ClientToken,
    OutboundEmail,
    REASON_CODE_ALIASES,
    Submission,
    SubmissionFile,
    CHANNELS,
    ENGAGEMENT_TYPES,
    RequestSuppression,
    RequestTemplate,
    TemplateItem,
    REASON_CODES,
    SIGNIFICANCE,
    TERMINAL_STATES,
    Engagement,
    EVENT_LABEL,
    EventType as E,
    Request,
    RequestEvent,
    utcnow,
)


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------
class LedgerError(Exception):
    """A rule of the ledger was broken (maps to HTTP 400)."""


class NotFoundError(LedgerError):
    """The engagement or request does not exist (maps to HTTP 404)."""


class InvalidTransition(LedgerError):
    """The event is not a legal move from the request's current state."""


class DuplicateRequestError(LedgerError):
    """A live request for the same document already exists in the engagement (maps to HTTP 409)."""

    def __init__(self, existing: Request):
        self.existing = existing
        super().__init__(
            f"A live request for this document already exists: {existing.ref} "
            f"({existing.state}), owned by {existing.owner_person}."
        )


# --------------------------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------------------------
# For each current state, the events that may be appended next. UNFULFILLED is only ever written
# by close_engagement(); RE_REQUESTED only ever follows a REJECTED automatically; DRAFTED only by
# create_request(). None of those three can be appended through append_event().
ALLOWED_NEXT: dict[str, set[str]] = {
    "Drafted": {E.SENT, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE},
    "Sent": {E.ACKNOWLEDGED, E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL,
             E.UNAVAILABLE_CLEARED},
    "Acknowledged": {E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL,
                     E.UNAVAILABLE_CLEARED},
    "Submitted": {E.UNDER_REVIEW, E.ACCEPTED, E.REJECTED, E.WAIVED, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Under review": {E.ACCEPTED, E.REJECTED, E.WAIVED, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Rejected": {E.RE_REQUESTED},
    "Re-requested": {
        E.ACKNOWLEDGED, E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL,
        E.UNAVAILABLE_CLEARED,
    },
    "Accepted": {E.NOTE, E.ARRIVAL},
    "Waived": {E.NOTE, E.ARRIVAL},
    "Not applicable": {E.NOTE, E.ARRIVAL},
    "Unfulfilled at report date": {E.NOTE, E.ARRIVAL},
}

# Event types a caller may not append directly. SUBMITTED and ARRIVAL_RECORDED are written by
# record_submission() (a submission always has a file); ACCEPTED/REJECTED of a pending submission
# go through review_submission().
SYSTEM_ONLY = {E.DRAFTED, E.RE_REQUESTED, E.UNFULFILLED, E.SUBMITTED, E.ARRIVAL, E.CLIENT_UNAVAILABLE}

# Display order for the action menu.
ACTION_ORDER = [
    E.SENT, E.ACKNOWLEDGED, E.CHASED, E.SUBMITTED, E.UNDER_REVIEW,
    E.ACCEPTED, E.REJECTED, E.WAIVED, E.NOT_APPLICABLE, E.UNAVAILABLE_CLEARED, E.NOTE,
]

CHANNEL_REQUIRED = {E.SENT, E.CHASED, E.ACKNOWLEDGED, E.SUBMITTED}
REASON_REQUIRED = {E.WAIVED, E.NOT_APPLICABLE}

# Keys a caller may put in the payload of each event type (anything else is dropped).
PAYLOAD_KEYS: dict[str, set[str]] = {
    E.SENT: {"note"},
    E.ACKNOWLEDGED: {"note"},
    E.CHASED: {"note", "template"},
    E.SUBMITTED: {"submitted_by", "filename", "note"},
    E.UNDER_REVIEW: {"note"},
    E.ACCEPTED: {"note"},
    E.REJECTED: {"detail", "note", "pages"},
    E.WAIVED: {"reason", "note"},
    E.NOT_APPLICABLE: {"reason", "note"},
    E.NOTE: {"note"},
    E.UNAVAILABLE_CLEARED: {"note"},
}


def pending_submission(request: Request) -> Optional[Submission]:
    """The submission awaiting a review decision, if any."""
    return next((sub for sub in reversed(request.submissions) if sub.outcome == "pending"), None)


def available_actions(request: Request) -> list[tuple[str, str]]:
    """(event_type, label) pairs that are legal for this request right now."""
    if request.engagement.status == "Closed":
        allowed = set()  # a closed engagement is strictly read-only
    else:
        allowed = ALLOWED_NEXT.get(request.state or "", set()) - SYSTEM_ONLY
        if pending_submission(request):
            allowed = allowed - {E.ACCEPTED, E.REJECTED}  # decided in the Audit Review panel
        if not request.reported_unavailable:
            allowed = allowed - {E.UNAVAILABLE_CLEARED}  # only meaningful after "I don't have this"
    labels = {**EVENT_LABEL, E.UNAVAILABLE_CLEARED: "Reopen - ask the client again"}
    return [(et, labels[et]) for et in ACTION_ORDER if et in allowed]


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def _aware(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return utcnow()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normalise_document(name: str) -> str:
    return " ".join(name.lower().split())


def _require_actor(actor) -> str:
    actor = _clean(actor)
    if not actor:
        raise LedgerError("Every event needs an actor - enter your name in 'Acting as'.")
    return actor


def _lock_engagement(session: Session, engagement_id: int) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.id == engagement_id).with_for_update()
    ).scalar_one_or_none()
    if eng is None:
        raise NotFoundError(f"Engagement {engagement_id} does not exist.")
    return eng


def _lock_request(session: Session, request_id: int) -> Request:
    """Load a request with its events, holding a row lock so concurrent writers serialise and
    `seq` stays gap-free."""
    req = session.execute(
        select(Request)
        .where(Request.id == request_id)
        .with_for_update()
        .options(
            selectinload(Request.events),
            selectinload(Request.submissions)
            .selectinload(Submission.files)
            .selectinload(SubmissionFile.arrivals),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if req is None:
        raise NotFoundError(f"Request {request_id} does not exist.")
    return req


def _insert_event(
    session: Session,
    request: Request,
    event_type: str,
    actor: str,
    channel: Optional[str],
    payload: dict,
    at: datetime,
) -> RequestEvent:
    """The one and only place a request_event row is written."""
    last = request.events[-1] if request.events else None
    if last is not None and at < last.at:
        raise LedgerError(
            "An event cannot be dated earlier than the one before it in this request's history."
        )
    ev = RequestEvent(
        seq=(last.seq + 1) if last else 1,
        event_type=event_type,
        actor=actor,
        at=at,
        channel=channel,
        payload=payload,
    )
    request.events.append(ev)
    session.flush()
    return ev


def reason_message(code: str, req: Optional[Request], pages: Optional[str] = None) -> str:
    """What the client is told for a rejection reason, with the request's own context filled in:
    the period asked for, the client entity, the pages that are missing, the document wanted."""
    label, base = REASON_CODES[code]
    if req is None:
        return base
    pages = _clean(pages)
    if code == "WRONG_PERIOD" and (req.period_from or req.period_to):
        return f"{base} The period asked for is {_period_phrase(req.period_from, req.period_to)}."
    if code == "WRONG_ENTITY":
        return f"{base} The document needed is for {req.engagement.client_name}."
    if code == "INCOMPLETE" and pages:
        return f"Pages are missing: {pages}."
    if code == "PASSWORD_PROTECTED":
        return f"{base} Please send the password by a separate route (a call or a separate message), not with the file."
    if code == "NOT_THE_DOCUMENT_ASKED_FOR":
        wanted = req.document + (f" - {req.description}" if req.description else "")
        return f"{base} What we need is: {wanted}"
    return base


def _build_payload(
    event_type: str, channel: Optional[str], raw: dict, req: Optional[Request] = None
) -> tuple[Optional[str], dict]:
    raw = {k: _clean(v) for k, v in (raw or {}).items()}
    channel = _clean(channel)

    if channel is not None and channel not in CHANNELS:
        raise LedgerError(f"Unknown channel '{channel}'. Use one of: {', '.join(CHANNELS)}.")
    if event_type in CHANNEL_REQUIRED and channel is None:
        raise LedgerError(f"'{EVENT_LABEL[event_type]}' needs the channel it happened on.")

    payload = {k: v for k, v in raw.items() if k in PAYLOAD_KEYS.get(event_type, set()) and v}

    if event_type in REASON_REQUIRED and not raw.get("reason"):
        raise LedgerError(f"'{EVENT_LABEL[event_type]}' is a recorded decision - a reason is required.")
    if event_type == E.NOTE and not raw.get("note"):
        raise LedgerError("A note cannot be empty.")
    if event_type == E.REJECTED:
        code = (raw.get("reason_code") or "").upper()
        code = REASON_CODE_ALIASES.get(code, code)
        if code not in REASON_CODES:
            raise LedgerError("Rejecting needs one of the eight reason codes.")
        label, _base = REASON_CODES[code]
        client_message = reason_message(code, req, raw.get("pages"))
        payload = {
            "reason_code": code,
            "reason_label": label,
            "client_message": client_message,
            **payload,
        }
    return channel, payload


# --------------------------------------------------------------------------------------------
# Creating a request
# --------------------------------------------------------------------------------------------
def create_request(
    session: Session,
    *,
    engagement_id: int,
    document: str,
    owner_person: str,
    actor: str,
    significance: str = "Routine",
    priority: int = 3,
    description: Optional[str] = None,
    period_from: Optional[date] = None,
    period_to: Optional[date] = None,
    template_item: Optional[str] = None,
    condition_basis: Optional[str] = None,
    carried_from_request_id: Optional[int] = None,
    carry_note: Optional[str] = None,
    template_item_id: Optional[int] = None,
    channel: str = "Manual",
    at: Optional[datetime] = None,
    commit: bool = True,
) -> Request:
    """Create a request and log its seq=1 DRAFTED event, atomically.

    Blocks (DuplicateRequestError) when the engagement already has a live request for the same
    document and period, so two team members never ask the client for the same paper twice.
    """
    actor = _require_actor(actor)
    document = _clean(document)
    owner_person = _clean(owner_person)
    description = _clean(description)
    if not document:
        raise LedgerError("A request needs a document name.")
    if not owner_person:
        raise LedgerError("Name the person on the client side who owns this request - not just 'the client'.")
    if significance not in SIGNIFICANCE:
        raise LedgerError("Significance must be Material or Routine.")
    if not 1 <= int(priority) <= 5:
        raise LedgerError("Priority must be between 1 (highest) and 5.")
    if period_from and period_to and period_from > period_to:
        raise LedgerError("'Period from' cannot be after 'period to'.")
    at = _aware(at)

    eng = _lock_engagement(session, engagement_id)  # serialises creation within the engagement
    if eng.status != "Active":
        raise LedgerError("This engagement is closed; no new requests can be raised.")

    existing_requests = session.execute(
        select(Request)
        .where(Request.engagement_id == engagement_id)
        .options(selectinload(Request.events), selectinload(Request.engagement))
    ).scalars().all()

    key = _normalise_document(document)
    for existing in existing_requests:
        if (
            existing.is_open
            and _normalise_document(existing.document) == key
            and existing.period_from == period_from
            and existing.period_to == period_to
        ):
            raise DuplicateRequestError(existing)

    ref = f"REQ-{eng.id:03d}-{len(existing_requests) + 1:03d}"
    req = Request(
        engagement_id=engagement_id,
        ref=ref,
        document=document,
        description=description,
        period_from=period_from,
        period_to=period_to,
        significance=significance,
        priority=int(priority),
        owner_person=owner_person,
        template_item=_clean(template_item),
        condition_basis=_clean(condition_basis),
        carried_from_request_id=carried_from_request_id,
        template_item_id=template_item_id,
        created_by=actor,
        created_at=at,
    )
    session.add(req)
    session.flush()

    _insert_event(
        session,
        req,
        E.DRAFTED,
        actor,
        _clean(channel),
        {
            "document": document,
            "significance": significance,
            "priority": int(priority),
            "owner_person": owner_person,
            **({"template_item": req.template_item} if req.template_item else {}),
            **({"condition_basis": req.condition_basis} if req.condition_basis else {}),
            **({"carried_forward": _clean(carry_note)} if _clean(carry_note) else {}),
        },
        at,
    )
    if commit:
        session.commit()
    return req


# --------------------------------------------------------------------------------------------
# The transition engine
# --------------------------------------------------------------------------------------------
def append_event(
    session: Session,
    request_id: int,
    event_type: str,
    actor: str,
    *,
    channel: Optional[str] = None,
    payload: Optional[dict] = None,
    at: Optional[datetime] = None,
    deliver=None,
) -> list[RequestEvent]:
    """Validate a transition and append it (never update, never delete).

    Rejecting appends TWO events in one transaction: REJECTED, then the automatic RE_REQUESTED that
    carries the new attempt number and a pointer to the rejection that caused it.
    Returns the list of events appended.
    """
    actor = _require_actor(actor)
    event_type = (event_type or "").strip().upper()
    if event_type not in EVENT_LABEL:
        raise LedgerError(f"Unknown event type '{event_type}'.")
    if event_type in SYSTEM_ONLY:
        raise InvalidTransition(f"'{EVENT_LABEL[event_type]}' cannot be recorded by hand.")

    try:
        req = _lock_request(session, request_id)
        state = req.state
        if req.engagement.status == "Closed":
            raise InvalidTransition("The engagement is closed and read-only: no further events can be recorded.")
        if event_type not in ALLOWED_NEXT.get(state or "", set()):
            raise InvalidTransition(
                f"Cannot record '{EVENT_LABEL[event_type]}' while the request is '{state}'."
            )

        if event_type == E.UNAVAILABLE_CLEARED and not req.reported_unavailable:
            raise InvalidTransition("The client has not reported this request as unavailable, so there is nothing to reopen.")

        if event_type in (E.ACCEPTED, E.REJECTED) and pending_submission(req):
            raise InvalidTransition(
                "A submission is awaiting review - use the Audit Review panel (Accept / Reject Document)."
            )

        channel, clean_payload = _build_payload(event_type, channel, {**(payload or {})}, req)
        at = _aware(at)

        if event_type == E.REJECTED:
            appended = _reject_and_rerequest(session, req, actor, channel, clean_payload, at)
        else:
            appended = [_insert_event(session, req, event_type, actor, channel, clean_payload, at)]
            if event_type == E.CHASED:
                # Section 9 `chase` row alongside the append-only CHASED event, in the same transaction.
                chase = Chase(
                    request_id=req.id, event_id=appended[0].id, at=appended[0].at, channel=channel,
                    by_whom=actor, template=clean_payload.get("template"),
                )
                session.add(chase)
                session.flush()
                if deliver is not None:
                    deliver(req, appended[0], chase)
        session.commit()
        return appended
    except Exception:
        session.rollback()
        raise


def _reject_and_rerequest(
    session: Session, req: Request, actor: str, channel: Optional[str], payload: dict, at: datetime
) -> list[RequestEvent]:
    """Append REJECTED and, immediately and in the same transaction, the automatic RE_REQUESTED
    that carries the next attempt number and points back at the rejection that caused it."""
    rejection = _insert_event(session, req, E.REJECTED, actor, channel, payload, at)
    re_requested = _insert_event(
        session,
        req,
        E.RE_REQUESTED,
        actor,
        "Manual",
        {
            "attempt": req.attempt_count + 1,
            "prior_rejection_seq": rejection.seq,
            "reason_code": payload["reason_code"],
            "reason_label": payload["reason_label"],
            "client_message": payload["client_message"],
            "automatic": True,
        },
        at,
    )
    return [rejection, re_requested]


# --------------------------------------------------------------------------------------------
# Submissions: upload, SHA-256 de-duplication, review
# --------------------------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR") or (Path(__file__).resolve().parent / "uploads"))
SUBMITTABLE_STATES = {"Sent", "Acknowledged", "Re-requested"}


def _safe_name(name: str) -> str:
    base = Path(str(name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ ()-]", "_", base).strip(" .")
    return (base or "file")[-120:]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_submission(
    session: Session,
    request_id: int,
    actor: str,
    files: list[tuple[str, bytes]],
    *,
    channel: str = "Web upload",
    submitted_by: Optional[str] = None,
    at: Optional[datetime] = None,
    upload_dir: Optional[Path] = None,
) -> dict:
    """Record what arrived against a request.

    Each file is hashed (SHA-256). A file whose hash already exists for THIS request is not stored
    again: an Arrival row ("identical content arrived again") and an ARRIVAL_RECORDED event are
    written instead. New content is saved under uploads/, grouped into ONE submission (page_order =
    upload order) and the request moves to Submitted via a SUBMITTED event. Everything commits
    together; if it fails, files written to disk are removed again.
    """
    actor = _require_actor(actor)
    channel = _clean(channel) or "Web upload"
    if channel not in CHANNELS:
        raise LedgerError(f"Unknown channel '{channel}'. Use one of: {', '.join(CHANNELS)}.")
    submitted_by = _clean(submitted_by)
    at = _aware(at)
    base = Path(upload_dir) if upload_dir else UPLOAD_DIR

    if not files:
        raise LedgerError("Choose at least one file to submit.")
    for name, data in files:
        if not data:
            raise LedgerError(f"'{_safe_name(name)}' is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise LedgerError(f"'{_safe_name(name)}' is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    written: list[Path] = []
    try:
        req = _lock_request(session, request_id)
        state = req.state
        if req.engagement.status == "Closed":
            raise InvalidTransition("The engagement is closed and read-only: no further uploads are accepted.")
        if state == "Drafted":
            raise InvalidTransition("This request has not been sent to the client yet - mark it Sent first.")

        known: dict[str, SubmissionFile] = {sf.content_hash: sf for sub in req.submissions for sf in sub.files}
        seen = set(known)
        plan = []
        for name, data in files:
            digest = sha256_hex(data)
            plan.append((name, data, digest, digest in seen))
            seen.add(digest)

        if any(not dup for *_, dup in plan):
            if state not in SUBMITTABLE_STATES:
                raise InvalidTransition(
                    f"New content cannot be submitted while the request is '{state}'. "
                    "(Identical re-sends of a file already held are still recorded.)"
                )

        submission: Optional[Submission] = None
        new_files: list[SubmissionFile] = []
        arrivals: list[SubmissionFile] = []
        page = 0
        for name, data, digest, is_dup in plan:
            if not is_dup:
                if submission is None:
                    submission = Submission(
                        request_id=req.id,
                        attempt_number=req.attempt_count,
                        received_at=at,
                        channel=channel,
                        submitted_by=submitted_by,
                        outcome="pending",
                    )
                    req.submissions.append(submission)
                    session.flush()
                page += 1
                safe = _safe_name(name)
                rel = f"{req.id}/{digest[:16]}_{safe}"
                path = base / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                written.append(path)
                sf = SubmissionFile(
                    request_id=req.id,
                    filename=safe,
                    content_hash=digest,
                    file_bytes_size=len(data),
                    storage_path=rel,
                    page_order=page,
                    retention_basis="none",
                )
                submission.files.append(sf)
                session.flush()
                known[digest] = sf
                new_files.append(sf)
            else:
                sf = known[digest]
                sf.arrivals.append(
                    Arrival(arrived_at=at, channel=channel, submitted_by=submitted_by)
                )
                session.flush()
                arrivals.append(sf)

        events = []
        if submission is not None:
            if E.SUBMITTED not in ALLOWED_NEXT.get(state, set()):
                raise InvalidTransition(f"Cannot record a submission while the request is '{state}'.")
            events.append(
                _insert_event(
                    session, req, E.SUBMITTED, actor, channel,
                    {
                        "submission_id": submission.id,
                        "attempt": submission.attempt_number,
                        **({"submitted_by": submitted_by} if submitted_by else {}),
                        "files": [f"{f.filename} (sha256 {f.content_hash[:12]}...)" for f in new_files],
                    },
                    at,
                )
            )
        for sf in arrivals:
            events.append(
                _insert_event(
                    session, req, E.ARRIVAL, actor, channel,
                    {
                        "message": "Identical content arrived again",
                        "filename": sf.filename,
                        "sha256": sf.content_hash[:12] + "...",
                        "submission_id": sf.submission_id,
                        "arrival_number": 1 + len(sf.arrivals),
                        **({"submitted_by": submitted_by} if submitted_by else {}),
                    },
                    at,
                )
            )
        session.commit()
        return {
            "submission": submission,
            "new_files": new_files,
            "arrivals": [{"file": sf, "count": 1 + len(sf.arrivals)} for sf in arrivals],
            "events": events,
        }
    except Exception:
        session.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise


def review_submission(
    session: Session,
    submission_id: int,
    action: str,
    actor: str,
    *,
    reason_code: Optional[str] = None,
    note: Optional[str] = None,
    at: Optional[datetime] = None,
    pages: Optional[str] = None,
) -> list[RequestEvent]:
    """Accept or reject a pending submission.

    accept -> submission 'accepted'; ACCEPTED event (actor = reviewer, at = review time).
    reject -> needs one of the eight reason codes (+ optional note); submission 'rejected'; REJECTED
              event with reason_code and note, then IMMEDIATELY RE_REQUESTED (attempt + 1) in the
              same transaction. The request stays live in the 'Re-requested' state.
    """
    actor = _require_actor(actor)
    action = (action or "").strip().lower()
    if action not in ("accept", "reject"):
        raise LedgerError("Review action must be 'accept' or 'reject'.")
    note = _clean(note)
    if note and len(note) > 2000:
        raise LedgerError("The review note is too long (2000 characters maximum).")
    at = _aware(at)

    try:
        found = session.get(Submission, submission_id)
        if found is None:
            raise NotFoundError(f"Submission {submission_id} does not exist.")
        req = _lock_request(session, found.request_id)
        sub = next(x for x in req.submissions if x.id == submission_id)

        if req.engagement.status == "Closed":
            raise InvalidTransition("The engagement is closed: its requests are frozen.")
        if sub.outcome != "pending":
            raise LedgerError(f"This submission was already reviewed ({sub.outcome}).")
        event_type = E.ACCEPTED if action == "accept" else E.REJECTED
        if event_type not in ALLOWED_NEXT.get(req.state or "", set()):
            raise InvalidTransition(f"Cannot {action} a submission while the request is '{req.state}'.")

        base_payload = {"submission_id": sub.id, "attempt": sub.attempt_number}
        if note:
            base_payload["note"] = note

        if action == "accept":
            events = [_insert_event(session, req, E.ACCEPTED, actor, None, base_payload, at)]
            sub.outcome = "accepted"
        else:
            code = (reason_code or "").strip().upper()
            code = REASON_CODE_ALIASES.get(code, code)
            if code not in REASON_CODES:
                raise LedgerError("Rejecting needs one of the eight reason codes.")
            label, _base = REASON_CODES[code]
            client_message = reason_message(code, req, pages)
            if _clean(pages):
                base_payload["pages"] = _clean(pages)
            events = _reject_and_rerequest(
                session, req, actor, None,
                {"reason_code": code, "reason_label": label, "client_message": client_message, **base_payload},
                at,
            )
            sub.outcome = "rejected"
            sub.reason_code = code
        sub.reviewed_by = actor
        sub.reviewed_at = at
        sub.review_notes = note
        session.commit()
        return events
    except Exception:
        session.rollback()
        raise


# --------------------------------------------------------------------------------------------
# Close-out
# --------------------------------------------------------------------------------------------
def close_engagement(
    session: Session,
    engagement_id: int,
    actor: str,
    report_date: Optional[date] = None,
    at: Optional[datetime] = None,
) -> list[Request]:
    """Close an engagement. Every request that is still open is FROZEN as 'Unfulfilled at report
    date' - it is neither deleted nor quietly waived. Returns the frozen requests."""
    actor = _require_actor(actor)
    at = _aware(at)
    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status == "Closed":
            raise LedgerError("This engagement is already closed.")
        report_date = report_date or at.date()

        requests = session.execute(
            select(Request)
            .where(Request.engagement_id == engagement_id)
            .order_by(Request.id)
            .with_for_update()
            .options(selectinload(Request.events))
            .execution_options(populate_existing=True)
        ).scalars().all()

        frozen = []
        for req in requests:
            if req.state in TERMINAL_STATES:
                continue
            _insert_event(
                session,
                req,
                E.UNFULFILLED,
                actor,
                "Manual",
                {
                    "frozen_from_state": req.state,
                    "significance": req.significance,
                    "report_date": report_date.isoformat(),
                    "attempts": req.attempt_count,
                    "chases": req.chase_count,
                },
                max(at, req.events[-1].at),
            )
            frozen.append(req)

        eng.status = "Closed"
        eng.closed_at = at
        eng.report_date = report_date
        session.commit()
        return frozen
    except Exception:
        session.rollback()
        raise


# --------------------------------------------------------------------------------------------
# Queries used by the UI
# --------------------------------------------------------------------------------------------
def list_engagements(session: Session) -> list[Engagement]:
    return list(
        session.execute(
            select(Engagement).order_by((Engagement.status != "Active"), Engagement.created_at.desc())
        ).scalars()
    )


def get_engagement(session: Session, engagement_id: int) -> Engagement:
    eng = session.get(Engagement, engagement_id)
    if eng is None:
        raise NotFoundError(f"Engagement {engagement_id} does not exist.")
    return eng


def engagement_requests(session: Session, engagement_id: int) -> list[Request]:
    return list(
        session.execute(
            select(Request)
            .where(Request.engagement_id == engagement_id)
            .order_by(Request.id)
            .options(selectinload(Request.events))
        ).scalars()
    )


def get_request(session: Session, request_id: int) -> Request:
    req = session.execute(
        select(Request)
        .where(Request.id == request_id)
        .options(
            selectinload(Request.events),
            selectinload(Request.engagement),
            selectinload(Request.submissions)
            .selectinload(Submission.files)
            .selectinload(SubmissionFile.arrivals),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if req is None:
        raise NotFoundError(f"Request {request_id} does not exist.")
    return req


def get_submission_file(session: Session, file_id: int) -> SubmissionFile:
    sf = session.get(SubmissionFile, file_id)
    if sf is None:
        raise NotFoundError(f"File {file_id} does not exist.")
    return sf


def engagement_stats(requests: list[Request]) -> dict:
    stats = {
        "total": len(requests),
        "accepted": 0,
        "open_material": 0,
        "open_routine": 0,
        "waived_na": 0,
        "unfulfilled": 0,
    }
    for r in requests:
        state = r.state
        if state == "Accepted":
            stats["accepted"] += 1
        elif state in ("Waived", "Not applicable"):
            stats["waived_na"] += 1
        elif state == "Unfulfilled at report date":
            stats["unfulfilled"] += 1
        elif r.significance == "Material":
            stats["open_material"] += 1
        else:
            stats["open_routine"] += 1
    return stats


# ============================================================================================
# Stage 2 - requirement lists, templates, entity profile and conditioning
# ============================================================================================
def get_profile(eng: Engagement) -> dict:
    """The engagement's entity profile with every known attribute present."""
    return normalise_profile(eng.entity_profile)


def update_profile(session: Session, engagement_id: int, raw: dict) -> dict:
    """Replace the entity profile (flags + turnover). Conditions are evaluated against the profile
    at generation time, and every suppression stores the snapshot it was evaluated against."""
    try:
        profile = normalise_profile(raw)
    except ConditionError as exc:
        raise LedgerError(str(exc)) from exc
    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status != "Active":
            raise LedgerError("This engagement is closed; its entity profile is frozen.")
        eng.entity_profile = profile  # assign a new dict so the JSONB change is detected
        session.commit()
        return profile
    except Exception:
        session.rollback()
        raise


def get_template(session: Session, engagement_type: str, version: Optional[int] = None) -> RequestTemplate:
    """A template by type; the latest version unless one is named."""
    stmt = (
        select(RequestTemplate)
        .where(RequestTemplate.engagement_type == engagement_type)
        .options(selectinload(RequestTemplate.items))
        .order_by(RequestTemplate.version.desc())
        .limit(1)
    )
    if version is not None:
        stmt = stmt.where(RequestTemplate.version == version)
    tmpl = session.execute(stmt).scalar_one_or_none()
    if tmpl is None:
        raise NotFoundError(f"No requirement-list template for '{engagement_type}'.")
    return tmpl


def list_templates(session: Session) -> list[RequestTemplate]:
    """Latest version of each template, in the standard order."""
    latest: dict[str, RequestTemplate] = {}
    for t in session.execute(
        select(RequestTemplate).options(selectinload(RequestTemplate.items)).order_by(RequestTemplate.version)
    ).scalars():
        latest[t.engagement_type] = t
    return [latest[t] for t in ENGAGEMENT_TYPES if t in latest]


def seed_templates(session: Session, catalogue: dict) -> int:
    """Insert version 1 of each template that does not exist yet. Idempotent. Returns how many
    templates were created. Conditions are validated before anything is written."""
    from conditions import validate_condition

    created = 0
    for engagement_type, spec in catalogue.items():
        exists = session.execute(
            select(RequestTemplate.id).where(RequestTemplate.engagement_type == engagement_type).limit(1)
        ).first()
        if exists:
            continue
        tmpl = RequestTemplate(engagement_type=engagement_type, version=1, title=spec["title"])
        for pos, item in enumerate(spec["items"], start=1):
            try:
                validate_condition(item.get("condition"))
            except ConditionError as exc:
                raise LedgerError(f"{engagement_type} / {item['document']}: {exc}") from exc
            tmpl.items.append(
                TemplateItem(
                    position=pos,
                    document=item["document"],
                    description=item.get("description"),
                    label_text=item.get("label"),
                    condition_expr=item.get("condition"),
                    significance_default=item.get("significance", "Routine"),
                    priority_default=item.get("priority", 3),
                    period_mode=item.get("period", "engagement"),
                )
            )
        session.add(tmpl)
        created += 1
    session.commit()
    return created


# ---- helpers for turning a template item into request fields -------------------------------
def _fy_label(eng: Engagement) -> str:
    if eng.period_from and eng.period_to:
        return f"FY {eng.period_from.year}-{str(eng.period_to.year)[-2:]}"
    return "the financial year"


def _fill(text_value: Optional[str], eng: Engagement) -> Optional[str]:
    if not text_value:
        return text_value
    start = eng.period_from.strftime("%d %B %Y") if eng.period_from else "start of period"
    end = eng.period_to.strftime("%d %B %Y") if eng.period_to else "end of period"
    return (
        text_value.replace("{period_start}", start)
        .replace("{period_end}", end)
        .replace("{fy_label}", _fy_label(eng))
    )


def _item_period(item: TemplateItem, eng: Engagement) -> tuple[Optional[date], Optional[date]]:
    if item.period_mode == "as_at":
        return None, eng.period_to
    if item.period_mode == "none":
        return None, None
    return eng.period_from, eng.period_to


def _evaluate_item(item: TemplateItem, profile: dict) -> tuple[bool, Optional[str]]:
    """(applies, basis). basis is the recorded reason, e.g. 'Suppressed: has_inventory is false'."""
    expr = (item.condition_expr or "").strip()
    if not expr:
        return True, None
    try:
        applies = evaluate(expr, profile)
        reason = explain(expr, profile, applies)
    except ConditionError as exc:
        raise LedgerError(f"Template rule '{expr}' could not be evaluated: {exc}") from exc
    return applies, ("Included: " if applies else "Suppressed: ") + reason


def _generated_docs(session: Session, requests: list[Request]) -> dict[str, Request]:
    """template-item wording -> the request raised from it, so an item republished in a newer
    template version is still recognised as already generated."""
    ids = {r.template_item_id for r in requests if r.template_item_id}
    if not ids:
        return {}
    docs = {
        i.id: i.document
        for i in session.execute(select(TemplateItem).where(TemplateItem.id.in_(ids))).scalars()
    }
    out: dict[str, Request] = {}
    for r in requests:
        doc = docs.get(r.template_item_id)
        if doc:
            out.setdefault(doc, r)
    return out


def _match_existing(
    item: TemplateItem, document: str, pf: Optional[date], pt: Optional[date], requests: list[Request],
    generated: Optional[dict] = None,
) -> tuple[Optional[Request], Optional[str]]:
    """Is this item already covered in the engagement?
    'generated' - a request was already made from this very template item (any state);
    'live'      - an open request for the same document and period (duplicate prevention);
    'accepted'  - the same document and period is already accepted, so asking again is pointless."""
    if generated and item.document in generated:
        return generated[item.document], "generated"
    for r in requests:
        if r.template_item_id == item.id:
            return r, "generated"
    key = _normalise_document(document)
    accepted = None
    for r in requests:
        if _normalise_document(r.document) == key and r.period_from == pf and r.period_to == pt:
            if r.is_open:
                return r, "live"
            if r.state == "Accepted":
                accepted = r
    return (accepted, "accepted") if accepted else (None, None)


def build_requirement_list(
    session: Session,
    engagement_id: int,
    engagement_type: str,
    *,
    profile_override: Optional[dict] = None,
    version: Optional[int] = None,
    carry_forward: bool = False,
) -> dict:
    """Preview of the requirement list: what would be created, what already exists (and who holds
    it), and what the entity profile suppresses (and the exact rule). Writes nothing.
    `profile_override` evaluates a what-if profile without saving it."""
    eng = get_engagement(session, engagement_id)
    tmpl = get_template(session, engagement_type, version)
    profile = normalise_profile(profile_override) if profile_override is not None else get_profile(eng)
    requests = engagement_requests(session, engagement_id)
    prior = find_prior_engagement(session, eng)
    history = prior_fulfilment(session, prior) if (prior and carry_forward) else {}
    gen = _generated_docs(session, requests)

    rows = []
    for item in tmpl.items:
        document = _fill(item.document, eng)
        pf, pt = _item_period(item, eng)
        applies, basis = _evaluate_item(item, profile)
        row = {
            "carry": None,
            "carry_tag": None,
            "default_include": True,
            "item": item,
            "document": document,
            "description": _fill(item.label_text or item.description, eng),
            "period_from": pf,
            "period_to": pt,
            "rule": item.condition_expr,
            "basis": basis,
            "existing": None,
            "existing_kind": None,
        }
        if not applies:
            row["status"] = "suppressed"
        else:
            existing, kind = _match_existing(item, document, pf, pt, requests, gen)
            row["existing"], row["existing_kind"] = existing, kind
            row["status"] = "existing" if existing else "create"
            if carry_forward and prior and row["status"] == "create":
                row["carry"] = history.get(item.document)
                row["carry_tag"] = carry_tag(row["carry"])
                # Last year's waived / not-applicable items are not asked again unless ticked.
                row["default_include"] = row["carry_tag"] not in ("Waived last year", "Not applicable last year")
        rows.append(row)

    return {
        "engagement": eng,
        "template": tmpl,
        "profile": profile,
        "rows": rows,
        "to_create": [r for r in rows if r["status"] == "create"],
        "existing": [r for r in rows if r["status"] == "existing"],
        "suppressed": [r for r in rows if r["status"] == "suppressed"],
        "prior": prior,
        "carry_forward": bool(carry_forward and prior),
        "carry_counts": _carry_counts([r for r in rows if r["status"] == "create"]) if (carry_forward and prior) else {},
    }


def recorded_suppressions(session: Session, engagement_id: int) -> list[RequestSuppression]:
    return list(
        session.execute(
            select(RequestSuppression)
            .where(RequestSuppression.engagement_id == engagement_id)
            .order_by(RequestSuppression.at.desc(), RequestSuppression.id.desc())
        ).scalars()
    )


_EXISTING_REASON = {
    "live": "already requested and still open",
    "accepted": "already accepted",
    "generated": "already raised from this template item",
}


def _existing_summary(document: str, existing: Request, kind: Optional[str]) -> dict:
    return {
        "document": document,
        "ref": existing.ref,
        "id": existing.id,
        "state": existing.state,
        "owner_person": existing.owner_person,
        "kind": kind,
        "reason": _EXISTING_REASON.get(kind or "", "already exists"),
    }


def generate_requests(
    session: Session,
    *,
    engagement_id: int,
    engagement_type: str,
    actor: str,
    default_owner: Optional[str] = None,
    selections: Optional[dict] = None,
    version: Optional[int] = None,
    carry_forward: bool = False,
) -> dict:
    """Turn a template into requests for the engagement, all in ONE transaction.

    For every template item the condition is re-evaluated here (the browser's preview is never
    trusted):
      * condition false -> no request; a RequestSuppression row records the rule and the profile
        it was evaluated against (once per item and rule);
      * already covered / duplicate -> skipped and reported with the holding request and owner;
      * otherwise a request is created with its seq=1 DRAFTED event.

    `selections` maps template_item_id -> {include, owner_person, significance, priority}; items
    the user unticked are skipped. When it is None every applicable item is created.
    """
    actor = _require_actor(actor)
    default_owner = _clean(default_owner)
    result = {"created": [], "existing": [], "suppressed": [], "deselected": [], "new_suppressions": 0}

    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status != "Active":
            raise LedgerError("This engagement is closed; no new requests can be raised.")
        tmpl = get_template(session, engagement_type, version)
        profile = get_profile(eng)
        template_label = f"{tmpl.engagement_type} v{tmpl.version}"
        now = utcnow()
        prior = find_prior_engagement(session, eng) if carry_forward else None
        if carry_forward and prior is None:
            raise LedgerError("There is no prior-year engagement for this client to carry forward from.")
        history = prior_fulfilment(session, prior) if prior else {}

        for item in tmpl.items:
            document = _fill(item.document, eng)
            pf, pt = _item_period(item, eng)
            applies, basis = _evaluate_item(item, profile)

            if not applies:
                result["suppressed"].append({"document": document, "basis": basis})
                already = session.execute(
                    select(RequestSuppression.id)
                    .where(
                        RequestSuppression.engagement_id == engagement_id,
                        RequestSuppression.template_item_id == item.id,
                        RequestSuppression.condition_basis == basis,
                    )
                    .limit(1)
                ).first()
                if not already:
                    session.add(
                        RequestSuppression(
                            engagement_id=engagement_id,
                            template_item_id=item.id,
                            template_label=template_label,
                            document=document,
                            condition_expr=item.condition_expr,
                            condition_basis=basis,
                            profile_snapshot=profile,
                            actor=actor,
                            at=now,
                        )
                    )
                    result["new_suppressions"] += 1
                continue

            sel = (selections or {}).get(item.id)
            if selections is not None and not (sel and sel.get("include")):
                result["deselected"].append(document)
                continue

            requests = engagement_requests(session, engagement_id)
            existing, kind = _match_existing(item, document, pf, pt, requests, _generated_docs(session, requests))
            if existing:
                result["existing"].append(_existing_summary(document, existing, kind))
                continue

            sel = sel or {}
            carry = history.get(item.document) if prior else None
            owner = _clean(sel.get("owner_person")) or (carry or {}).get("owner_person") or default_owner
            if not owner:
                raise LedgerError(f"Name the client-side owner for '{document}' (or set a default owner).")
            try:
                req = create_request(
                    session,
                    engagement_id=engagement_id,
                    document=document,
                    owner_person=owner,
                    actor=actor,
                    significance=sel.get("significance") or item.significance_default,
                    priority=int(sel.get("priority") or item.priority_default),
                    description=_fill(item.label_text or item.description, eng),
                    period_from=pf,
                    period_to=pt,
                    template_item=f"{template_label} · #{item.position}",
                    template_item_id=item.id,
                    condition_basis=basis or "No condition - asked on every engagement of this type",
                    carried_from_request_id=carry["request_id"] if carry else None,
                    carry_note=carry_note(carry, prior) if prior else None,
                    channel="Template",
                    commit=False,
                )
            except DuplicateRequestError as dup:  # lost a race with another team member
                result["existing"].append(_existing_summary(document, dup.existing, "live"))
                continue
            result["created"].append(
                {"id": req.id, "ref": req.ref, "document": document, "significance": req.significance,
                 "carry_tag": carry_tag(carry) if prior else None}
            )

        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


# ============================================================================================
# Stage 4 - the Outstanding Board, inline chasing, the exportable thread, carry-forward
# ============================================================================================
AGE_WARN_DAYS = 14  # more than this many days outstanding: amber
AGE_CRIT_DAYS = 21  # more than this: red, escalate
CLIENT_SIDE_STATES = ("Sent", "Acknowledged", "Re-requested")  # the ball is in the client's court
CHASEABLE_STATES = set(CLIENT_SIDE_STATES)
CHASE_CHANNELS = ("WhatsApp", "Email", "Phone")
BOARD_CUTS = (
    ("age", "By age"),
    ("owner", "By owner"),
    ("significance", "By significance"),
    ("chase", "By chase count"),
)

# Wording the auditor can start from. Tokens: {first_name} {owner} {document} {ref} {age}
CHASE_TEMPLATES = [
    {"key": "gentle", "label": "Gentle reminder",
     "text": "Hi {first_name}, a gentle reminder about {document} ({ref}). Could you send it across when you get a chance?"},
    {"key": "date", "label": "Ask for a date",
     "text": "Hi {first_name}, {document} ({ref}) has been outstanding for {age} days. Could you give us a date it will reach us?"},
    {"key": "escalate", "label": "Escalation",
     "text": "{first_name}, {document} ({ref}) is now {age} days outstanding and is holding up the audit. Please prioritise it, or tell us what is blocking you."},
    {"key": "call", "label": "Phone follow-up log",
     "text": "Phoned {owner} about {document} ({ref}). Outcome: "},
    {"key": "custom", "label": "Write my own note", "text": ""},
]
_CHASE_BY_KEY = {t["key"]: t for t in CHASE_TEMPLATES}


def age_band(days: int, warn: int = AGE_WARN_DAYS, crit: int = AGE_CRIT_DAYS) -> str:
    """'crit' (escalate to the partner) beyond `crit` days, 'warn' beyond `warn`, else 'ok'.
    The thresholds are per engagement (defaults 14 / 21)."""
    if days > crit:
        return "crit"
    if days > warn:
        return "warn"
    return "ok"


def owner_parts(owner: str) -> tuple[str, str]:
    """'Meera Shah (Finance Controller)' -> ('Meera Shah', 'Finance Controller')."""
    owner = (owner or "").strip()
    if "(" in owner and owner.endswith(")"):
        name, _, role = owner.partition("(")
        return name.strip(), role[:-1].strip()
    return owner, ""


def render_chase_text(template_key: Optional[str], req: Request, now: Optional[datetime] = None) -> str:
    tmpl = _CHASE_BY_KEY.get(template_key or "")
    if not tmpl or not tmpl["text"]:
        return ""
    name, _ = owner_parts(req.owner_person)
    return (
        tmpl["text"]
        .replace("{first_name}", (name.split() or [req.owner_person])[0])
        .replace("{owner}", name)
        .replace("{document}", req.document)
        .replace("{ref}", req.ref)
        .replace("{age}", str(req.age_days(now)))
    )


def record_chase(
    session: Session,
    request_id: int,
    actor: str,
    channel: str,
    *,
    template: Optional[str] = None,
    note: Optional[str] = None,
    at: Optional[datetime] = None,
    notify: bool = True,
) -> dict:
    """Record a chase: a CHASED event (append-only) plus the Section 9 `chase` row, in one
    transaction. By Email the reminder is also sent to the engagement's client address with the
    upload link (logged in the Outbox) and the chase is marked delivered; the client opening the link
    later marks it opened. WhatsApp / Phone chases are made by hand, so they are recorded but never
    "delivered" by the system. `notify=False` records an Email chase as delivered without sending
    anything (used when loading historical demo data)."""
    channel = _clean(channel)
    if channel not in CHASE_CHANNELS:
        raise LedgerError(f"A chase goes by {', '.join(CHASE_CHANNELS[:-1])} or {CHASE_CHANNELS[-1]}.")
    template = _clean(template)
    if template and template not in _CHASE_BY_KEY:
        raise LedgerError(f"Unknown chase template '{template}'.")
    note = _clean(note)
    req = get_request(session, request_id)
    if not note and template and template != "custom":
        note = _clean(render_chase_text(template, req))
    if not note:
        raise LedgerError("Choose a chase template or write a short note.")
    if len(note) > 1000:
        raise LedgerError("The chase note is too long (1000 characters maximum).")
    label = _CHASE_BY_KEY[template]["label"] if template else None

    deliver = None
    if channel == "Email":
        deliver = _email_chase_hook(session, note, actor) if notify else _delivered_hook
    eng, document = req.engagement, req.document
    try:
        events = append_event(
            session, request_id, E.CHASED, actor,
            channel=channel, payload={"note": note, "template": label}, at=at, deliver=deliver,
        )
    except emailer.EmailDeliveryError as exc:
        session.rollback()
        emailer.record_failure(
            session, kind="chase", to_address=eng.client_email or "", subject=f"Reminder: {document}",
            body=note, created_by=actor, error=str(exc), engagement_id=eng.id,
        )
        raise LedgerError(f"{exc} The chase was not recorded.") from exc
    req = get_request(session, request_id)
    chase = session.execute(select(Chase).where(Chase.event_id == events[0].id)).scalar_one()
    return {
        "event": events[0],
        "chase": chase,
        "chase_count": req.chase_count,
        "request": req,
        "emailed": chase.email_id is not None,
        "no_client_email": channel == "Email" and notify and not eng.client_email,
    }


def _delivered_hook(req: Request, ev: RequestEvent, chase: Chase) -> None:
    chase.delivered = True
    chase.delivered_at = ev.at


def _email_chase_hook(session: Session, note: str, actor: str):
    """Email the reminder with the client link, inside the chase's transaction."""

    def hook(req: Request, ev: RequestEvent, chase: Chase) -> None:
        eng = req.engagement
        if not eng.client_email:
            return  # nobody to email: the chase is recorded, but not delivered
        token = _link_token_for(session, eng, req, actor)
        body = (
            f"{note}\n\n"
            f"Document: {req.document} ({req.ref})\n"
            f"Period: {_period_phrase(req.period_from, req.period_to)}\n"
            f"Asked of: {req.owner_person}\n\n"
            f"Upload it here - no login or password needed:\n{emailer.client_link_url(token.token)}\n"
        )
        mail = emailer.dispatch(
            session, kind="chase", to_address=eng.client_email, subject=f"Reminder: {req.document}",
            body=body, created_by=actor, engagement_id=eng.id, request_id=req.id, token_id=token.id,
        )
        chase.delivered = True
        chase.delivered_at = mail.created_at
        chase.email_id = mail.id

    return hook


# ---- the board -------------------------------------------------------------------------------
def _board_row(req: Request, now: datetime, warn: int = AGE_WARN_DAYS, crit: int = AGE_CRIT_DAYS) -> dict:
    age = req.age_days(now)
    chases = [ev for ev in req.events if ev.event_type == E.CHASED]
    last_response = max(
        (ev.seq for ev in req.events if ev.event_type in (E.SUBMITTED, E.ARRIVAL)), default=0
    )
    last_chase = chases[-1] if chases else None
    name, role = owner_parts(req.owner_person)
    return {
        "req": req,
        "id": req.id,
        "ref": req.ref,
        "document": req.document,
        "owner": req.owner_person,
        "owner_name": name,
        "owner_role": role,
        "material": req.significance == "Material",
        "state": req.state,
        "age": age,
        "band": age_band(age, warn, crit),
        "unavailable": req.reported_unavailable,
        "escalated": any(
            ev.event_type == E.NOTE and (ev.payload or {}).get("escalation") for ev in req.events
        ),
        "chase_count": len(chases),
        "silent_chases": sum(1 for ev in chases if ev.seq > last_response),
        "last_chase": last_chase,
        "last_chase_days": (now - _aware(last_chase.at)).days if last_chase else None,
        "attempts": req.attempt_count,
        "can_chase": req.state in CHASEABLE_STATES and req.engagement.status == "Active",
    }


def outstanding_board(session: Session, engagement_id: int, cut: str = "age", now: Optional[datetime] = None) -> dict:
    """Everything the client still owes (Sent / Acknowledged / Re-requested), cut one of four ways.

    age          oldest first
    owner        grouped by the named person on the client side, busiest first
    significance oldest first within the two bands
    chase        most-chased first

    In EVERY cut the Material-and-open requests are PINNED above everything else (`pinned`), in the
    order of the cut, and the board's filter never hides them. `body` is what is left (routine).
    Ageing thresholds are the engagement's own (default 14 / 21 days).
    """
    cut = cut if cut in dict(BOARD_CUTS) else "age"
    now = now or utcnow()
    eng = get_engagement(session, engagement_id)
    warn, crit = eng.age_warn_days, eng.age_escalate_days
    requests = engagement_requests(session, engagement_id)

    rows = [_board_row(r, now, warn, crit) for r in requests if r.state in CLIENT_SIDE_STATES]
    by_age = sorted(rows, key=lambda b: (-b["age"], -b["chase_count"], b["id"]))

    # owner groups (over ALL rows, so a person's totals are honest even though material is pinned)
    bucket: dict[str, dict] = {}
    for b in by_age:
        g = bucket.setdefault(
            b["owner_name"].lower(),
            {"name": b["owner_name"], "role": b["owner_role"], "rows": [], "total": 0, "material": 0,
             "chases": 0, "oldest": 0},
        )
        g["total"] += 1
        g["material"] += 1 if b["material"] else 0
        g["chases"] += b["chase_count"]
        g["oldest"] = max(g["oldest"], b["age"])
    group_list = sorted(bucket.values(), key=lambda g: (-g["total"], -g["oldest"], g["name"]))
    group_rank = {g["name"].lower(): i for i, g in enumerate(group_list)}
    for g in group_list:
        g["band"] = age_band(g["oldest"], warn, crit)

    if cut == "chase":
        ordered = sorted(rows, key=lambda b: (-b["chase_count"], -b["age"], b["id"]))
    elif cut == "owner":
        ordered = sorted(rows, key=lambda b: (group_rank[b["owner_name"].lower()], -b["age"], b["id"]))
    else:
        ordered = by_age

    pinned = [b for b in ordered if b["material"]]
    body = [b for b in ordered if not b["material"]]
    groups: list[dict] = []
    if cut == "owner":
        for g in group_list:
            g["rows"] = [b for b in body if b["owner_name"].lower() == g["name"].lower()]
            g["pinned_count"] = g["material"]
            groups.append(g)

    summary = {
        "outstanding": len(rows),
        "material": len(pinned),
        "warn": sum(1 for b in rows if b["band"] == "warn"),
        "crit": sum(1 for b in rows if b["band"] == "crit"),
        "to_escalate": sum(1 for b in rows if b["band"] == "crit" and not b["escalated"]),
        "chased_3plus": sum(1 for b in rows if b["chase_count"] >= 3),
        "owners": len(bucket),
        "with_audit_team": sum(1 for r in requests if r.state in ("Submitted", "Under review")),
        "not_sent": sum(1 for r in requests if r.state == "Drafted"),
    }
    return {
        "engagement": eng,
        "cut": cut,
        "cuts": BOARD_CUTS,
        "rows": pinned + body,
        "pinned": pinned,
        "body": body,
        "groups": groups,
        "material_rows": [b for b in by_age if b["material"]],
        "routine_rows": [b for b in by_age if not b["material"]],
        "summary": summary,
        "warn": warn,
        "crit": crit,
        "partner_email": eng.partner_email,
        "now": now,
    }


# ---- the exportable thread ---------------------------------------------------------------------
def get_request_by_ref(session: Session, engagement_id: int, ref: str) -> Request:
    found = session.execute(
        select(Request.id).where(Request.engagement_id == engagement_id, Request.ref == ref)
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(f"There is no request {ref} in engagement {engagement_id}.")
    return get_request(session, found)


def thread_fingerprint(req: Request) -> str:
    """SHA-256 over the whole ordered event history. Two printouts with the same fingerprint show
    the same thread; any later edit of history would change it."""
    rows = [
        [ev.seq, ev.event_type, ev.actor, _aware(ev.at).isoformat(), ev.channel, ev.payload]
        for ev in req.events
    ]
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def chases_by_event(session: Session, request_id: int) -> dict:
    """event id -> its `chase` row (delivered / opened flags), for the thread views."""
    return {
        c.event_id: c
        for c in session.execute(select(Chase).where(Chase.request_id == request_id)).scalars()
    }


def carried_from_request(session: Session, req: Request) -> Optional[Request]:
    return session.get(Request, req.carried_from_request_id) if req.carried_from_request_id else None


def export_context(session: Session, engagement_id: int, ref: str) -> dict:
    req = get_request_by_ref(session, engagement_id, ref)
    seqs = [ev.seq for ev in req.events]
    drafted = next((ev for ev in req.events if ev.event_type == E.DRAFTED), None)
    return {
        "r": req,
        "eng": req.engagement,
        "fingerprint": thread_fingerprint(req),
        "seq_ok": seqs == list(range(1, len(seqs) + 1)),
        "carried_from": carried_from_request(session, req),
        "carried_note": (drafted.payload or {}).get("carried_forward") if drafted else None,
        "arrival_total": sum(len(f.arrivals) for sub in req.submissions for f in sub.files),
        "chases": chases_by_event(session, req.id),
        "now": utcnow(),
    }


# ---- carry-forward -----------------------------------------------------------------------------
def find_prior_engagement(session: Session, eng: Engagement) -> Optional[Engagement]:
    """The same client's most recent earlier engagement of the same type, if there is one."""
    candidates = session.execute(
        select(Engagement).where(
            Engagement.id != eng.id,
            Engagement.client_name == eng.client_name,
            Engagement.engagement_type == eng.engagement_type,
        )
    ).scalars().all()

    def earlier(c: Engagement) -> bool:
        if c.period_to and eng.period_to:
            return c.period_to < eng.period_to
        return _aware(c.created_at) < _aware(eng.created_at)

    prior = [c for c in candidates if earlier(c)]
    return max(prior, key=lambda c: (c.period_to or date.min, c.id)) if prior else None


def prior_fulfilment(session: Session, prior: Engagement) -> dict[str, dict]:
    """How each template item went last year, keyed by the template item's own document wording
    (so a '{fy_label}' in a document name does not stop this year matching last year)."""
    requests = engagement_requests(session, prior.id)
    ids = {r.template_item_id for r in requests if r.template_item_id}
    docs = (
        {i.id: i.document for i in session.execute(select(TemplateItem).where(TemplateItem.id.in_(ids))).scalars()}
        if ids else {}
    )
    out: dict[str, dict] = {}
    for r in requests:
        key = docs.get(r.template_item_id)
        if not key:
            continue
        out[key] = {
            "request_id": r.id,
            "ref": r.ref,
            "state": r.state,
            "owner_person": r.owner_person,
            "significance": r.significance,
            "attempts": r.attempt_count,
            "chases": r.chase_count,
            "days": r.age_days(),
            "rejections": [
                (ev.payload or {}).get("reason_label", "")
                for ev in r.events if ev.event_type == E.REJECTED
            ],
            "engagement": prior.name,
        }
    return out


def carry_tag(summary: Optional[dict]) -> str:
    if summary is None:
        return "New this year"
    return {
        "Accepted": "Same as last year",
        "Waived": "Waived last year",
        "Not applicable": "Not applicable last year",
        "Unfulfilled at report date": "Unfulfilled last year",
    }.get(summary["state"], "Open at last year's close")


def carry_note(summary: Optional[dict], prior: Optional[Engagement]) -> str:
    if summary is None:
        return f"New this year - no matching request in {prior.name if prior else 'the prior engagement'}"
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"  # noqa: E731
    return (
        f"{carry_tag(summary)} - {summary['ref']} in {summary['engagement']} was {summary['state'].lower()} "
        f"after {plural(summary['attempts'], 'attempt')}, {plural(summary['chases'], 'chase')}, "
        f"{plural(summary['days'], 'day')}"
    )


def _carry_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["carry_tag"]] = counts.get(r["carry_tag"], 0) + 1
    return counts


# ============================================================================================
# Stage 5 - close-out evidence: EQR gate, SA 705 extract, SA 580 schedule
# ============================================================================================
def eqr_gate(requests: list[Request], *, closed: bool) -> dict:
    """The EQR readiness gate. A Material request that is open (engagement still running) or
    Unfulfilled at report date (engagement closed) blocks it. Accepted, waived and not-applicable
    material requests do not."""
    blocking = [
        r for r in requests
        if r.significance == "Material" and (r.is_open or r.state == "Unfulfilled at report date")
    ]
    n = len(blocking)
    if n == 0:
        message = "PASSED: No material requests unfulfilled"
    elif closed:
        message = f"BLOCKED: {n} material request{'s' if n != 1 else ''} remain{'s' if n == 1 else ''} unfulfilled"
    else:
        message = (
            f"BLOCKED: {n} material request{'s' if n != 1 else ''} still open - "
            f"{'it' if n == 1 else 'they'} would be frozen as unfulfilled at close-out"
        )
    return {"passed": n == 0, "blocked": n > 0, "count": n, "message": message, "requests": blocking}


def _fmt_date(d) -> str:
    return d.strftime("%d %B %Y") if d else "-"


def _period_phrase(pf: Optional[date], pt: Optional[date]) -> str:
    if pf and pt:
        return f"{_fmt_date(pf)} to {_fmt_date(pt)}"
    if pt:
        return f"as at {_fmt_date(pt)}"
    if pf:
        return f"from {_fmt_date(pf)}"
    return "no period specified"


def _unfulfilled_row(req: Request, now: datetime) -> dict:
    frozen = next((ev for ev in reversed(req.events) if ev.event_type == E.UNFULFILLED), None)
    return {
        "req": req,
        "id": req.id,
        "ref": req.ref,
        "document": req.document,
        "period": _period_phrase(req.period_from, req.period_to),
        "owner": req.owner_person,
        "significance": req.significance,
        "material": req.significance == "Material",
        "chases": req.chase_count,
        "attempts": req.attempt_count,
        "requested_on": req.sent_at or req.created_at,
        "released": req.sent_at is not None,  # a request never sent was never asked of the client
        "frozen_from": (frozen.payload or {}).get("frozen_from_state") if frozen else req.state,
        "age": req.age_days(now),
    }


def closeout_report(session: Session, engagement_id: int) -> dict:
    """Everything Screen 5 shows. For an engagement that is still running it is a PREVIEW: the open
    requests are listed as the ones that would be frozen. Nothing is written."""
    eng = get_engagement(session, engagement_id)
    requests = engagement_requests(session, engagement_id)
    closed = eng.status == "Closed"
    now = utcnow()

    frozen_reqs = [
        r for r in requests
        if (r.state == "Unfulfilled at report date") or (not closed and r.is_open)
    ]
    rows = sorted(
        (_unfulfilled_row(r, now) for r in frozen_reqs),
        key=lambda x: (not x["material"], -x["age"], x["id"]),
    )
    counts = {
        "raised": len(requests),
        "accepted": sum(1 for r in requests if r.state == "Accepted"),
        "waived_na": sum(1 for r in requests if r.state in ("Waived", "Not applicable")),
        "unfulfilled": len(rows),
    }
    report_date = eng.report_date if closed else (eng.report_date or now.date())
    gate = eqr_gate(requests, closed=closed)
    material_rows = [x for x in rows if x["material"]]
    report = {
        "engagement": eng,
        "closed": closed,
        "preview": not closed,
        "report_date": report_date,
        "counts": counts,
        "rows": rows,
        "material_rows": material_rows,
        "routine_rows": [x for x in rows if not x["material"]],
        "gate": gate,
        "open_now": sum(1 for r in requests if r.is_open) if not closed else 0,
        "now": now,
    }
    report["sa705"] = sa705_extract(report)
    report["sa580"] = sa580_schedule(report)
    return report


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def sa705_extract(report: dict) -> dict:
    """Drafted scope-limitation wording for the auditor's report (SA 705 Revised): the material
    documents that were requested and not provided. A DRAFT for the engagement partner to adapt."""
    eng = report["engagement"]
    items = [x for x in report["material_rows"] if x["released"]]
    routine = len([x for x in report["routine_rows"] if x["released"]])
    unreleased = len([x for x in report["rows"] if not x["released"]])
    period = _period_phrase(eng.period_from, eng.period_to)
    heading = "Basis for Qualified Opinion - Limitation on Scope (draft)"
    if not items:
        return {
            "has_limitation": False,
            "heading": heading,
            "intro": "",
            "lines": [],
            "conclusion": "",
            "drafting_note": (
                "No material document request is unfulfilled, so this engagement records no scope "
                "limitation arising from documents that were not provided."
            ),
            "text": (
                f"{heading}\n\nNo material document request is unfulfilled at "
                f"{_fmt_date(report['report_date'])}; no scope limitation arises from documents not provided.\n"
            ),
        }
    intro = (
        f"As part of our audit of the financial statements of {eng.client_name} for the period {period}, we "
        f"requested the documents listed below from management. They had not been provided to us as at "
        f"{_fmt_date(report['report_date'])}."
    )
    listed = []
    for i, x in enumerate(items, start=1):
        listed.append(
            f"({_roman(i)}) {x['document']} ({x['period']}) - requested on {_fmt_date(x['requested_on'])}; "
            f"{_plural(x['chases'], 'follow-up')} made; held by {x['owner']} [{x['ref']}]."
        )
    conclusion = (
        "We were unable to obtain sufficient appropriate audit evidence about the matters to which these "
        "documents relate, and we were unable to satisfy ourselves by performing alternative audit procedures. "
        "Consequently, we were unable to determine whether any adjustments might be necessary in respect of the "
        "related amounts and disclosures in the financial statements."
    )
    note = (
        "Drafting note: the engagement partner decides under SA 705 (Revised) whether the possible effects are "
        "material but not pervasive (qualified opinion) or material and pervasive (disclaimer of opinion), and "
        "aligns this paragraph with the final opinion wording."
        + (f" {_plural(routine, 'routine request')} also remain unfulfilled; they are listed in the SA 580 schedule, not here."
           if routine else "")
        + (f" {_plural(unreleased, 'request')} frozen before being sent to the client {'is' if unreleased == 1 else 'are'} not listed: nothing was asked of management."
           if unreleased else "")
    )
    text = "\n".join([heading, "", intro, "", *listed, "", conclusion, "", note, ""])
    return {
        "has_limitation": True,
        "heading": heading,
        "intro": intro,
        "lines": listed,
        "conclusion": conclusion,
        "drafting_note": note,
        "text": text,
    }


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += sym
            n -= value
    return out


def sa580_schedule(report: dict) -> dict:
    """The formal schedule of unfulfilled requests, to be attached to management's written
    representation letter (SA 580). Every unfulfilled request is listed, material first."""
    eng = report["engagement"]
    rows = [x for x in report["rows"] if x["released"]]  # only what was actually asked of management
    preamble = (
        f"Schedule of documents requested by the auditor and not provided - annexure to the management "
        f"representation letter for the audit of {eng.client_name} for the period "
        f"{_period_phrase(eng.period_from, eng.period_to)}."
    )
    statement = (
        f"Management acknowledges that the {_plural(len(rows), 'document request')} listed below were made by "
        f"the auditor during the engagement and had not been fulfilled as at {_fmt_date(report['report_date'])}."
        if rows else
        f"No document request made by the auditor remained unfulfilled as at {_fmt_date(report['report_date'])}."
    )
    return {"preamble": preamble, "statement": statement, "rows": rows,
            "unreleased": len(report["rows"]) - len(rows)}


def sa580_csv(report: dict) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["No.", "Reference", "Document", "Period", "Client owner", "Significance",
                "Requested on", "Chases", "Attempts", "State when frozen"])
    for i, x in enumerate(report["sa580"]["rows"], start=1):
        w.writerow([i, x["ref"], x["document"], x["period"], x["owner"], x["significance"],
                    x["requested_on"].date().isoformat(), x["chases"], x["attempts"], x["frozen_from"] or ""])
    return buf.getvalue()


# ============================================================================================
# Pass 1 completion - settings, tokenised client link, batch release, escalation, outbox,
# template manager
# ============================================================================================
CLIENT_ACTOR = "Client (tokenised link)"
TOKEN_DEFAULT_DAYS = 30
CLIENT_QUEUE_SIZE = 5  # the client sees this many open requests at a time
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TokenError(LedgerError):
    """The client link cannot be used. `reason` is invalid / revoked / expired / closed."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


# ---- engagement settings ---------------------------------------------------------------------
def _valid_email(value: Optional[str], label: str) -> Optional[str]:
    value = _clean(value)
    if value and not _EMAIL_RE.match(value):
        raise LedgerError(f"'{value}' is not a valid email address ({label}).")
    return value.lower() if value else None


def update_engagement_settings(
    session: Session,
    engagement_id: int,
    *,
    client_email: Optional[str] = None,
    partner_email: Optional[str] = None,
    age_warn_days: Optional[int] = None,
    age_escalate_days: Optional[int] = None,
) -> Engagement:
    """Who the client link and reminders are emailed to, who escalations go to, and the ageing
    thresholds (amber warning / partner escalation, in days outstanding)."""
    client_email = _valid_email(client_email, "client")
    partner_email = _valid_email(partner_email, "partner")
    try:
        warn = int(age_warn_days if age_warn_days not in (None, "") else AGE_WARN_DAYS)
        crit = int(age_escalate_days if age_escalate_days not in (None, "") else AGE_CRIT_DAYS)
    except (TypeError, ValueError) as exc:
        raise LedgerError("The ageing thresholds must be whole numbers of days.") from exc
    if not 1 <= warn < crit <= 365:
        raise LedgerError("The warning threshold must be at least 1 day and less than the escalation threshold (365 at most).")
    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status != "Active":
            raise LedgerError("This engagement is closed; its settings are frozen.")
        eng.client_email = client_email
        eng.partner_email = partner_email
        eng.age_warn_days = warn
        eng.age_escalate_days = crit
        session.commit()
        return eng
    except Exception:
        session.rollback()
        raise


# ---- tokenised client link -------------------------------------------------------------------
def owner_name_key(owner: Optional[str]) -> str:
    """'Meera Shah (Finance Controller)' and 'meera shah' are the same named person."""
    return owner_parts(owner or "")[0].strip().lower()


def token_covers(token: ClientToken, req: Request) -> bool:
    """A whole-engagement link covers every request; an owner-scoped link only that person's."""
    return not token.owner_person or owner_name_key(token.owner_person) == owner_name_key(req.owner_person)


def engagement_owner_names(session: Session, engagement_id: int) -> list[str]:
    """The distinct named client-side owners in the engagement (role dropped), for scoped links."""
    names: dict[str, str] = {}
    for (owner,) in session.execute(
        select(Request.owner_person).where(Request.engagement_id == engagement_id).distinct()
    ):
        name = owner_parts(owner)[0]
        names.setdefault(name.lower(), name)
    return sorted(names.values())


def _live_tokens(session: Session, engagement_id: int) -> list[ClientToken]:
    now = utcnow()
    return list(
        session.execute(
            select(ClientToken)
            .where(ClientToken.engagement_id == engagement_id, ClientToken.revoked_at.is_(None), ClientToken.expires_at > now)
            .order_by(ClientToken.id.desc())
        ).scalars()
    )


def live_tokens(session: Session, engagement_id: int) -> list[ClientToken]:
    """Every live link of the engagement: the whole-engagement one and any owner-scoped ones."""
    return _live_tokens(session, engagement_id)


def _ensure_token(
    session: Session, eng: Engagement, actor: str, days: int = TOKEN_DEFAULT_DAYS, owner: Optional[str] = None
) -> ClientToken:
    """The live link with this scope (whole engagement by default), creating one if there is none.
    Flushes, never commits."""
    for token in _live_tokens(session, eng.id):
        if owner_name_key(token.owner_person) == owner_name_key(owner):
            return token
    now = utcnow()
    token = ClientToken(
        engagement_id=eng.id, expires_at=now + timedelta(days=days), created_by=actor, created_at=now,
        owner_person=_clean(owner),
    )
    session.add(token)
    session.flush()
    return token


def _link_token_for(session: Session, eng: Engagement, req: Request, actor: str) -> ClientToken:
    """The link to put in a reminder about this request: its owner's own link if one is live,
    otherwise the engagement-wide one."""
    for token in _live_tokens(session, eng.id):
        if token.owner_person and token_covers(token, req):
            return token
    return _ensure_token(session, eng, actor)


def active_token(session: Session, engagement_id: int, owner: Optional[str] = None) -> Optional[ClientToken]:
    """The live link with the given scope (the whole-engagement link when `owner` is None)."""
    for token in _live_tokens(session, engagement_id):
        if owner_name_key(token.owner_person) == owner_name_key(owner):
            return token
    return None


def list_tokens(session: Session, engagement_id: int) -> list[ClientToken]:
    return list(
        session.execute(
            select(ClientToken).where(ClientToken.engagement_id == engagement_id).order_by(ClientToken.id.desc())
        ).scalars()
    )


def create_client_token(
    session: Session,
    engagement_id: int,
    actor: str,
    *,
    days: int = TOKEN_DEFAULT_DAYS,
    expires_at: Optional[datetime] = None,
    owner_person: Optional[str] = None,
) -> ClientToken:
    """Generate a link - for the whole engagement, or scoped to one named client-side owner - and
    withdraw the previous live link OF THE SAME SCOPE (other scopes keep working)."""
    actor = _require_actor(actor)
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise LedgerError("The link's validity must be a whole number of days.") from exc
    if not 1 <= days <= 180:
        raise LedgerError("A link can be valid for 1 to 180 days.")
    owner = _clean(owner_person)
    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status != "Active":
            raise LedgerError("This engagement is closed; no client link can be issued.")
        if owner:
            known = {n.lower(): n for n in engagement_owner_names(session, engagement_id)}
            canonical = known.get(owner_name_key(owner))
            if canonical is None:
                raise LedgerError(f"No request in this engagement is held by '{owner}'.")
            owner = canonical
        now = utcnow()
        for old in session.execute(
            select(ClientToken).where(ClientToken.engagement_id == engagement_id, ClientToken.revoked_at.is_(None))
        ).scalars():
            if owner_name_key(old.owner_person) == owner_name_key(owner):
                old.revoked_at = now
        token = ClientToken(
            engagement_id=engagement_id, created_by=actor, created_at=now, owner_person=owner,
            expires_at=expires_at or (now + timedelta(days=days)),
        )
        session.add(token)
        session.commit()
        return token
    except Exception:
        session.rollback()
        raise


def revoke_client_token(session: Session, token_id: int, actor: str) -> ClientToken:
    _require_actor(actor)
    try:
        token = session.get(ClientToken, token_id)
        if token is None:
            raise NotFoundError("That link does not exist.")
        if token.revoked_at is None:
            token.revoked_at = utcnow()
        session.commit()
        return token
    except Exception:
        session.rollback()
        raise


def resolve_token(session: Session, token_str: str) -> tuple[ClientToken, Engagement]:
    """The link and its engagement, or a TokenError saying why it cannot be used."""
    token = session.execute(select(ClientToken).where(ClientToken.token == (token_str or ""))).scalar_one_or_none()
    if token is None:
        raise TokenError("invalid", "This link is not valid. Please check you opened the whole link, or ask us to send it again.")
    status = token.status()
    if status == "revoked":
        raise TokenError("revoked", "This link has been withdrawn. Please ask us for a new one.")
    if status == "expired":
        raise TokenError("expired", "This link has expired. Please ask us to send a fresh one.")
    eng = session.get(Engagement, token.engagement_id)
    if eng is None or eng.status != "Active":
        raise TokenError("closed", "This engagement has been closed, so documents can no longer be received through this link.")
    return token, eng


def open_client_link(session: Session, token_str: str) -> dict:
    """The client opened the link: every Sent request the link covers becomes Acknowledged (an
    ACKNOWLEDGED event each - "seen" is a different fact from "not seen"), and every chase on those
    requests that was waiting to be opened is marked opened. An owner-scoped link touches only that
    person's requests. Repeat opens change nothing."""
    token, eng = resolve_token(session, token_str)
    acknowledged, covered_ids = [], set()
    for req in engagement_requests(session, eng.id):
        if not token_covers(token, req):
            continue
        covered_ids.add(req.id)
        if req.state == "Sent":
            append_event(
                session, req.id, E.ACKNOWLEDGED, CLIENT_ACTOR, channel="Tokenised link",
                payload={"note": "The client opened the upload link."},
            )
            acknowledged.append(req.ref)
    now = utcnow()
    opened = 0
    try:
        pending = session.execute(
            select(Chase).join(Request, Request.id == Chase.request_id)
            .where(Request.engagement_id == eng.id, Chase.opened.is_(False))
        ).scalars().all()
        for chase in pending:
            if chase.request_id in covered_ids:
                chase.opened = True
                chase.opened_at = now
                opened += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {"token": token, "engagement": eng, "acknowledged": acknowledged, "chases_opened": opened}


def _client_buckets(session: Session, eng: Engagement, token: ClientToken):
    """The link's requests split into: open (in the queue), received (submitted / under review) and
    unavailable (the client said they do not have it). The queue is strictly priority order -
    P1 first, and Material before Routine within a priority."""
    open_, received, unavailable = [], [], []
    for req in engagement_requests(session, eng.id):
        if not token_covers(token, req):
            continue
        state = req.state
        if state in SUBMITTABLE_STATES:
            (unavailable if req.reported_unavailable else open_).append(req)
        elif state in ("Submitted", "Under review"):
            received.append(req)
    open_.sort(key=lambda r: (r.priority, 0 if r.significance == "Material" else 1, r.id))
    return open_, received, unavailable


def client_view(session: Session, eng: Engagement, token: ClientToken) -> dict:
    """What the client page shows: at most CLIENT_QUEUE_SIZE open requests at a time, in priority
    order and plainly worded, with the reason for any earlier rejection; what has been received;
    and what the client said they do not have. Finishing the shown items unlocks the next ones."""
    open_, received, unavailable = _client_buckets(session, eng, token)
    shown = []
    for req in open_[:CLIENT_QUEUE_SIZE]:
        rejected = next((ev for ev in reversed(req.events) if ev.event_type == E.REJECTED), None)
        shown.append({
            "id": req.id,
            "ref": req.ref,
            "label": req.description or req.document,
            "document": req.document,
            "period": _period_phrase(req.period_from, req.period_to),
            "owner": req.owner_person,
            "priority": req.priority,
            "material": req.significance == "Material",
            "attempt": req.attempt_count,
            "state": req.state,
            "rejection": (
                {"message": (rejected.payload or {}).get("client_message"),
                 "detail": (rejected.payload or {}).get("detail") or (rejected.payload or {}).get("note")}
                if rejected and req.state == "Re-requested" else None
            ),
        })
    flagged = []
    for req in unavailable:
        ev = req.reported_unavailable
        flagged.append({"id": req.id, "document": req.document, "reason": (ev.payload or {}).get("reason"), "at": ev.at})
    answered = len(received) + len(flagged)
    total = answered + len(open_)
    return {
        "open": shown,
        "shown": len(shown),
        "total_open": len(open_),
        "hidden": max(len(open_) - len(shown), 0),
        "queue_size": CLIENT_QUEUE_SIZE,
        "received": [{"ref": r.ref, "document": r.document} for r in received],
        "unavailable": flagged,
        "answered": answered,
        "total": total,
        "percent": round(100 * answered / total) if total else 100,
        "scope_name": token.owner_person,
    }


def _client_request(session: Session, token_str: str, request_id: int):
    """Resolve the link and the request it is being used on; refuse anything outside its scope and
    anything the queue has not unlocked yet. Returns (token, engagement, request)."""
    token, eng = resolve_token(session, token_str)
    req = session.get(Request, request_id)
    if req is None or req.engagement_id != eng.id or not token_covers(token, req):
        raise NotFoundError("That request is not part of this link.")
    open_, _received, unavailable = _client_buckets(session, eng, token)
    if request_id in {r.id for r in unavailable}:
        raise LedgerError("You told us you do not have this one. If you find it, please contact us and we will reopen it.")
    if request_id in {r.id for r in open_} and request_id not in {r.id for r in open_[:CLIENT_QUEUE_SIZE]}:
        raise LedgerError("Please finish the items shown first - this one unlocks as you complete them.")
    return token, eng, req


def client_upload(
    session: Session,
    token_str: str,
    request_id: int,
    files: list[tuple[str, bytes]],
    submitted_by: Optional[str] = None,
) -> dict:
    """A client upload through the link: no account, no password. The files are the pages of ONE
    submission, in exactly the order given (page_order 1, 2, 3 ...). The token is the only
    credential; it only reaches the requests it covers, and only the ones currently shown."""
    _token, _eng, req = _client_request(session, token_str, request_id)
    name = _clean(submitted_by)
    return record_submission(
        session, request_id, f"{name} (client)" if name else CLIENT_ACTOR, files,
        channel="Tokenised link", submitted_by=name or req.owner_person,
    )


def client_clear_unavailable(
    session: Session, token_str: str, request_id: int, submitted_by: Optional[str] = None
) -> RequestEvent:
    """"I found it" / "I clicked that by mistake": the client takes back their "I don't have this".
    Appends a CLIENT_UNAVAILABLE_CLEARED event (the first report stays in the history) and the
    request returns to the client's queue."""
    token, eng = resolve_token(session, token_str)
    req = session.get(Request, request_id)
    if req is None or req.engagement_id != eng.id or not token_covers(token, req):
        raise NotFoundError("That request is not part of this link.")
    name = _clean(submitted_by)
    try:
        locked = _lock_request(session, request_id)
        if not locked.reported_unavailable:
            raise InvalidTransition("This request is not marked as something you do not have.")
        event = _insert_event(
            session, locked, E.UNAVAILABLE_CLEARED, f"{name} (client)" if name else CLIENT_ACTOR,
            "Tokenised link", {"note": "The client says they found it / reported it by mistake."}, utcnow(),
        )
        session.commit()
        return event
    except Exception:
        session.rollback()
        raise


def client_report_unavailable(
    session: Session, token_str: str, request_id: int, reason: str, submitted_by: Optional[str] = None
) -> RequestEvent:
    """"I don't have this": the client says, with a reason, that they cannot provide the document.
    Appends a CLIENT_REPORTED_UNAVAILABLE event (it is information - it does not change the state,
    and the firm decides whether to waive, mark not applicable, or press on)."""
    reason = _clean(reason)
    if not reason or len(reason) < 3:
        raise LedgerError("Please tell us briefly why you do not have it.")
    if len(reason) > 1000:
        raise LedgerError("That explanation is too long (1000 characters maximum).")
    token, eng, req = _client_request(session, token_str, request_id)
    name = _clean(submitted_by)
    try:
        locked = _lock_request(session, request_id)
        if locked.state not in SUBMITTABLE_STATES or locked.engagement.status != "Active":
            raise InvalidTransition(f"This request cannot be answered now (it is '{locked.state}').")
        event = _insert_event(
            session, locked, E.CLIENT_UNAVAILABLE, f"{name} (client)" if name else CLIENT_ACTOR,
            "Tokenised link", {"reason": reason}, utcnow(),
        )
        session.commit()
        return event
    except Exception:
        session.rollback()
        raise


# ---- batch release -----------------------------------------------------------------------------
def _request_lines(reqs: list[Request]) -> str:
    lines = []
    for i, r in enumerate(sorted(reqs, key=lambda r: (r.priority, r.id)), start=1):
        lines.append(f"  {i}. {r.document} - {_period_phrase(r.period_from, r.period_to)} (for {r.owner_person})")
    return "\n".join(lines)


def release_requests(session: Session, engagement_id: int, request_ids: list[int], actor: str) -> dict:
    """Release several Drafted requests to the client together. In ONE transaction: a SENT event for
    each (channel Email), the engagement's client link is created if needed, and one email listing
    the requests with the link is sent (or logged in the Outbox). If any request is not Drafted, or
    the email cannot be sent, nothing is released."""
    actor = _require_actor(actor)
    ids = list(dict.fromkeys(int(i) for i in request_ids))
    if not ids:
        raise LedgerError("Tick at least one Drafted request to release.")
    mail_args = {}
    try:
        eng = _lock_engagement(session, engagement_id)
        if eng.status != "Active":
            raise LedgerError("This engagement is closed; nothing can be released.")
        if not eng.client_email:
            raise LedgerError("Set the client's email address first (Settings) - the release email goes there.")
        reqs, problems = [], []
        for rid in ids:
            req = _lock_request(session, rid)
            if req.engagement_id != engagement_id:
                problems.append(f"request {rid} is not in this engagement")
            elif req.state != "Drafted":
                problems.append(f"{req.ref} is {req.state}, not Drafted")
            else:
                reqs.append(req)
        if problems:
            raise LedgerError("Nothing was released: " + "; ".join(problems) + ".")

        token = _ensure_token(session, eng, actor)
        at = utcnow()
        for req in reqs:
            _insert_event(session, req, E.SENT, actor, "Email",
                          {"note": f"Released to the client by email ({eng.client_email}) with the upload link."}, at)
        body = (
            f"Dear {eng.client_name} team,\n\n"
            f"We have released {len(reqs)} document request{'s' if len(reqs) != 1 else ''} for {eng.name}. "
            f"Please upload them using the link below - no login or password is needed, and it works "
            f"from a phone.\n\n{emailer.client_link_url(token.token)}\n\n"
            f"The link is valid until {token.expires_at.strftime('%d %B %Y')}.\n\n"
            f"Requests, in the order we would like them:\n{_request_lines(reqs)}\n\n"
            f"Thank you,\n{actor}\n"
        )
        subject = f"{len(reqs)} document request{'s' if len(reqs) != 1 else ''} - {eng.name}"
        mail_args = dict(kind="request_batch", to_address=eng.client_email, subject=subject, body=body,
                         created_by=actor, engagement_id=eng.id, token_id=token.id)
        mail = emailer.dispatch(session, **mail_args)
        session.commit()
        return {"released": [r.ref for r in reqs], "email": mail, "token": token,
                "link": emailer.client_link_url(token.token)}
    except emailer.EmailDeliveryError as exc:
        session.rollback()
        emailer.record_failure(session, error=str(exc), **{k: v for k, v in mail_args.items() if k != "token_id"})
        raise LedgerError(f"{exc} Nothing was released.") from exc
    except Exception:
        session.rollback()
        raise


def send_client_link(
    session: Session, engagement_id: int, actor: str, token_id: Optional[int] = None
) -> OutboundEmail:
    """(Re-)send a live link - the whole-engagement one by default, or the one given - with the list
    of requests it covers that are waiting on the client, to the client's email address."""
    actor = _require_actor(actor)
    eng = get_engagement(session, engagement_id)
    if eng.status != "Active":
        raise LedgerError("This engagement is closed; no client link can be sent.")
    if not eng.client_email:
        raise LedgerError("Set the client's email address first (Settings).")
    if token_id:
        token = next((t for t in _live_tokens(session, engagement_id) if t.id == int(token_id)), None)
    else:
        token = active_token(session, engagement_id)
    if token is None:
        raise LedgerError("There is no live link - generate one first.")
    waiting = [
        r for r in engagement_requests(session, engagement_id)
        if r.state in SUBMITTABLE_STATES and token_covers(token, r)
    ]
    scope = f" for {token.owner_person}" if token.owner_person else ""
    body = (
        f"Dear {eng.client_name} team,\n\n"
        f"Here is your upload link{scope} for {eng.name}. No login or password is needed.\n\n"
        f"{emailer.client_link_url(token.token)}\n\n"
        f"It is valid until {token.expires_at.strftime('%d %B %Y')}. "
        f"We show five items at a time; as you finish them the next ones appear.\n\n"
        + (f"Still waiting on:\n{_request_lines(waiting)}\n\n" if waiting else "")
        + f"Thank you,\n{actor}\n"
    )
    args = dict(kind="client_link", to_address=eng.client_email,
                subject=f"Your upload link{scope} - {eng.name}",
                body=body, created_by=actor, engagement_id=eng.id, token_id=token.id)
    try:
        mail = emailer.dispatch(session, **args)
        session.commit()
        return mail
    except emailer.EmailDeliveryError as exc:
        session.rollback()
        emailer.record_failure(session, error=str(exc), **{k: v for k, v in args.items() if k != "token_id"})
        raise LedgerError(str(exc)) from exc


# ---- partner escalation --------------------------------------------------------------------------
def escalate_to_partner(session: Session, engagement_id: int, actor: str) -> dict:
    """Email the partner one digest of everything past the engagement's escalation threshold that has
    not been escalated yet, and record a NOTE ("Escalated to the partner") on each request."""
    actor = _require_actor(actor)
    board = outstanding_board(session, engagement_id, "age")
    eng = board["engagement"]
    if eng.status != "Active":
        raise LedgerError("This engagement is closed.")
    if not eng.partner_email:
        raise LedgerError("Set the partner's email address first (Settings).")
    due = [b for b in board["rows"] if b["band"] == "crit" and not b["escalated"]]
    if not due:
        raise LedgerError(f"Nothing is past the {eng.age_escalate_days}-day escalation threshold, or it has all been escalated already.")
    lines = [
        f"  {b['ref']}  {b['document']} - {b['age']} days - {b['chase_count']} chase{'s' if b['chase_count'] != 1 else ''}"
        f" - held by {b['owner']}{' [MATERIAL]' if b['material'] else ''}"
        for b in due
    ]
    body = (
        f"{eng.name}: {len(due)} request{'s are' if len(due) != 1 else ' is'} past the "
        f"{eng.age_escalate_days}-day escalation threshold.\n\n" + "\n".join(lines) +
        f"\n\nEscalated by {actor}.\n"
    )
    args = dict(kind="escalation", to_address=eng.partner_email,
                subject=f"Escalation: {len(due)} overdue request{'s' if len(due) != 1 else ''} - {eng.name}",
                body=body, created_by=actor, engagement_id=eng.id)
    try:
        at = utcnow()
        for b in due:
            req = _lock_request(session, b["id"])
            _insert_event(session, req, E.NOTE, actor, None,
                          {"note": f"Escalated to the partner: {b['age']} days outstanding "
                                   f"(threshold {eng.age_escalate_days}).", "escalation": eng.partner_email}, at)
        mail = emailer.dispatch(session, **args)
        session.commit()
        return {"refs": [b["ref"] for b in due], "email": mail}
    except emailer.EmailDeliveryError as exc:
        session.rollback()
        emailer.record_failure(session, error=str(exc), **args)
        raise LedgerError(f"{exc} Nothing was escalated.") from exc
    except Exception:
        session.rollback()
        raise


# ---- outbox ------------------------------------------------------------------------------------------
def list_outbox(session: Session, engagement_id: Optional[int] = None, limit: int = 200) -> list[OutboundEmail]:
    stmt = select(OutboundEmail).order_by(OutboundEmail.id.desc()).limit(limit)
    if engagement_id:
        stmt = stmt.where(OutboundEmail.engagement_id == engagement_id)
    return list(session.execute(stmt).scalars())


# ---- template manager: edit client-facing labels, publish a new immutable version --------------------
def template_versions(session: Session, engagement_type: str) -> list[RequestTemplate]:
    return list(
        session.execute(
            select(RequestTemplate)
            .where(RequestTemplate.engagement_type == engagement_type)
            .options(selectinload(RequestTemplate.items))
            .order_by(RequestTemplate.version.desc())
        ).scalars()
    )


def publish_template_version(
    session: Session,
    engagement_type: str,
    actor: str,
    label_edits: dict[int, Optional[str]],
    note: Optional[str] = None,
) -> RequestTemplate:
    """Publish version N+1 of a template with new client-facing labels (`label_text`). Every other
    field is copied as it was. Earlier versions are never touched, so engagements that already used
    them keep the wording they were asked in. `label_edits` maps template-item id -> new label."""
    actor = _require_actor(actor)
    if engagement_type not in ENGAGEMENT_TYPES:
        raise LedgerError(f"Unknown engagement type '{engagement_type}'.")
    try:
        # Lock the current latest version so two people publishing at once get v2 and v3, not two v2s.
        latest_id = session.execute(
            select(RequestTemplate.id)
            .where(RequestTemplate.engagement_type == engagement_type)
            .order_by(RequestTemplate.version.desc()).limit(1).with_for_update()
        ).scalar_one_or_none()
        if latest_id is None:
            raise NotFoundError(f"No requirement-list template for '{engagement_type}'.")
        latest = get_template(session, engagement_type)
        new_items, changed = [], 0
        for item in latest.items:
            label = item.label_text
            if item.id in label_edits:
                edited = _clean(label_edits[item.id])
                if edited and len(edited) > 2000:
                    raise LedgerError(f"The label for '{item.document}' is too long (2000 characters maximum).")
                if edited != _clean(item.label_text):
                    label, changed = edited, changed + 1
            new_items.append(
                TemplateItem(
                    position=item.position, document=item.document, description=item.description,
                    label_text=label, condition_expr=item.condition_expr,
                    significance_default=item.significance_default, priority_default=item.priority_default,
                    period_mode=item.period_mode,
                )
            )
        if not changed:
            raise LedgerError("No label was changed - there is nothing to publish.")
        tmpl = RequestTemplate(
            engagement_type=engagement_type, version=latest.version + 1, title=latest.title,
            published_by=actor, change_note=_clean(note) or f"{changed} label{'s' if changed != 1 else ''} edited",
        )
        tmpl.items.extend(new_items)
        session.add(tmpl)
        session.commit()
        return tmpl
    except Exception:
        session.rollback()
        raise


# ---- the pages of a submission, assembled into ONE document ----------------------------------------
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")
ASSEMBLE_EXTENSIONS = IMAGE_EXTENSIONS + (".pdf",)


def can_assemble(submission: Submission) -> bool:
    """True when every page is an image or a PDF, so the pages can be combined into one PDF."""
    return bool(submission.files) and all(f.filename.lower().endswith(ASSEMBLE_EXTENSIONS) for f in submission.files)


def assemble_submission_pdf(session: Session, submission_id: int) -> tuple[bytes, str]:
    """Combine a submission's pages, in page_order, into one PDF: each photo becomes a page (rotated
    upright from its camera orientation), PDFs are appended as they are. Returns (bytes, filename).
    The stored originals are never touched."""
    import io

    from PIL import Image, ImageOps
    from pypdf import PdfReader, PdfWriter

    sub = session.execute(
        select(Submission).where(Submission.id == submission_id).options(selectinload(Submission.files))
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(f"Submission {submission_id} does not exist.")
    if not sub.files:
        raise LedgerError("This submission has no pages.")
    root = UPLOAD_DIR.resolve()
    writer = PdfWriter()
    for f in sorted(sub.files, key=lambda x: x.page_order):
        path = (root / f.storage_path).resolve()
        if root not in path.parents or not path.is_file():
            raise NotFoundError(f"The stored file '{f.filename}' is missing.")
        data = path.read_bytes()
        try:
            if data[:5] == b"%PDF-":
                writer.append(PdfReader(io.BytesIO(data)))
            else:
                image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="PDF", resolution=150.0)
                writer.append(PdfReader(io.BytesIO(buf.getvalue())))
        except Exception as exc:  # noqa: BLE001 - an unreadable image or PDF, whatever the library says
            raise LedgerError(
                f"Page {f.page_order} ('{f.filename}') is not an image or PDF that can be combined: {exc}"
            ) from exc
    out = io.BytesIO()
    writer.write(out)
    req = session.get(Request, sub.request_id)
    return out.getvalue(), f"{req.ref}_attempt{sub.attempt_number}.pdf"
