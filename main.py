"""FastAPI app for the Document Request Ledger - Pass 1 ("The Ledger").

Run:  python main.py      (serves on http://127.0.0.1:8015)
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Query, Request as HttpRequest, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

import asyncio
import base64
import json

import emailer
import services
import whatsapp
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
    interval = int(os.getenv("WHATSAPP_FLUSH_INTERVAL", "30") or 0)
    flusher = asyncio.create_task(_whatsapp_flusher(interval)) if interval > 0 else None
    try:
        yield
    finally:
        if flusher:
            flusher.cancel()


def _flush_whatsapp_once() -> None:
    with SessionLocal() as session:
        services.flush_whatsapp_batches(session)


async def _whatsapp_flusher(interval: int) -> None:
    """Photos arrive one by one; once a client has been quiet for the assembly window, their photos
    become one submission. This wakes up every `interval` seconds to do that."""
    while True:
        await asyncio.sleep(interval)
        try:
            await run_in_threadpool(_flush_whatsapp_once)
        except Exception:  # noqa: BLE001 - never let the background task die
            pass


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
    "CLIENT_REPORTED_UNAVAILABLE": "bg-fuchsia-500",
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
    "pages": "Missing pages",
    "whatsapp_message": "WhatsApp message",
    "escalation": "Escalated to",
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

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def linkify(text: str):
    """Escape the email body, then turn URLs into clickable links (opening in a new tab)."""
    from markupsafe import Markup, escape

    out = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener" class="font-medium text-sky-700 underline break-all">{m.group(1)}</a>',
        str(escape(text or "")),
    )
    return Markup(out)


templates.env.filters["linkify"] = linkify
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
    smtp_configured=emailer.smtp_configured,
    whatsapp_mode=whatsapp.mode_label,
    whatsapp_live=whatsapp.is_live,
    can_assemble=services.can_assemble,
    link_url=emailer.client_link_url,
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


@app.exception_handler(services.TokenError)
async def token_error_handler(_: HttpRequest, exc: services.TokenError):
    code = 404 if exc.reason == "invalid" else 410
    return JSONResponse(status_code=code, content={"error": str(exc), "reason": exc.reason})


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
        {"cards": cards, "selected": selected, "requests": requests, "stats": stats, "now": utcnow(),
         "eqr": services.eqr_gate(requests, closed=selected.status == "Closed") if selected else None},
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
            "chases": services.chases_by_event(session, req.id),
            "reject_messages": {code: services.reason_message(code, req) for code in REASON_CODES},
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
    pages: str = Form(""),
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
            "pages": pages,
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
    pages: str = Form(""),
    session: Session = Depends(get_session),
):
    events = services.review_submission(
        session, submission_id, action, actor, reason_code=reason_code, note=note, pages=pages
    )
    return {"ok": True, "appended": [{"seq": ev.seq, "event_type": ev.event_type} for ev in events]}


@app.get("/submissions/{submission_id}/document.pdf")
def submission_as_one_pdf(submission_id: int, session: Session = Depends(get_session)):
    """The submission's pages, in page order, combined into ONE PDF (photos become pages)."""
    data, name = services.assemble_submission_pdf(session, submission_id)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
    return {"ok": True, "frozen": [r.ref for r in frozen], "closeout_url": f"/engagements/{engagement_id}/close-out"}


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
        "emailed": result["emailed"],
        "delivered": result["chase"].delivered,
        "no_client_email": result["no_client_email"],
        "whatsapped": result["whatsapped"],
        "no_client_whatsapp": result["no_client_whatsapp"],
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


# --------------------------------------------------------------------------------------------
# Stage 5: close-out screen and the downstream evidence outputs
# --------------------------------------------------------------------------------------------
@app.get("/engagements/{engagement_id}/close-out", response_class=HTMLResponse)
def closeout_screen(http_request: HttpRequest, engagement_id: int, session: Session = Depends(get_session)):
    """Screen 5: unfulfilled summary, EQR readiness gate, SA 705 extract, SA 580 schedule."""
    rep = services.closeout_report(session, engagement_id)
    return templates.TemplateResponse(http_request, "closeout.html", {"eng": rep["engagement"], "rep": rep})


def _closeout_doc(doc: str) -> str:
    if doc not in ("sa705", "sa580"):
        raise services.NotFoundError("Unknown close-out document.")
    return doc


@app.get("/engagements/{engagement_id}/close-out/{doc}", response_class=HTMLResponse)
def closeout_print(
    http_request: HttpRequest,
    engagement_id: int,
    doc: str,
    by: str = Query(""),
    session: Session = Depends(get_session),
):
    """Print-ready SA 705 extract or SA 580 schedule (use the browser's Save as PDF)."""
    rep = services.closeout_report(session, engagement_id)
    return templates.TemplateResponse(
        http_request,
        "closeout_print.html",
        {"eng": rep["engagement"], "rep": rep, "doc": _closeout_doc(doc), "exported_by": by.strip()},
    )


@app.get("/engagements/{engagement_id}/close-out/{doc}/download")
def closeout_download(engagement_id: int, doc: str, session: Session = Depends(get_session)):
    rep = services.closeout_report(session, engagement_id)
    ref = f"engagement-{engagement_id}"
    if _closeout_doc(doc) == "sa705":
        body, media, name = rep["sa705"]["text"], "text/plain; charset=utf-8", f"SA705-extract-{ref}.txt"
    else:
        body, media, name = services.sa580_csv(rep), "text/csv; charset=utf-8", f"SA580-schedule-{ref}.csv"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------------------------
# Pass 1 completion: client link, outbox, settings, batch release, template manager
# --------------------------------------------------------------------------------------------
NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    payload = []
    for f in files[:30]:
        data = f.file.read(services.MAX_UPLOAD_BYTES + 1)
        if not f.filename and not data:
            continue  # an empty file input
        payload.append((f.filename or "file", data))
    return payload


@app.get("/c/{token}", response_class=HTMLResponse)
def client_page(http_request: HttpRequest, token: str, session: Session = Depends(get_session)):
    """The no-login client page. Opening it acknowledges the Sent requests and marks chases opened."""
    try:
        opened = services.open_client_link(session, token)
    except services.TokenError as exc:
        resp = templates.TemplateResponse(
            http_request, "client_error.html", {"message": str(exc), "reason": exc.reason},
            status_code=404 if exc.reason == "invalid" else 410,
        )
        resp.headers.update(NO_STORE)
        return resp
    eng = opened["engagement"]
    resp = templates.TemplateResponse(
        http_request,
        "client.html",
        {"eng": eng, "token": token, "view": services.client_view(session, eng, opened["token"]),
         "expires_at": opened["token"].expires_at},
    )
    resp.headers.update(NO_STORE)
    return resp


@app.post("/c/{token}/requests/{request_id}/upload")
def client_upload(
    token: str,
    request_id: int,
    files: list[UploadFile] = File(default=[]),
    submitted_by: str = Form(""),
    session: Session = Depends(get_session),
):
    result = services.client_upload(session, token, request_id, _read_uploads(files), submitted_by)
    return JSONResponse(
        {
            "ok": True,
            "stored": len(result["new_files"]),
            "repeats": [{"filename": a["file"].filename, "count": a["count"]} for a in result["arrivals"]],
        },
        headers=NO_STORE,
    )


@app.post("/c/{token}/requests/{request_id}/unavailable")
def client_unavailable(
    token: str,
    request_id: int,
    reason: str = Form(""),
    submitted_by: str = Form(""),
    session: Session = Depends(get_session),
):
    """"I don't have this": appends a CLIENT_REPORTED_UNAVAILABLE event with the client's reason."""
    event = services.client_report_unavailable(session, token, request_id, reason, submitted_by)
    return JSONResponse({"ok": True, "seq": event.seq}, headers=NO_STORE)


@app.post("/c/{token}/requests/{request_id}/found")
def client_found(
    token: str, request_id: int, submitted_by: str = Form(""), session: Session = Depends(get_session)
):
    """"I found it" / "that was a mistake": the client takes back their "I don't have this"."""
    event = services.client_clear_unavailable(session, token, request_id, submitted_by)
    return JSONResponse({"ok": True, "seq": event.seq}, headers=NO_STORE)


@app.get("/outbox", response_class=HTMLResponse)
def outbox(http_request: HttpRequest, eng: Optional[int] = None, session: Session = Depends(get_session)):
    """Every email the module sent (or would send): open the links straight from here."""
    return templates.TemplateResponse(
        http_request,
        "outbox.html",
        {
            "emails": services.list_outbox(session, eng),
            "engagements": services.list_engagements(session),
            "filter_eng": eng,
            "smtp": emailer.smtp_configured(),
        },
    )


@app.get("/engagements/{engagement_id}/client-link", response_class=HTMLResponse)
def client_link_view(http_request: HttpRequest, engagement_id: int, session: Session = Depends(get_session)):
    eng = services.get_engagement(session, engagement_id)
    live = [
        {"token": t, "link": emailer.client_link_url(t.token)} for t in services.live_tokens(session, engagement_id)
    ]
    # whole-engagement link first, then owner-scoped links by name
    live.sort(key=lambda x: (bool(x["token"].owner_person), (x["token"].owner_person or "").lower()))
    return templates.TemplateResponse(
        http_request,
        "partials/clientlink.html",
        {"eng": eng, "live": live, "owners": services.engagement_owner_names(session, engagement_id),
         "tokens": services.list_tokens(session, engagement_id)},
    )


@app.post("/engagements/{engagement_id}/client-link/generate")
def client_link_generate(
    engagement_id: int, actor: str = Form(""), days: int = Form(services.TOKEN_DEFAULT_DAYS),
    owner: str = Form(""), session: Session = Depends(get_session),
):
    """A link for the whole engagement, or (owner given) one scoped to a named client-side owner."""
    token = services.create_client_token(session, engagement_id, actor, days=days, owner_person=owner)
    return {"ok": True, "link": emailer.client_link_url(token.token), "owner": token.owner_person}


@app.post("/engagements/{engagement_id}/client-link/resend")
def client_link_resend(
    engagement_id: int, actor: str = Form(""), token_id: int = Form(0), session: Session = Depends(get_session)
):
    mail = services.send_client_link(session, engagement_id, actor, token_id or None)
    return {"ok": True, "email_id": mail.id, "status": mail.status, "to": mail.to_address}


@app.post("/engagements/{engagement_id}/client-link/revoke")
def client_link_revoke(
    engagement_id: int, actor: str = Form(""), token_id: int = Form(...), session: Session = Depends(get_session)
):
    token = services.revoke_client_token(session, token_id, actor)
    if token.engagement_id != engagement_id:
        raise services.NotFoundError("That link is not part of this engagement.")
    return {"ok": True}


@app.get("/engagements/{engagement_id}/settings", response_class=HTMLResponse)
def settings_view(http_request: HttpRequest, engagement_id: int, session: Session = Depends(get_session)):
    eng = services.get_engagement(session, engagement_id)
    contacts = {k.lower(): v for k, v in (eng.whatsapp_contacts or {}).items()}
    owners = [{"name": n, "phone": contacts.get(n.lower(), "")} for n in services.engagement_owner_names(session, engagement_id)]
    return templates.TemplateResponse(
        http_request, "partials/settings.html",
        {"eng": eng, "smtp": emailer.smtp_configured(), "owners": owners, "wa_mode": whatsapp.mode_label()},
    )


@app.post("/engagements/{engagement_id}/settings")
def settings_save(
    engagement_id: int,
    client_email: str = Form(""),
    partner_email: str = Form(""),
    age_warn_days: str = Form(""),
    age_escalate_days: str = Form(""),
    client_whatsapp: str = Form(""),
    owner_name: list[str] = Form([]),
    owner_phone: list[str] = Form([]),
    session: Session = Depends(get_session),
):
    eng = services.update_engagement_settings(
        session, engagement_id, client_email=client_email, partner_email=partner_email,
        age_warn_days=age_warn_days, age_escalate_days=age_escalate_days,
        client_whatsapp=client_whatsapp, whatsapp_contacts=dict(zip(owner_name, owner_phone)),
    )
    return {"ok": True, "warn": eng.age_warn_days, "escalate": eng.age_escalate_days}


@app.post("/engagements/{engagement_id}/release")
def release_selected(
    engagement_id: int,
    request_id: list[int] = Form([]),
    actor: str = Form(""),
    channel: str = Form("Email"),
    session: Session = Depends(get_session),
):
    """Batch release: mark the ticked Drafted requests Sent together and send the client link, by
    email or on WhatsApp."""
    result = services.release_requests(session, engagement_id, request_id, actor, channel)
    sent = result["email"] or result["whatsapp"]
    return {
        "ok": True,
        "released": result["released"],
        "channel": result["channel"],
        "email_id": result["email"].id if result["email"] else None,
        "whatsapp_id": result["whatsapp"].id if result["whatsapp"] else None,
        "status": sent.status,
        "to": result["email"].to_address if result["email"] else result["whatsapp"].phone,
        "link": result["link"],
    }


@app.post("/engagements/{engagement_id}/escalate")
def escalate(engagement_id: int, actor: str = Form(""), session: Session = Depends(get_session)):
    result = services.escalate_to_partner(session, engagement_id, actor)
    return {"ok": True, "refs": result["refs"], "email_id": result["email"].id, "status": result["email"].status}


# --------------------------------------------------------------------------------------------
# Pass 2: WhatsApp - webhook, simulator, inbox
# --------------------------------------------------------------------------------------------
def _parse_timestamp(value) -> Optional[datetime]:
    """Provider timestamps are unix seconds (Meta) or ISO strings (generic)."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _handle_whatsapp_payload(payload: dict) -> list[dict]:
    """Turn a webhook body into receive_whatsapp() calls. Two shapes are accepted: Meta's Cloud API
    format ("entry"), and a plain JSON body (see the simulator) with
    {from, text, reply_to, timestamp, name, id, final, media: [{filename, content_type, data_base64}]}."""
    results = []
    with SessionLocal() as session:
        if "entry" in payload:
            for m in whatsapp.parse_meta_payload(payload):
                media = []
                for item in m["media"]:
                    data, mime = whatsapp.fetch_media(item["media_id"])
                    media.append((item["filename"], item["content_type"] or mime, data))
                out = services.receive_whatsapp(
                    session, phone=m["phone"], text=m["text"], media=media, reply_to=m["reply_to"],
                    provider_message_id=m["id"], received_at=_parse_timestamp(m["timestamp"]), name=m["name"],
                )
                results.append({"status": out["status"], "id": m["id"]})
        else:
            media = []
            for item in payload.get("media") or []:
                try:
                    data = base64.b64decode(item.get("data_base64", ""), validate=True)
                except ValueError as exc:
                    raise services.LedgerError("A media item is not valid base64.") from exc
                media.append((item.get("filename") or "file", item.get("content_type"), data))
            out = services.receive_whatsapp(
                session, phone=payload.get("from") or "", text=payload.get("text"), media=media,
                reply_to=payload.get("reply_to"), provider_message_id=payload.get("id"),
                received_at=_parse_timestamp(payload.get("timestamp")), name=payload.get("name"),
                final=bool(payload.get("final", bool(media))),
            )
            results.append({"status": out["status"]})
    return results


@app.get("/webhooks/whatsapp/inbound")
def whatsapp_verify(
    mode: str = Query("", alias="hub.mode"),
    verify_token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Meta's one-off webhook verification handshake."""
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    if mode == "subscribe" and expected and verify_token == expected:
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhooks/whatsapp/inbound")
async def whatsapp_inbound(http_request: HttpRequest):
    """The inbound WhatsApp receiver. With a live provider every call must carry a valid signature."""
    raw = await http_request.body()
    if whatsapp.is_live() and not whatsapp.verify_signature(raw, http_request.headers.get("x-hub-signature-256")):
        return JSONResponse(status_code=403, content={"error": "Invalid or missing webhook signature."})
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "The body is not valid JSON."})
    results = await run_in_threadpool(_handle_whatsapp_payload, payload)
    return {"ok": True, "results": results}


@app.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_screen(http_request: HttpRequest, phone: Optional[str] = None, session: Session = Depends(get_session)):
    """The WhatsApp screen: every conversation, the messages in it, and (in simulator mode) a panel
    to play the client."""
    threads = services.whatsapp_threads(session)
    chosen = whatsapp.normalise_phone(phone) if phone else (threads[0]["phone"] if threads else None)
    conversation = services.whatsapp_conversation(session, chosen) if chosen else []
    ids = {m.request_id for m in conversation if m.request_id}
    refs = {rid: ref for rid, ref in session.execute(
        select(services.Request.id, services.Request.ref).where(services.Request.id.in_(ids)))} if ids else {}
    return templates.TemplateResponse(
        http_request,
        "whatsapp.html",
        {
            "threads": threads,
            "phone": chosen,
            "conversation": conversation,
            "refs": refs,
            "known": services.whatsapp_known_numbers(session),
            "open_requests": services.whatsapp_open_requests(session, chosen) if chosen else [],
            "simulator": not whatsapp.is_live(),
            "wa_mode": whatsapp.mode_label(),
            "assembly_seconds": services.WHATSAPP_ASSEMBLY_SECONDS,
        },
    )


@app.post("/whatsapp/simulate")
def whatsapp_simulate(
    phone: str = Form(...),
    text: str = Form(""),
    reply_to: str = Form(""),
    action: str = Form(""),
    separate: str = Form(""),
    name: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    """Play the client: send a WhatsApp reply (text, photos / PDFs, or "I don't have this")."""
    if whatsapp.is_live():
        return JSONResponse(status_code=403, content={"error": "The simulator is off while a live WhatsApp provider is configured."})
    media = [(f.filename or "file", f.content_type, data) for f, data in
             ((f, f.file.read(services.MAX_UPLOAD_BYTES + 1)) for f in files[:30]) if f.filename or data]
    if action == "unavailable":
        text = f"{text} I don't have this".strip()
    results = []
    if separate and len(media) > 1:
        # one WhatsApp message per photo, like a real phone: they are assembled later, in order
        for i, item in enumerate(media):
            out = services.receive_whatsapp(
                session, phone=phone, text=text if i == 0 else None, media=[item], reply_to=reply_to or None,
                name=name or None, final=False,
            )
            results.append(out["status"])
    else:
        out = services.receive_whatsapp(
            session, phone=phone, text=text or None, media=media, reply_to=reply_to or None,
            name=name or None, final=True,
        )
        results.append(out["status"])
    return {"ok": True, "statuses": results}


@app.post("/whatsapp/flush")
def whatsapp_flush(session: Session = Depends(get_session)):
    """Assemble every buffered WhatsApp photo run into submissions now (instead of waiting)."""
    done = services.flush_whatsapp_batches(session, force=True)
    return {"ok": True, "assembled": done}


@app.post("/whatsapp/messages/{message_id}/assign")
def whatsapp_assign(
    message_id: int, request_id: int = Form(...), actor: str = Form(""), session: Session = Depends(get_session)
):
    out = services.assign_whatsapp_message(session, message_id, request_id, actor)
    return {"ok": True, "status": out["status"]}


@app.get("/whatsapp/media/{message_id}/{index}")
def whatsapp_media(message_id: int, index: int, inline: int = 0, session: Session = Depends(get_session)):
    msg = session.get(services.WhatsAppMessage, message_id)
    if msg is None or not 0 <= index < len(msg.media or []):
        raise services.NotFoundError("That attachment does not exist.")
    item = msg.media[index]
    root = services.UPLOAD_DIR.resolve()
    path = (root / item["path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise services.NotFoundError("The stored file is missing.")
    if inline and item.get("content_type") in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        # raster photos only (never SVG/HTML), sandboxed, so a thumbnail can show in the thread
        return FileResponse(path, media_type=item["content_type"], content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
    return FileResponse(path, filename=item["filename"], media_type="application/octet-stream",
                        content_disposition_type="attachment")


@app.get("/templates/manager", response_class=HTMLResponse)
def template_manager(
    http_request: HttpRequest,
    etype: Optional[str] = Query(None, alias="type"),
    version: Optional[int] = None,
    session: Session = Depends(get_session),
):
    chosen = etype if etype in ENGAGEMENT_TYPES else ENGAGEMENT_TYPES[0]
    versions = services.template_versions(session, chosen)
    if not versions:
        raise services.NotFoundError(f"No template for '{chosen}'.")
    shown = next((t for t in versions if t.version == version), versions[0])
    return templates.TemplateResponse(
        http_request,
        "partials/templates_manager.html",
        {"chosen": chosen, "versions": versions, "shown": shown, "latest": versions[0]},
    )


@app.post("/templates/publish")
def template_publish(
    etype: str = Form(..., alias="type"),
    actor: str = Form(""),
    note: str = Form(""),
    item_id: list[int] = Form([]),
    label: list[str] = Form([]),
    significance: list[str] = Form([]),
    priority: list[str] = Form([]),
    condition: list[str] = Form([]),
    remove: list[int] = Form([]),
    add_document: list[str] = Form([]),
    add_label: list[str] = Form([]),
    add_significance: list[str] = Form([]),
    add_priority: list[str] = Form([]),
    add_condition: list[str] = Form([]),
    add_period: list[str] = Form([]),
    session: Session = Depends(get_session),
):
    """Publish the next version of a template: wording, significance, priority, conditions, removed
    and added items. The parallel lists carry one entry per item, in the same order."""
    n = len(item_id)
    if not (len(label) == n and len(significance) in (0, n) and len(priority) in (0, n) and len(condition) in (0, n)):
        raise services.LedgerError("The form was incomplete - reload the template manager and try again.")
    item_edits = {}
    for i, iid in enumerate(item_id):
        edits = {}
        if significance:
            edits["significance"] = significance[i]
        if priority:
            edits["priority"] = priority[i]
        if condition:
            edits["condition"] = condition[i]
        item_edits[iid] = edits
    additions = [
        {"document": add_document[i], "label": (add_label + [""] * 99)[i],
         "significance": (add_significance + [""] * 99)[i], "priority": (add_priority + [""] * 99)[i],
         "condition": (add_condition + [""] * 99)[i], "period": (add_period + [""] * 99)[i]}
        for i in range(len(add_document))
    ]
    tmpl = services.publish_template_version(
        session, etype, actor, dict(zip(item_id, label)), note,
        item_edits=item_edits, removals=set(remove), additions=additions,
    )
    return {"ok": True, "version": tmpl.version, "type": tmpl.engagement_type, "items": len(tmpl.items)}


@app.get("/api/engagements/{engagement_id}/eqr-readiness")
def api_eqr_readiness(engagement_id: int, session: Session = Depends(get_session)):
    """The hand-off to EQR Readiness: an open (or, once closed, unfulfilled) request that is Material
    to the opinion is a BLOCKER, not a line in an admin list."""
    eng = services.get_engagement(session, engagement_id)
    requests = services.engagement_requests(session, engagement_id)
    gate = services.eqr_gate(requests, closed=eng.status == "Closed")
    now = utcnow()
    return {
        "engagement": {"id": eng.id, "name": eng.name, "status": eng.status},
        "passed": gate["passed"],
        "blocked": gate["blocked"],
        "message": gate["message"],
        "blockers": [
            {"ref": r.ref, "document": r.document, "owner": r.owner_person, "state": r.state,
             "age_days": r.age_days(now), "chases": r.chase_count, "attempts": r.attempt_count}
            for r in gate["requests"]
        ],
    }


@app.get("/api/engagements/{engagement_id}/unfulfilled")
def api_unfulfilled(engagement_id: int, session: Session = Depends(get_session)):
    """The frozen list of requests unfulfilled at the report date - the input to the opinion (SA 705)
    and written-representation (SA 580) modules. Before close-out it is a preview."""
    rep = services.closeout_report(session, engagement_id)
    eng = rep["engagement"]
    return {
        "engagement": {"id": eng.id, "name": eng.name, "status": eng.status},
        "frozen": rep["closed"],
        "report_date": rep["report_date"].isoformat() if rep["report_date"] else None,
        "closed_at": eng.closed_at.isoformat() if eng.closed_at else None,
        "counts": rep["counts"],
        "effects": rep["effects"],
        "items": [
            {"ref": x["ref"], "document": x["document"], "period": x["period"], "owner": x["owner"],
             "significance": x["significance"], "chases": x["chases"], "attempts": x["attempts"],
             "requested_on": x["requested_on"].isoformat(), "state_when_frozen": x["frozen_from"],
             "effect": x["effect"], "effect_label": x["effect_label"]}
            for x in rep["rows"]
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
