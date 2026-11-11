"""Business logic for the Document Request Ledger.

Everything that changes a request goes through this module, and the only thing it ever does to
history is INSERT a `request_event` row. There is no update or delete of events anywhere.
"""
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
    "Sent": {E.ACKNOWLEDGED, E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Acknowledged": {E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Submitted": {E.UNDER_REVIEW, E.ACCEPTED, E.REJECTED, E.WAIVED, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Under review": {E.ACCEPTED, E.REJECTED, E.WAIVED, E.UNFULFILLED, E.NOTE, E.ARRIVAL},
    "Rejected": {E.RE_REQUESTED},
    "Re-requested": {
        E.ACKNOWLEDGED, E.SUBMITTED, E.CHASED, E.WAIVED, E.NOT_APPLICABLE, E.UNFULFILLED, E.NOTE, E.ARRIVAL,
    },
    "Accepted": {E.NOTE, E.ARRIVAL},
    "Waived": {E.NOTE, E.ARRIVAL},
    "Not applicable": {E.NOTE, E.ARRIVAL},
    "Unfulfilled at report date": {E.NOTE, E.ARRIVAL},
}

# Event types a caller may not append directly. SUBMITTED and ARRIVAL_RECORDED are written by
# record_submission() (a submission always has a file); ACCEPTED/REJECTED of a pending submission
# go through review_submission().
SYSTEM_ONLY = {E.DRAFTED, E.RE_REQUESTED, E.UNFULFILLED, E.SUBMITTED, E.ARRIVAL}

# Display order for the action menu.
ACTION_ORDER = [
    E.SENT, E.ACKNOWLEDGED, E.CHASED, E.SUBMITTED, E.UNDER_REVIEW,
    E.ACCEPTED, E.REJECTED, E.WAIVED, E.NOT_APPLICABLE, E.NOTE,
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
    E.REJECTED: {"detail", "note"},
    E.WAIVED: {"reason", "note"},
    E.NOT_APPLICABLE: {"reason", "note"},
    E.NOTE: {"note"},
}


def pending_submission(request: Request) -> Optional[Submission]:
    """The submission awaiting a review decision, if any."""
    return next((sub for sub in reversed(request.submissions) if sub.outcome == "pending"), None)


def available_actions(request: Request) -> list[tuple[str, str]]:
    """(event_type, label) pairs that are legal for this request right now."""
    if request.engagement.status == "Closed":
        allowed = {E.NOTE}
    else:
        allowed = ALLOWED_NEXT.get(request.state or "", set()) - SYSTEM_ONLY
        if pending_submission(request):
            allowed = allowed - {E.ACCEPTED, E.REJECTED}  # decided in the Audit Review panel
    return [(et, EVENT_LABEL[et]) for et in ACTION_ORDER if et in allowed]


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


def _build_payload(event_type: str, channel: Optional[str], raw: dict) -> tuple[Optional[str], dict]:
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
        label, client_message = REASON_CODES[code]
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
        if req.engagement.status == "Closed" and event_type != E.NOTE:
            raise InvalidTransition("The engagement is closed: its requests are frozen. Only notes can be added.")
        if event_type not in ALLOWED_NEXT.get(state or "", set()):
            raise InvalidTransition(
                f"Cannot record '{EVENT_LABEL[event_type]}' while the request is '{state}'."
            )

        if event_type in (E.ACCEPTED, E.REJECTED) and pending_submission(req):
            raise InvalidTransition(
                "A submission is awaiting review - use the Audit Review panel (Accept / Reject Document)."
            )

        channel, clean_payload = _build_payload(event_type, channel, {**(payload or {})})
        at = _aware(at)

        if event_type == E.REJECTED:
            appended = _reject_and_rerequest(session, req, actor, channel, clean_payload, at)
        else:
            appended = [_insert_event(session, req, event_type, actor, channel, clean_payload, at)]
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
            if req.engagement.status == "Closed":
                raise InvalidTransition("The engagement is closed: its requests are frozen.")
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
            label, client_message = REASON_CODES[code]
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


def _match_existing(
    item: TemplateItem, document: str, pf: Optional[date], pt: Optional[date], requests: list[Request]
) -> tuple[Optional[Request], Optional[str]]:
    """Is this item already covered in the engagement?
    'generated' - a request was already made from this very template item (any state);
    'live'      - an open request for the same document and period (duplicate prevention);
    'accepted'  - the same document and period is already accepted, so asking again is pointless."""
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
            existing, kind = _match_existing(item, document, pf, pt, requests)
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
            existing, kind = _match_existing(item, document, pf, pt, requests)
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


def age_band(days: int) -> str:
    """'crit' beyond 21 days, 'warn' beyond 14, else 'ok'."""
    if days > AGE_CRIT_DAYS:
        return "crit"
    if days > AGE_WARN_DAYS:
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
) -> dict:
    """Append a CHASED event (channel + template or short note). Goes through the same transition
    engine as every other event, so a chase on a request that is not waiting on the client (or on a
    closed engagement) is refused. Returns the event and the new chase count."""
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
    events = append_event(
        session, request_id, E.CHASED, actor,
        channel=channel, payload={"note": note, "template": label}, at=at,
    )
    req = get_request(session, request_id)
    return {"event": events[0], "chase_count": req.chase_count, "request": req}


# ---- the board -------------------------------------------------------------------------------
def _board_row(req: Request, now: datetime) -> dict:
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
        "band": age_band(age),
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
    significance Material pinned above Routine (and never hidden by a filter)
    chase        most-chased first
    """
    cut = cut if cut in dict(BOARD_CUTS) else "age"
    now = now or utcnow()
    eng = get_engagement(session, engagement_id)
    requests = engagement_requests(session, engagement_id)

    rows = [_board_row(r, now) for r in requests if r.state in CLIENT_SIDE_STATES]
    by_age = sorted(rows, key=lambda b: (-b["age"], -b["chase_count"], b["id"]))
    by_chase = sorted(rows, key=lambda b: (-b["chase_count"], -b["age"], b["id"]))

    groups: list[dict] = []
    if cut == "owner":
        bucket: dict[str, dict] = {}
        for b in by_age:
            g = bucket.setdefault(
                b["owner_name"].lower(),
                {"name": b["owner_name"], "role": b["owner_role"], "rows": [], "material": 0,
                 "chases": 0, "oldest": 0},
            )
            g["rows"].append(b)
            g["material"] += 1 if b["material"] else 0
            g["chases"] += b["chase_count"]
            g["oldest"] = max(g["oldest"], b["age"])
        groups = sorted(bucket.values(), key=lambda g: (-len(g["rows"]), -g["oldest"], g["name"]))
        for g in groups:
            g["band"] = age_band(g["oldest"])

    if cut == "chase":
        ordered = by_chase
    elif cut == "owner":
        ordered = [b for g in groups for b in g["rows"]]
    elif cut == "significance":
        ordered = [b for b in by_age if b["material"]] + [b for b in by_age if not b["material"]]
    else:
        ordered = by_age

    summary = {
        "outstanding": len(rows),
        "material": sum(1 for b in rows if b["material"]),
        "warn": sum(1 for b in rows if b["band"] == "warn"),
        "crit": sum(1 for b in rows if b["band"] == "crit"),
        "chased_3plus": sum(1 for b in rows if b["chase_count"] >= 3),
        "owners": len({b["owner_name"].lower() for b in rows}),
        "with_audit_team": sum(1 for r in requests if r.state in ("Submitted", "Under review")),
        "not_sent": sum(1 for r in requests if r.state == "Drafted"),
    }
    return {
        "engagement": eng,
        "cut": cut,
        "cuts": BOARD_CUTS,
        "rows": ordered,
        "groups": groups,
        "material_rows": [b for b in by_age if b["material"]],
        "routine_rows": [b for b in by_age if not b["material"]],
        "summary": summary,
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
