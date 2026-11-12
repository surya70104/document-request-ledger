"""Schema for the Document Request Ledger (Pass 1).

Design rules (from the module spec):
  * The unit of record is the REQUEST, not the file.
  * `request` rows are immutable once written. Their state, attempt count and chase count are NOT
    stored - they are derived from the request's events.
  * `request_event` is strictly append-only. There is no UPDATE or DELETE path in the application
    (ORM listeners below refuse them) and PostgreSQL triggers (see database.py) refuse them too.
"""
import secrets
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, object_session, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------
class EventType:
    # Events that move the request to one of the eleven lifecycle states
    DRAFTED = "DRAFTED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    RE_REQUESTED = "RE_REQUESTED"
    WAIVED = "WAIVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNFULFILLED = "UNFULFILLED_AT_REPORT_DATE"
    # Events that are part of the history but do not change the state
    CHASED = "CHASED"
    NOTE = "NOTE"
    ARRIVAL = "ARRIVAL_RECORDED"  # identical content arrived again (matched on SHA-256)
    # The client said "I don't have this" (with a reason). Information, not a state change.
    CLIENT_UNAVAILABLE = "CLIENT_REPORTED_UNAVAILABLE"


# The eleven lifecycle states, in lifecycle order.
STATES = [
    "Drafted",
    "Sent",
    "Acknowledged",
    "Submitted",
    "Under review",
    "Accepted",
    "Rejected",
    "Re-requested",
    "Waived",
    "Not applicable",
    "Unfulfilled at report date",
]

EVENT_STATE = {
    EventType.DRAFTED: "Drafted",
    EventType.SENT: "Sent",
    EventType.ACKNOWLEDGED: "Acknowledged",
    EventType.SUBMITTED: "Submitted",
    EventType.UNDER_REVIEW: "Under review",
    EventType.ACCEPTED: "Accepted",
    EventType.REJECTED: "Rejected",
    EventType.RE_REQUESTED: "Re-requested",
    EventType.WAIVED: "Waived",
    EventType.NOT_APPLICABLE: "Not applicable",
    EventType.UNFULFILLED: "Unfulfilled at report date",
}

EVENT_LABEL = {
    **EVENT_STATE,
    EventType.CHASED: "Chased",
    EventType.NOTE: "Note",
    EventType.ARRIVAL: "Identical content re-sent",
    EventType.CLIENT_UNAVAILABLE: "Client reported unavailable",
}

ALL_EVENT_TYPES = list(EVENT_LABEL.keys())

# A request in one of these states is closed out: nothing further is expected from the client.
TERMINAL_STATES = {"Accepted", "Waived", "Not applicable", "Unfulfilled at report date"}

SIGNIFICANCE = ("Material", "Routine")

ENGAGEMENT_TYPES = (
    "Statutory audit",
    "Tax audit",
    "GST annual return",
    "Bank audit",
    "Internal audit",
    "Certification work",
)
CHANNELS = ("Email", "WhatsApp", "Tokenised link", "Web upload", "Phone", "In person", "Manual")
SUBMISSION_OUTCOMES = ("pending", "accepted", "rejected")

# The eight rejection reason codes: (label, what the client is told)
REASON_CODES = {
    "ILLEGIBLE": (
        "Illegible",
        "The scan or photo cannot be read. Retake it in better light, whole page in frame.",
    ),
    "WRONG_PERIOD": ("Wrong period", "This covers a different period from the one asked for."),
    "WRONG_ENTITY": ("Wrong entity", "This belongs to a different entity."),
    "UNSIGNED": (
        "Unsigned",
        "The document requires a signature or seal and does not carry one.",
    ),
    "INCOMPLETE": ("Incomplete", "Pages are missing."),
    "SUPERSEDED": ("Superseded", "A later version exists and is what is needed."),
    "PASSWORD_PROTECTED": ("Password protected", "It cannot be opened. The password is needed."),
    "NOT_THE_DOCUMENT_ASKED_FOR": ("Not the document asked for", "Something arrived, but not this."),
}
# Code names used by earlier builds, still accepted on input.
REASON_CODE_ALIASES = {"NOT_THE_DOCUMENT": "NOT_THE_DOCUMENT_ASKED_FOR"}


def _sql_list(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


class AppendOnlyViolation(Exception):
    """Raised when code tries to update or delete something that is append-only."""


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------
class Engagement(Base):
    __tablename__ = "engagement"
    __table_args__ = (
        CheckConstraint("status IN ('Active', 'Closed')", name="ck_engagement_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    engagement_type: Mapped[str] = mapped_column(String(60), nullable=False, default="Statutory audit")
    period_from: Mapped[Optional[date]] = mapped_column(Date)
    period_to: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="Active")
    report_date: Mapped[Optional[date]] = mapped_column(Date)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Pass 1 completion: where the client link / reminders go, who escalations go to, and the
    # ageing thresholds (days outstanding) for the amber warning and the partner escalation.
    client_email: Mapped[Optional[str]] = mapped_column(String(200))
    partner_email: Mapped[Optional[str]] = mapped_column(String(200))
    age_warn_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14, server_default=text("14"))
    age_escalate_days: Mapped[int] = mapped_column(Integer, nullable=False, default=21, server_default=text("21"))
    # Entity profile: flags/attributes that template-item conditions are evaluated against.
    entity_profile: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    requests: Mapped[list["Request"]] = relationship(back_populates="engagement", order_by="Request.id")


# --------------------------------------------------------------------------------------------
# Requirement-list templates (versioned, immutable once published)
# --------------------------------------------------------------------------------------------
class RequestTemplate(Base):
    """One version of the requirement list for an engagement type. Never edited: a change is a
    new version, so this year's change does not rewrite what was asked last year."""

    __tablename__ = "request_template"
    __table_args__ = (UniqueConstraint("engagement_type", "version", name="uq_template_type_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_type: Mapped[str] = mapped_column(String(60), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_by: Mapped[Optional[str]] = mapped_column(String(200))  # NULL = shipped with the module
    change_note: Mapped[Optional[str]] = mapped_column(Text)

    items: Mapped[list["TemplateItem"]] = relationship(
        back_populates="template", order_by="TemplateItem.position"
    )


class TemplateItem(Base):
    __tablename__ = "template_item"
    __table_args__ = (
        CheckConstraint("significance_default IN ('Material', 'Routine')", name="ck_item_significance"),
        CheckConstraint("priority_default BETWEEN 1 AND 5", name="ck_item_priority"),
        CheckConstraint("period_mode IN ('engagement', 'as_at', 'none')", name="ck_item_period_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("request_template.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # May contain {period_start}, {period_end}, {fy_label}; filled from the engagement.
    document: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Wording the client reads (the firm maintains it in the language it uses with the client).
    label_text: Mapped[Optional[str]] = mapped_column(Text)
    condition_expr: Mapped[Optional[str]] = mapped_column(Text)  # NULL = always asked
    significance_default: Mapped[str] = mapped_column(String(10), nullable=False, default="Routine")
    priority_default: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    period_mode: Mapped[str] = mapped_column(String(12), nullable=False, default="engagement")

    template: Mapped[RequestTemplate] = relationship(back_populates="items")


class RequestSuppression(Base):
    """A template item that was NOT turned into a request because its condition was false.
    Recorded so the list can be explained rather than just shortened. Append-only."""

    __tablename__ = "request_suppression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(
        ForeignKey("engagement.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_item_id: Mapped[int] = mapped_column(ForeignKey("template_item.id", ondelete="RESTRICT"), nullable=False)
    template_label: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. "Statutory audit v1"
    document: Mapped[str] = mapped_column(String(300), nullable=False)
    condition_expr: Mapped[str] = mapped_column(Text, nullable=False)
    condition_basis: Mapped[str] = mapped_column(Text, nullable=False)
    profile_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Request(Base):
    """A durable request. Immutable: every change to it is an appended RequestEvent."""

    __tablename__ = "request"
    __table_args__ = (
        UniqueConstraint("engagement_id", "ref", name="uq_request_engagement_ref"),
        CheckConstraint("significance IN ('Material', 'Routine')", name="ck_request_significance"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="ck_request_priority"),
        CheckConstraint(
            "period_from IS NULL OR period_to IS NULL OR period_from <= period_to",
            name="ck_request_period_order",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(
        ForeignKey("engagement.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ref: Mapped[str] = mapped_column(String(30), nullable=False)
    document: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    period_from: Mapped[Optional[date]] = mapped_column(Date)
    period_to: Mapped[Optional[date]] = mapped_column(Date)
    significance: Mapped[str] = mapped_column(String(10), nullable=False, default="Routine")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    owner_person: Mapped[str] = mapped_column(String(200), nullable=False)
    template_item: Mapped[Optional[str]] = mapped_column(String(200))
    template_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("template_item.id", ondelete="RESTRICT"), index=True
    )
    condition_basis: Mapped[Optional[str]] = mapped_column(Text)
    carried_from_request_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("request.id", ondelete="RESTRICT")
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="requests")
    events: Mapped[list["RequestEvent"]] = relationship(
        back_populates="request", order_by="RequestEvent.seq", passive_deletes=True
    )
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="request", order_by="Submission.id", passive_deletes=True
    )

    # ---- derived from the event history (nothing here is stored or edited) ----
    @property
    def state(self) -> Optional[str]:
        for ev in reversed(self.events):
            state = EVENT_STATE.get(ev.event_type)
            if state:
                return state
        return None

    @property
    def is_open(self) -> bool:
        return self.state not in TERMINAL_STATES

    @property
    def is_material_open(self) -> bool:
        return self.significance == "Material" and self.is_open

    @property
    def attempt_count(self) -> int:
        return 1 + sum(1 for ev in self.events if ev.event_type == EventType.RE_REQUESTED)

    @property
    def chase_count(self) -> int:
        return sum(1 for ev in self.events if ev.event_type == EventType.CHASED)

    @property
    def sent_at(self) -> Optional[datetime]:
        for ev in self.events:
            if ev.event_type == EventType.SENT:
                return ev.at
        return None

    @property
    def reported_unavailable(self) -> Optional["RequestEvent"]:
        """The client's "I don't have this" event while it still stands: the request must be open,
        and no later submission or re-request has superseded it. None otherwise."""
        if not self.is_open:
            return None
        flag = None
        for ev in self.events:
            if ev.event_type == EventType.CLIENT_UNAVAILABLE:
                flag = ev
            elif flag is not None and ev.event_type in (EventType.SUBMITTED, EventType.RE_REQUESTED):
                flag = None
        return flag

    @property
    def last_event(self) -> Optional["RequestEvent"]:
        return self.events[-1] if self.events else None

    def age_days(self, now: Optional[datetime] = None) -> int:
        """Whole days outstanding: from release to the client (or creation if never sent) until
        now, or until the request was closed out."""
        start = self.sent_at or self.created_at
        end = now or utcnow()
        if not self.is_open and self.last_event is not None:
            end = self.last_event.at
        return max(0, (end - start).days)


class RequestEvent(Base):
    """One row per thing that happened to a request. Append-only."""

    __tablename__ = "request_event"
    __table_args__ = (
        UniqueConstraint("request_id", "seq", name="uq_request_event_request_seq"),
        CheckConstraint("seq >= 1", name="ck_request_event_seq_positive"),
        CheckConstraint(f"event_type IN ({_sql_list(ALL_EVENT_TYPES)})", name="ck_request_event_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("request.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    channel: Mapped[Optional[str]] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    request: Mapped[Request] = relationship(back_populates="events")

    @property
    def label(self) -> str:
        return EVENT_LABEL.get(self.event_type, self.event_type)


# --------------------------------------------------------------------------------------------
# Submissions: what came back against a request
# --------------------------------------------------------------------------------------------
class Submission(Base):
    """One attempt by the client. The request outlives every submission; a rejection leaves the
    submission in place with its outcome and reason, and the next attempt is a new row."""

    __tablename__ = "submission"
    __table_args__ = (
        CheckConstraint("outcome IN ('pending', 'accepted', 'rejected')", name="ck_submission_outcome"),
        UniqueConstraint("request_id", "attempt_number", name="uq_submission_request_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("request.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    submitted_by: Mapped[Optional[str]] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    reason_code: Mapped[Optional[str]] = mapped_column(String(40))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(200))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    request: Mapped[Request] = relationship(back_populates="submissions")
    files: Mapped[list["SubmissionFile"]] = relationship(
        back_populates="submission", order_by="SubmissionFile.page_order", passive_deletes=True
    )


class SubmissionFile(Base):
    """A stored file. Unique per (request, SHA-256): the same content is never stored twice for
    one request - a re-send is an Arrival instead."""

    __tablename__ = "submission_file"
    __table_args__ = (
        UniqueConstraint("request_id", "content_hash", name="uq_submission_file_request_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submission.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_id: Mapped[int] = mapped_column(
        ForeignKey("request.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256, lowercase hex
    file_bytes_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)  # relative to the uploads dir
    page_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retention_basis: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    purge_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    submission: Mapped[Submission] = relationship(back_populates="files")
    arrivals: Mapped[list["Arrival"]] = relationship(
        back_populates="file", order_by="Arrival.id", passive_deletes=True
    )


class Arrival(Base):
    """One row per RE-SEND of identical content (the first arrival is the submission_file itself).
    Kept because 'they sent it three times' is sometimes the point. Append-only."""

    __tablename__ = "arrival"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_file_id: Mapped[int] = mapped_column(
        ForeignKey("submission_file.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    submitted_by: Mapped[Optional[str]] = mapped_column(String(200))

    file: Mapped[SubmissionFile] = relationship(back_populates="arrivals")


# --------------------------------------------------------------------------------------------
# Pass 1 completion: outbound email log, chase records, tokenised client links
# --------------------------------------------------------------------------------------------
EMAIL_KINDS = ("request_batch", "client_link", "chase", "escalation")


class OutboundEmail(Base):
    """Every email the module sends (or would send). Written whether or not SMTP is configured, so
    the Outbox page always shows exactly what went to the client. Append-only."""

    __tablename__ = "outbound_email"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    engagement_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("engagement.id", ondelete="RESTRICT"), index=True
    )
    request_id: Mapped[Optional[int]] = mapped_column(ForeignKey("request.id", ondelete="RESTRICT"))
    token_id: Mapped[Optional[int]] = mapped_column(Integer)
    to_address: Mapped[str] = mapped_column(String(200), nullable=False)
    from_address: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 'logged' = stored in the local Outbox only (no SMTP configured); 'sent' = handed to the SMTP
    # server; 'failed' = SMTP refused it (the error is kept).
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)


class Chase(Base):
    """Section 9 `chase`: one row per chase alongside the append-only CHASED event. Only the
    delivery flags change after insert (false -> true); the database refuses anything else."""

    __tablename__ = "chase"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("request.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("request_event.id", ondelete="RESTRICT"), unique=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    by_whom: Mapped[str] = mapped_column("by", String(200), nullable=False)
    template: Mapped[Optional[str]] = mapped_column(String(120))
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    opened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outbound_email.id", ondelete="RESTRICT"))


class ClientToken(Base):
    """The no-login link a client opens to upload: one per engagement at a time, time-limited,
    re-sendable and revocable. Only `revoked_at` ever changes."""

    __tablename__ = "client_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(
        ForeignKey("engagement.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, default=lambda: secrets.token_urlsafe(32))
    # NULL = a link for the whole engagement; otherwise it reaches only this named client-side owner's requests.
    owner_person: Mapped[Optional[str]] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def status(self, now: Optional[datetime] = None) -> str:
        now = now or utcnow()
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return "active"


# --------------------------------------------------------------------------------------------
# Application-level append-only guards
# --------------------------------------------------------------------------------------------
@event.listens_for(RequestEvent, "before_update")
def _refuse_event_update(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation("request_event is append-only: rows cannot be updated.")


@event.listens_for(RequestEvent, "before_delete")
def _refuse_event_delete(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation("request_event is append-only: rows cannot be deleted.")


@event.listens_for(Request, "before_update")
def _refuse_request_update(mapper, connection, target):  # noqa: ANN001
    # Appending an event marks the parent "dirty" because the collection changed; only a real
    # column change is an edit.
    session = object_session(target)
    if session is not None and session.is_modified(target, include_collections=False):
        raise AppendOnlyViolation("request rows are immutable: record a new event instead.")


@event.listens_for(Request, "before_delete")
def _refuse_request_delete(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation("requests are never deleted: record a Waived / Not applicable event instead.")


# Templates and suppression records are immutable too (a change is a new template version).
def _refuse_mutation(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation(f"{type(target).__name__} rows are immutable: publish a new version instead.")


def _refuse_update_if_modified(mapper, connection, target):  # noqa: ANN001
    session = object_session(target)
    if session is not None and session.is_modified(target, include_collections=False):
        _refuse_mutation(mapper, connection, target)


for _cls in (RequestTemplate, TemplateItem, RequestSuppression, Arrival, OutboundEmail):
    event.listen(_cls, "before_update", _refuse_update_if_modified)
    event.listen(_cls, "before_delete", _refuse_mutation)
