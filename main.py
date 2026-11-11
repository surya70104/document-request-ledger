"""FastAPI app for the Document Request Ledger - Pass 1 ("The Ledger").

Run:  python main.py      (serves on http://127.0.0.1:8015)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Query, Request as HttpRequest, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import services
from database import SessionLocal, ensure_database, init_db
from conditions import CRORE, PROFILE_FIELDS, ConditionError
from models import CHANNELS, ENGAGEMENT_TYPES, EVENT_LABEL, REASON_CODES, SIGNIFICANCE, utcnow

BASE_DIR = Path(__file__).resolve().parent
HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "8015"))
FIRM_NAME = os.getenv("FIRM_NAME", "OAK Audit")
IST = timezone(timedelta(hours=5, minutes=30), "IST")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_database()
    init_db()
    services.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Document Request Ledger - OAK Audit", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------------------------
STATE_STYLES = {
    "Drafted": "bg-slate-100 text-slate-700 ring-slate-300",
    "Sent": "bg-sky-50 text-sky-800 ring-sky-300",
    "Acknowledged": "bg-cyan-50 text-cyan-800 ring-cyan-300",
    "Submitted": "bg-indigo-50 text-indigo-800 ring-indigo-300",
    "Under review": "bg-violet-50 text-violet-800 ring-violet-300",
    "Accepted": "bg-emerald-50 text-emerald-800 ring-emerald-300",
    "Rejected": "bg-rose-50 text-rose-800 ring-rose-300",
    "Re-requested": "bg-amber-50 text-amber-800 ring-amber-300",
    "Waived": "bg-stone-100 text-stone-700 ring-stone-300",
    "Not applicable": "bg-stone-100 text-stone-600 ring-stone-300",
    "Unfulfilled at report date": "bg-red-100 text-red-800 ring-red-400",
}
EVENT_DOT = {
    "DRAFTED": "bg-slate-400",
    "SENT": "bg-sky-500",
    "ACKNOWLEDGED": "bg-cyan-500",
    "SUBMITTED": "bg-indigo-500",
    "UNDER_REVIEW": "bg-violet-500",
    "ACCEPTED": "bg-emerald-500",
    "REJECTED": "bg-rose-500",
    "RE_REQUESTED": "bg-amber-500",
    "WAIVED": "bg-stone-500",
    "NOT_APPLICABLE": "bg-stone-400",
    "UNFULFILLED_AT_REPORT_DATE": "bg-red-600",
    "CHASED": "bg-orange-500",
    "NOTE": "bg-slate-300",
}
PAYLOAD_LABELS = {
    "reason_label": "Reason",
    "client_message": "Told to client",
    "detail": "Detail",
    "reason": "Reason",
    "note": "Note",
    "submitted_by": "Submitted by",
    "filename": "File",
    "attempt": "Attempt",
    "prior_rejection_seq": "Follows rejection at event",
    "frozen_from_state": "Frozen from state",
    "report_date": "Report date",
    "attempts": "Attempts so far",
    "chases": "Chases so far",
    "significance": "Significance",
    "priority": "Priority",
    "owner_person": "Client owner",
    "document": "Document",
    "submission_id": "Submission",
    "files": "Files",
    "filename": "File",
    "sha256": "SHA-256",
    "arrival_number": "Arrival no.",
    "message": "Detail",
    "template_item": "Template item",
    "condition_basis": "Condition basis",
    "template": "Chase template",
    "carried_forward": "Carried forward",
}
HIDDEN_PAYLOAD_KEYS = {"reason_code", "automatic"}


def to_ist(dt: Optional[datetime], fmt: str = "%d %b %Y, %H:%M") -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt) + " IST"


def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d %b %Y") if d else "-"


def period_text(r) -> str:
    """Accepts a Request or a dict with period_from / period_to."""
    get = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k))
    pf, pt = get("period_from"), get("period_to")
    if pf and pt:
        return f"{fmt_date(pf)} - {fmt_date(pt)}"
    if pf:
        return f"from {fmt_date(pf)}"
    if pt:
        return f"as at {fmt_date(pt)}"
    return "-"


def payload_items(ev) -> list[tuple[str, str]]:
    items = []
    for key, value in (ev.payload or {}).items():
        if key in HIDDEN_PAYLOAD_KEYS or value in (None, ""):
            continue
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(v) for v in value)
        items.append((PAYLOAD_LABELS.get(key, key.replace("_", " ").capitalize()), str(value)))
    return items


templates.env.filters["ist"] = to_ist
templates.env.filters["fdate"] = fmt_date


def inr_cr(value) -> str:
    """Turnover in INR -> 'Rs 48.00 Cr'."""
    return f"Rs {float(value or 0) / CRORE:,.2f} Cr"


templates.env.filters["inr_cr"] = inr_cr


def fsize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


templates.env.filters["fsize"] = fsize
templates.env.globals["pending_submission"] = services.pending_submission
templates.env.globals.update(
    state_style=lambda s: STATE_STYLES.get(s, "bg-slate-100 text-slate-700 ring-slate-300"),
    event_dot=lambda t: EVENT_DOT.get(t, "bg-slate-400"),
    payload_items=payload_items,
    period_text=period_text,
    channels=CHANNELS,
    reason_codes=REASON_CODES,
    significance_options=SIGNIFICANCE,
    event_labels=EVENT_LABEL,
    age_band=services.age_band,
    chase_channels=services.CHASE_CHANNELS,
    chase_templates=services.CHASE_TEMPLATES,
    chaseable_states=services.CHASEABLE_STATES,
    age_warn=services.AGE_WARN_DAYS,
    age_crit=services.AGE_CRIT_DAYS,
    firm_name=FIRM_NAME,
    profile_fields=PROFILE_FIELDS,
    engagement_types=ENGAGEMENT_TYPES,
)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _parse_date(value: Optional[str], label: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise services.LedgerError(f"{label} is not a valid date.") from exc


# --------------------------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------------------------
@app.exception_handler(services.DuplicateRequestError)
async def duplicate_handler(_: HttpRequest, exc: services.DuplicateRequestError):
    ex = exc.existing
    return JSONResponse(
        status_code=409,
        content={
            "error": str(exc),
            "duplicate": True,
            "existing": {
                "id": ex.id,
                "ref": ex.ref,
                "document": ex.document,
                "state": ex.state,
                "owner_person": ex.owner_person,
            },
        },
    )


@app.exception_handler(services.NotFoundError)
async def not_found_handler(_: HttpRequest, exc: services.NotFoundError):
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(ConditionError)
async def condition_error_handler(_: HttpRequest, exc: ConditionError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(services.LedgerError)
async def ledger_error_handler(_: HttpRequest, exc: services.LedgerError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


# --------------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(http_request: HttpRequest, eng: Optional[int] = None, session: Session = Depends(get_session)):
    engagements = services.list_engagements(session)
    cards = []
    for e in engagements:
        cards.append({"engagement": e, "stats": services.engagement_stats(services.engagement_requests(session, e.id))})

    selected = None
    if engagements:
        selected = next((e for e in engagements if e.id == eng), engagements[0])

    requests, stats = [], None
    if selected is not None:
        requests = services.engagement_requests(session, selected.id)
        stats = services.engagement_stats(requests)
        # Material-and-open requests are pinned to the top; then by priority, then oldest first.
        now = utcnow()
        requests.sort(key=lambda r: (not r.is_material_open, not r.is_open, r.priority, -r.age_days(now)))

    return templates.TemplateResponse(
        http_request,
        "dashboard.html",
        {"cards": cards, "selected": selected, "requests": requests, "stats": stats, "now": utcnow()},
    )


@app.get("/requests/{request_id}/trail", response_class=HTMLResponse)
def request_trail(http_request: HttpRequest, request_id: int, session: Session = Depends(get_session)):
    """Audit-trail drawer content: the request's full sequential history."""
    req = services.get_request(session, request_id)
    carried = services.carried_from_request(session, req)
    drafted = next((ev for ev in req.events if ev.event_type == "DRAFTED"), None)
    return templates.TemplateResponse(
        http_request,
        "partials/trail.html",
        {
            "r": req,
            "actions": services.available_actions(req),
            "now": utcnow(),
            "carried_from": carried,
            "carried_note": (drafted.payload or {}).get("carried_forward") if drafted else None,
        },
    )


@app.get("/api/requests/{request_id}/events")
def request_events_json(request_id: int, session: Session = Depends(get_session)):
    """The same history as JSON (exportable evidence of what was asked, when and how)."""
    req = services.get_request(session, request_id)
    return {
        "ref": req.ref,
        "document": req.document,
        "state": req.state,
        "attempt_count": req.attempt_count,
        "events": [
            {
                "seq": ev.seq,
                "event_type": ev.event_type,
                "actor": ev.actor,
                "at": ev.at.isoformat(),
                "channel": ev.channel,
                "payload": ev.payload,
            }
            for ev in req.events
        ],
    }


# --------------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------------
@app.post("/requests")
def create_request(
    engagement_id: int = Form(...),
    document: str = Form(...),
    owner_person: str = Form(...),
    actor: str = Form(""),
    significance: str = Form("Routine"),
    priority: int = Form(3),
    description: str = Form(""),
    period_from: str = Form(""),
    period_to: str = Form(""),
    session: Session = Depends(get_session),
):
    req = services.create_request(
        session,
        engagement_id=engagement_id,
        document=document,
        owner_person=owner_person,
        actor=actor,
        significance=significance,
        priority=priority,
        description=description,
        period_from=_parse_date(period_from, "Period from"),
        period_to=_parse_date(period_to, "Period to"),
        channel="Manual",
    )
    return {"ok": True, "id": req.id, "ref": req.ref}


@app.post("/requests/{request_id}/events")
def add_event(
    request_id: int,
    event_type: str = Form(...),
    actor: str = Form(""),
    channel: str = Form(""),
    reason_code: str = Form(""),
    reason: str = Form(""),
    detail: str = Form(""),
    submitted_by: str = Form(""),
    filename: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    appended = services.append_event(
        session,
        request_id,
        event_type,
        actor,
        channel=channel,
        payload={
            "reason_code": reason_code,
            "reason": reason,
            "detail": detail,
            "submitted_by": submitted_by,
            "filename": filename,
            "note": note,
        },
    )
    return {"ok": True, "appended": [{"seq": ev.seq, "event_type": ev.event_type} for ev in appended]}


# --------------------------------------------------------------------------------------------
# Stage 3: submissions, content hashing, review
# --------------------------------------------------------------------------------------------
@app.post("/requests/{request_id}/submissions")
def submit_document(
    request_id: int,
    files: list[UploadFile] = File(default=[]),
    actor: str = Form(""),
    channel: str = Form("Web upload"),
    submitted_by: str = Form(""),
    session: Session = Depends(get_session),
):
    """Upload one or more files (pages, in order) against a request. Identical content already held
    for this request is recorded as an arrival instead of a new file."""
    payload = []
    for f in files:
        data = f.file.read(services.MAX_UPLOAD_BYTES + 1)
        if not f.filename and not data:
            continue  # an empty file input
        payload.append((f.filename or "file", data))
    result = services.record_submission(
        session, request_id, actor, payload, channel=channel, submitted_by=submitted_by
    )
    sub = result["submission"]
    return {
        "ok": True,
        "submission_id": sub.id if sub else None,
        "new_files": [{"filename": f.filename, "sha256": f.content_hash} for f in result["new_files"]],
        "arrivals": [{"filename": a["file"].filename, "count": a["count"]} for a in result["arrivals"]],
    }


@app.post("/submissions/{submission_id}/review")
def review_submission(
    submission_id: int,
    action: str = Form(...),
    actor: str = Form(""),
    reason_code: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    events = services.review_submission(
        session, submission_id, action, actor, reason_code=reason_code, note=note
    )
    return {"ok": True, "appended": [{"seq": ev.seq, "event_type": ev.event_type} for ev in events]}


@app.get("/files/{file_id}")
def download_file(file_id: int, session: Session = Depends(get_session)):
    sf = services.get_submission_file(session, file_id)
    root = services.UPLOAD_DIR.resolve()
    path = (root / sf.storage_path).resolve()
    if root not in path.parents or not path.is_file():
        raise services.NotFoundError("The stored file is missing.")
    # Always a download, never rendered in the browser (uploads are untrusted content).
    return FileResponse(
        path, filename=sf.filename, media_type="application/octet-stream", content_disposition_type="attachment"
    )


@app.post("/engagements/{engagement_id}/close")
def close_engagement(
    engagement_id: int,
    actor: str = Form(""),
    report_date: str = Form(""),
    session: Session = Depends(get_session),
):
    frozen = services.close_engagement(
        session, engagement_id, actor, report_date=_parse_date(report_date, "Report date")
    )
    return {"ok": True, "frozen": [r.ref for r in frozen]}


# --------------------------------------------------------------------------------------------
# Stage 2: requirement lists, templates, entity profile
# --------------------------------------------------------------------------------------------
def _template_type(requested: Optional[str], eng) -> str:
    if requested in ENGAGEMENT_TYPES:
        return requested
    return eng.engagement_type if eng.engagement_type in ENGAGEMENT_TYPES else ENGAGEMENT_TYPES[0]


def _profile_from_params(params) -> dict:
    """Build a profile dict from form/query parameters (checkbox present = true; turnover in crore)."""
    raw = {}
    for field in PROFILE_FIELDS:
        key = field["key"]
        if field["kind"] == "bool":
            raw[key] = str(params.get(key, "")).strip().lower() not in ("", "0", "false", "off")
        else:
            value = str(params.get("turnover_cr", "") or "0").strip()
            try:
                raw[key] = round(float(value) * CRORE)
            except ValueError as exc:
                raise services.LedgerError("Turnover must be a number (INR crore).") from exc
    return raw


@app.get("/engagements/{engagement_id}/requirements", response_class=HTMLResponse)
def requirements_view(
    http_request: HttpRequest,
    engagement_id: int,
    etype: Optional[str] = Query(None, alias="type"),
    carry: int = Query(0),
    session: Session = Depends(get_session),
):
    """Requirement-list builder: template preview, already-held items, suppressed items."""
    eng = services.get_engagement(session, engagement_id)
    chosen = _template_type(etype, eng)
    return templates.TemplateResponse(
        http_request,
        "partials/requirements.html",
        {
            "eng": eng,
            "chosen": chosen,
            "plan": services.build_requirement_list(session, engagement_id, chosen, carry_forward=bool(carry)),
            "prior": services.find_prior_engagement(session, eng),
            "recorded": services.recorded_suppressions(session, engagement_id),
        },
    )


@app.get("/engagements/{engagement_id}/profile", response_class=HTMLResponse)
def profile_view(
    http_request: HttpRequest,
    engagement_id: int,
    etype: Optional[str] = Query(None, alias="type"),
    session: Session = Depends(get_session),
):
    eng = services.get_engagement(session, engagement_id)
    chosen = _template_type(etype, eng)
    plan = services.build_requirement_list(session, engagement_id, chosen)
    return templates.TemplateResponse(
        http_request,
        "partials/profile.html",
        {"eng": eng, "chosen": chosen, "plan": plan, "profile": plan["profile"]},
    )


@app.get("/engagements/{engagement_id}/profile/impact", response_class=HTMLResponse)
def profile_impact(
    http_request: HttpRequest,
    engagement_id: int,
    etype: Optional[str] = Query(None, alias="type"),
    session: Session = Depends(get_session),
):
    """What-if: evaluate the template against the unsaved profile in the query string."""
    eng = services.get_engagement(session, engagement_id)
    chosen = _template_type(etype, eng)
    plan = services.build_requirement_list(
        session, engagement_id, chosen, profile_override=_profile_from_params(http_request.query_params)
    )
    return templates.TemplateResponse(http_request, "partials/impact.html", {"plan": plan, "chosen": chosen})


@app.post("/engagements/{engagement_id}/profile")
async def save_profile(http_request: HttpRequest, engagement_id: int, session: Session = Depends(get_session)):
    form = await http_request.form()
    profile = services.update_profile(session, engagement_id, _profile_from_params(form))
    return {"ok": True, "profile": profile}


@app.post("/engagements/{engagement_id}/requirements/generate")
def generate_from_template(
    engagement_id: int,
    etype: str = Form(..., alias="type"),
    actor: str = Form(""),
    default_owner: str = Form(""),
    item_id: list[int] = Form([]),
    owner: list[str] = Form([]),
    significance: list[str] = Form([]),
    priority: list[int] = Form([]),
    include: list[int] = Form([]),
    carry_forward: int = Form(0),
    session: Session = Depends(get_session),
):
    if not (len(item_id) == len(owner) == len(significance) == len(priority)):
        raise services.LedgerError("The form was incomplete - reload the builder and try again.")
    selections = {
        iid: {
            "include": iid in include,
            "owner_person": owner[i],
            "significance": significance[i],
            "priority": priority[i],
        }
        for i, iid in enumerate(item_id)
    }
    result = services.generate_requests(
        session,
        engagement_id=engagement_id,
        engagement_type=etype,
        actor=actor,
        default_owner=default_owner,
        selections=selections,
        carry_forward=bool(carry_forward),
    )
    return {"ok": True, **result}


# --------------------------------------------------------------------------------------------
# Stage 4: Outstanding Board, inline chase, exportable audit thread
# --------------------------------------------------------------------------------------------
@app.get("/engagements/{engagement_id}/board", response_class=HTMLResponse)
def outstanding_board(
    http_request: HttpRequest,
    engagement_id: int,
    cut: str = Query("age"),
    session: Session = Depends(get_session),
):
    """The Outstanding Board: what the client still owes, in one of four cuts."""
    board = services.outstanding_board(session, engagement_id, cut)
    return templates.TemplateResponse(http_request, "partials/board.html", {"b": board})


@app.post("/requests/{request_id}/chase")
def chase_request(
    request_id: int,
    actor: str = Form(""),
    channel: str = Form(""),
    template: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Record a chase straight from the board or the drawer: appends a CHASED event."""
    result = services.record_chase(session, request_id, actor, channel, template=template, note=note)
    ev = result["event"]
    return {
        "ok": True,
        "request_id": request_id,
        "seq": ev.seq,
        "chase_count": result["chase_count"],
        "note": (ev.payload or {}).get("note"),
    }


@app.get("/engagements/{engagement_id}/requests/{ref}/export", response_class=HTMLResponse)
def export_thread(
    http_request: HttpRequest,
    engagement_id: int,
    ref: str,
    by: str = Query(""),
    session: Session = Depends(get_session),
):
    """The screen you export when the argument starts: header, full timeline, submissions, hashes.
    A standalone, print-ready page (Save as PDF from the browser's print dialog)."""
    ctx = services.export_context(session, engagement_id, ref)
    return templates.TemplateResponse(http_request, "export.html", {**ctx, "exported_by": by.strip()})


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
