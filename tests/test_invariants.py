"""Acceptance tests for the ledger's invariants (spec section 12).

Each test builds its own engagement, so the tests are independent and order-free.
"""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import services
from models import AppendOnlyViolation, Arrival, RequestEvent, RequestSuppression


def states(session, eng):
    session.expire_all()  # other sessions (the app under test) may have appended events
    return {r.ref: r.state for r in services.engagement_requests(session, eng.id)}


# --------------------------------------------------------------------------------------------
# Test 1 - the ledger is append-only
# --------------------------------------------------------------------------------------------
def test_1_append_only_orm_and_sql(session, engine_db, make_engagement, make_request):
    eng = make_engagement()
    req = make_request(eng)
    req_id = req.id  # plain ints survive rollback() without re-querying
    ev = session.execute(select(RequestEvent).where(RequestEvent.request_id == req_id)).scalars().first()

    # ORM update is refused
    ev.actor = "somebody else"
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    # ORM delete is refused
    ev = session.execute(select(RequestEvent).where(RequestEvent.request_id == req_id)).scalars().first()
    session.delete(ev)
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    # Raw SQL cannot rewrite history either: the database itself refuses.
    session.rollback()  # release this session's read locks so TRUNCATE can be attempted
    for statement in (
        "UPDATE request_event SET actor = 'x' WHERE request_id = :i",
        "DELETE FROM request_event WHERE request_id = :i",
        "TRUNCATE request_event",
        "UPDATE request SET document = 'x' WHERE id = :i",
        "DELETE FROM request WHERE id = :i",
    ):
        with pytest.raises(DBAPIError):
            with engine_db.begin() as conn:
                conn.execute(text(statement), {"i": req_id} if ":i" in statement else {})

    events = services.get_request(session, req_id).events
    assert [e.event_type for e in events] == ["DRAFTED", "SENT"]
    assert [e.seq for e in events] == [1, 2]


# --------------------------------------------------------------------------------------------
# Test 2 - close-out freezes open requests; nothing is deleted or auto-waived
# --------------------------------------------------------------------------------------------
def test_2_closeout_freeze(session, engine_db, make_engagement, make_request):
    eng = make_engagement()
    drafted = make_request(eng, "Drafted doc", send=False)
    sent = make_request(eng, "Sent doc")
    acked = make_request(eng, "Acknowledged doc")
    services.append_event(session, acked.id, "ACKNOWLEDGED", "Tester", channel="WhatsApp")
    rerequested = make_request(eng, "Re-requested doc", significance="Material")
    sub = services.record_submission(session, rerequested.id, "Client", [("a.pdf", b"first try")])
    services.review_submission(session, sub["submission"].id, "reject", "Reviewer", reason_code="UNSIGNED")
    in_review = make_request(eng, "Under review doc")
    services.record_submission(session, in_review.id, "Client", [("b.pdf", b"awaiting review")])
    accepted = make_request(eng, "Accepted doc")
    sub = services.record_submission(session, accepted.id, "Client", [("c.pdf", b"good")])
    services.review_submission(session, sub["submission"].id, "accept", "Reviewer")
    waived = make_request(eng, "Waived doc")
    services.append_event(session, waived.id, "WAIVED", "Partner", payload={"reason": "Not needed"})
    na = make_request(eng, "N/A doc")
    services.append_event(session, na.id, "NOT_APPLICABLE", "Partner", payload={"reason": "No such account"})

    ids = {"eng": eng.id, "sent": sent.id}
    before = states(session, eng)
    assert before[rerequested.ref] == "Re-requested" and before[drafted.ref] == "Drafted"
    total = len(before)
    waived_events_before = session.execute(
        select(RequestEvent).where(RequestEvent.event_type == "WAIVED", RequestEvent.request_id.in_(
            [r.id for r in services.engagement_requests(session, eng.id)]))
    ).scalars().all()

    frozen = services.close_engagement(session, eng.id, "Partner")

    after = states(session, eng)
    assert len(after) == total, "nothing is deleted"
    for ref in (drafted.ref, sent.ref, acked.ref, rerequested.ref, in_review.ref):
        assert after[ref] == "Unfulfilled at report date", ref
    assert after[accepted.ref] == "Accepted"
    assert after[waived.ref] == "Waived"
    assert after[na.ref] == "Not applicable"
    assert len(frozen) == 5

    waived_events_after = session.execute(
        select(RequestEvent).where(RequestEvent.event_type == "WAIVED", RequestEvent.request_id.in_(
            [r.id for r in services.engagement_requests(session, eng.id)]))
    ).scalars().all()
    assert len(waived_events_after) == len(waived_events_before) == 1, "no automatic waivers"

    for r in services.engagement_requests(session, eng.id):
        if r.state == "Unfulfilled at report date":
            assert r.events[-1].event_type == "UNFULFILLED_AT_REPORT_DATE"
            assert r.events[-1].payload["frozen_from_state"]
    session.refresh(eng)
    assert eng.status == "Closed" and eng.closed_at is not None and eng.report_date is not None

    # ---- strictly read-only from now on ----
    session.rollback()
    with pytest.raises(services.LedgerError):
        services.create_request(session, engagement_id=eng.id, document="New", owner_person="O", actor="T")
    with pytest.raises(services.LedgerError):
        services.append_event(session, sent.id, "NOTE", "T", payload={"note": "late thought"})
    with pytest.raises(services.LedgerError):
        services.record_chase(session, sent.id, "T", "Email", note="hello?")
    with pytest.raises(services.LedgerError):
        services.record_submission(session, sent.id, "Client", [("late.pdf", b"too late")])
    with pytest.raises(services.LedgerError):  # not even an identical re-send
        services.record_submission(session, accepted.id, "Client", [("c.pdf", b"good")])
    with pytest.raises(services.LedgerError):
        services.close_engagement(session, eng.id, "Partner")
    assert services.available_actions(services.get_request(session, sent.id)) == []

    # ... and the database agrees, whatever the application does
    session.rollback()
    with pytest.raises(DBAPIError):
        with engine_db.begin() as conn:
            conn.execute(text(
                "INSERT INTO request_event (request_id, seq, event_type, actor, at, payload) "
                "VALUES (:i, 99, 'NOTE', 'x', now(), CAST('{}' AS jsonb))"), {"i": ids["sent"]})
    with pytest.raises(DBAPIError):
        with engine_db.begin() as conn:
            conn.execute(text("UPDATE engagement SET status = 'Active' WHERE id = :i"), {"i": ids["eng"]})
    with pytest.raises(DBAPIError):
        with engine_db.begin() as conn:
            conn.execute(text("DELETE FROM engagement WHERE id = :i"), {"i": ids["eng"]})


# --------------------------------------------------------------------------------------------
# Test 3 - rejection cycle
# --------------------------------------------------------------------------------------------
def test_3_rejection_cycle(session, make_engagement, make_request):
    eng = make_engagement()
    req = make_request(eng, "Bank statements")
    assert services.get_request(session, req.id).attempt_count == 1

    sub = services.record_submission(session, req.id, "Client", [("s.pdf", b"wrong period")])["submission"]
    events = services.review_submission(
        session, sub.id, "reject", "Reviewer", reason_code="WRONG_PERIOD", note="Need FY 25-26")

    assert [e.event_type for e in events] == ["REJECTED", "RE_REQUESTED"]
    assert events[1].seq == events[0].seq + 1
    assert events[0].payload["reason_code"] == "WRONG_PERIOD"
    assert events[1].payload["attempt"] == 2 and events[1].payload["prior_rejection_seq"] == events[0].seq

    req = services.get_request(session, req.id)
    assert req.attempt_count == 2
    assert req.state == "Re-requested" and req.is_open, "the request stays live"
    assert req.submissions[0].outcome == "rejected" and req.submissions[0].reason_code == "WRONG_PERIOD"

    # a code outside the eight is refused, and a reviewed submission cannot be reviewed twice
    sub2 = services.record_submission(session, req.id, "Client", [("s2.pdf", b"second try")])["submission"]
    with pytest.raises(services.LedgerError):
        services.review_submission(session, sub2.id, "reject", "Reviewer", reason_code="BECAUSE")
    with pytest.raises(services.LedgerError):
        services.review_submission(session, sub.id, "accept", "Reviewer")
    assert services.get_request(session, req.id).attempt_count == 2


# --------------------------------------------------------------------------------------------
# Test 4 - content-hash de-duplication
# --------------------------------------------------------------------------------------------
def test_4_content_hash_dedup(session, make_engagement, make_request):
    eng = make_engagement()
    req = make_request(eng, "Trial balance")
    data = b"%PDF-1.4 identical bytes"

    first = services.record_submission(session, req.id, "Client", [("tb.pdf", data)], channel="Email")
    again = services.record_submission(session, req.id, "Client", [("renamed_copy.pdf", data)], channel="WhatsApp")

    req = services.get_request(session, req.id)
    assert len(req.submissions) == 1, "one submission"
    files = [f for sub in req.submissions for f in sub.files]
    assert len(files) == 1, "one stored file"
    assert again["submission"] is None and not again["new_files"]
    # arrivals of that content: the original receipt + the re-send
    assert 1 + len(files[0].arrivals) == 2
    assert again["arrivals"][0]["count"] == 2
    assert files[0].content_hash == services.sha256_hex(data)
    assert [e.event_type for e in req.events].count("ARRIVAL_RECORDED") == 1
    assert [e.event_type for e in req.events].count("SUBMITTED") == 1
    assert first["submission"].id == req.submissions[0].id

    # the same content inside ONE batch is also stored once
    req2 = make_request(eng, "Payroll")
    res = services.record_submission(session, req2.id, "Client", [("p1.pdf", b"X"), ("p2.pdf", b"X")])
    assert len(res["new_files"]) == 1 and len(res["arrivals"]) == 1

    # arrivals are append-only too
    with pytest.raises(AppendOnlyViolation):
        arrival = session.execute(select(Arrival)).scalars().first()
        arrival.channel = "tampered"
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------------------------
# Test 5 - in-flight duplicate prevention (HTTP 409)
# --------------------------------------------------------------------------------------------
def test_5_duplicate_live_request_returns_409(session, make_engagement):
    from fastapi.testclient import TestClient

    import main

    eng = make_engagement()
    session.rollback()
    form = {"engagement_id": eng.id, "document": "Fixed asset register", "owner_person": "Ravi (Accountant)",
            "actor": "Priya", "period_from": "2025-04-01", "period_to": "2026-03-31"}
    with TestClient(main.app) as client:
        first = client.post("/requests", data=form)
        assert first.status_code == 200, first.text
        second = client.post("/requests", data={**form, "document": "  fixed ASSET register ", "owner_person": "Someone Else"})
        assert second.status_code == 409
        body = second.json()
        assert body["duplicate"] is True
        assert body["existing"]["ref"] == first.json()["ref"]
        assert body["existing"]["owner_person"] == "Ravi (Accountant)"  # who already holds it

        # a different period is a different request
        other = client.post("/requests", data={**form, "period_from": "2024-04-01", "period_to": "2025-03-31"})
        assert other.status_code == 200


# --------------------------------------------------------------------------------------------
# Test 6 - entity conditioning suppresses requests and records why
# --------------------------------------------------------------------------------------------
def test_6_entity_suppression_records_basis(session, make_engagement):
    stock = "Stock register and physical count sheets"

    services_co = make_engagement(profile={"has_inventory": False})
    res = services.generate_requests(
        session, engagement_id=services_co.id, engagement_type="Statutory audit",
        actor="Priya", default_owner="Ravi (Accountant)")
    docs = [r.document for r in services.engagement_requests(session, services_co.id)]
    assert stock not in docs, "no request for a suppressed item"
    assert any(s["document"] == stock for s in res["suppressed"])
    rec = session.execute(
        select(RequestSuppression).where(RequestSuppression.engagement_id == services_co.id,
                                         RequestSuppression.document == stock)
    ).scalar_one()
    assert rec.condition_expr == "has_inventory"
    assert rec.condition_basis == "Suppressed: has_inventory is false"
    assert rec.profile_snapshot["has_inventory"] is False and rec.actor == "Priya"

    manufacturer = make_engagement(profile={"has_inventory": True})
    services.generate_requests(
        session, engagement_id=manufacturer.id, engagement_type="Statutory audit",
        actor="Priya", default_owner="Ravi (Accountant)")
    made = {r.document: r for r in services.engagement_requests(session, manufacturer.id)}
    assert stock in made
    assert "has_inventory is true" in made[stock].condition_basis  # the basis is kept on the request too

    # a condition that tries to run code is refused, never evaluated
    from conditions import ConditionError, validate_condition

    for bad in ("__import__('os').system('x')", "has_inventory and open('f')", "unknown_flag"):
        with pytest.raises(ConditionError):
            validate_condition(bad)


# --------------------------------------------------------------------------------------------
# Test 8 - EQR readiness gate
# --------------------------------------------------------------------------------------------
def test_8_eqr_blocker(session, make_engagement, make_request):
    def gate(eng, closed=False):
        return services.eqr_gate(services.engagement_requests(session, eng.id), closed=closed)

    # an open MATERIAL request blocks while the engagement runs
    eng = make_engagement()
    material = make_request(eng, "Trial balance", significance="Material")
    routine = make_request(eng, "Board minutes", significance="Routine")
    g = gate(eng)
    assert g["blocked"] and g["count"] == 1 and g["requests"][0].id == material.id
    assert g["message"].startswith("BLOCKED: 1 material request")

    # open routine requests alone do not block
    eng_r = make_engagement()
    make_request(eng_r, "Minutes", significance="Routine")
    assert gate(eng_r)["passed"] and gate(eng_r)["message"] == "PASSED: No material requests unfulfilled"

    # after close-out the unfulfilled material request still blocks
    services.close_engagement(session, eng.id, "Partner")
    g = gate(eng, closed=True)
    assert g["blocked"] and g["message"] == "BLOCKED: 1 material request remains unfulfilled"
    rep = services.closeout_report(session, eng.id)
    assert rep["gate"]["blocked"] and rep["counts"]["unfulfilled"] == 2 and rep["counts"]["raised"] == 2
    assert [x["ref"] for x in rep["material_rows"]] == [material.ref]
    assert routine.ref in [x["ref"] for x in rep["routine_rows"]]

    # accepted / waived / not-applicable material requests do not block
    eng2 = make_engagement()
    a = make_request(eng2, "Bank statements", significance="Material")
    sub = services.record_submission(session, a.id, "Client", [("b.pdf", b"bank")])["submission"]
    assert gate(eng2)["blocked"]  # still open until accepted
    services.review_submission(session, sub.id, "accept", "Reviewer")
    w = make_request(eng2, "Loan letters", significance="Material")
    services.append_event(session, w.id, "WAIVED", "Partner", payload={"reason": "No loans"})
    n = make_request(eng2, "Stock sheets", significance="Material")
    services.append_event(session, n.id, "NOT_APPLICABLE", "Partner", payload={"reason": "Services company"})
    assert gate(eng2)["passed"]
    services.close_engagement(session, eng2.id, "Partner")
    rep2 = services.closeout_report(session, eng2.id)
    assert rep2["gate"]["message"] == "PASSED: No material requests unfulfilled"
    assert rep2["counts"] == {"raised": 3, "accepted": 1, "waived_na": 2, "unfulfilled": 0}
    assert not rep2["sa705"]["has_limitation"]


# --------------------------------------------------------------------------------------------
# Close-out outputs (SA 705 extract, SA 580 schedule)
# --------------------------------------------------------------------------------------------
def test_closeout_outputs_and_screen(session, make_engagement, make_request):
    from fastapi.testclient import TestClient

    import main

    eng = make_engagement(client_name="Acme Test Ltd")
    m = make_request(eng, "Trial balance as at 31 March 2026", significance="Material", owner="Meera Shah (Finance Controller)")
    services.record_chase(session, m.id, "Arjun", "Email", template="gentle")
    services.record_chase(session, m.id, "Arjun", "Phone", note="Called, no answer")
    r = make_request(eng, "Board minutes", significance="Routine")
    never_sent = make_request(eng, "Never released", significance="Material", send=False)

    # before close-out the report is a labelled preview and writes nothing
    pre = services.closeout_report(session, eng.id)
    assert pre["preview"] and pre["counts"]["unfulfilled"] == 3
    assert services.get_request(session, m.id).state == "Sent"

    services.close_engagement(session, eng.id, "Partner")
    rep = services.closeout_report(session, eng.id)
    assert rep["closed"] and not rep["preview"]
    assert rep["rows"][0]["ref"] == m.ref and rep["rows"][0]["chases"] == 2  # material first, with its chases

    t705 = rep["sa705"]["text"]
    assert "Trial balance as at 31 March 2026" in t705 and "Acme Test Ltd" in t705
    assert "Board minutes" not in t705, "routine requests are not part of the SA 705 extract"
    assert "2 follow-ups" in t705 and "Meera Shah" in t705

    # a request frozen before it was ever sent was never asked of management: it blocks the EQR gate
    # but is not part of the SA 705 / SA 580 statements
    assert "Never released" not in t705 and rep["gate"]["count"] == 2
    assert rep["sa580"]["unreleased"] == 1 and never_sent.ref not in services.sa580_csv(rep)

    csv_text = services.sa580_csv(rep)
    assert "Trial balance" in csv_text and "Board minutes" in csv_text  # SA 580 lists every unfulfilled request
    assert csv_text.splitlines()[0].startswith("No.,Reference,Document")

    session.rollback()  # app start-up runs DDL, which must not wait on this session
    with TestClient(main.app) as client:
        page = client.get(f"/engagements/{eng.id}/close-out")
        assert page.status_code == 200
        assert "BLOCKED: 2 material requests remain unfulfilled" in page.text
        for suffix in ("sa705", "sa580"):
            assert client.get(f"/engagements/{eng.id}/close-out/{suffix}").status_code == 200
        assert client.get(f"/engagements/{eng.id}/close-out/sa705/download").headers["content-type"].startswith("text/plain")
        assert client.get(f"/engagements/{eng.id}/close-out/sa580/download").headers["content-type"].startswith("text/csv")
        assert client.get(f"/engagements/{eng.id}/close-out/nonsense").status_code == 404
        # uploads and events are refused over HTTP too
        resp = client.post(f"/requests/{r.id}/submissions", files={"files": ("x.pdf", b"late")}, data={"actor": "C"})
        assert resp.status_code == 400
        assert client.post(f"/requests/{r.id}/chase", data={"actor": "A", "channel": "Email", "note": "hi"}).status_code == 400


# ============================================================================================
# Pass 1 completion
# ============================================================================================
import re
import uuid
from datetime import date, timedelta

import emailer
from models import Chase, ClientToken, OutboundEmail, RequestTemplate, TemplateItem, utcnow


def aged(session, eng, document, days, *, significance="Routine", owner="Owner One (Accountant)", chases=0, **kw):
    """A request that was sent `days` days ago, chased `chases` times by WhatsApp."""
    now = utcnow()
    req = services.create_request(
        session, engagement_id=eng.id, document=document, owner_person=owner, actor="T",
        significance=significance, at=now - timedelta(days=days + 1), **kw)
    services.append_event(session, req.id, "SENT", "T", channel="Email", at=now - timedelta(days=days))
    for i in range(chases):
        services.record_chase(session, req.id, "T", "WhatsApp", note="nudge", at=now - timedelta(days=max(days - 1 - i, 0)))
    return req


# --------------------------------------------------------------------------------------------
# Acceptance test 10 - a client submits without an account or a password
# --------------------------------------------------------------------------------------------
def test_10_client_uploads_through_link_without_an_account(session, make_engagement, make_request, client):
    eng = make_engagement(client_email="accounts@client.example")
    a = make_request(eng, "Bank statements", description="Your bank statements for the year", priority=1)
    b = make_request(eng, "Payroll register", priority=2)
    hidden = make_request(eng, "Draft not yet sent", send=False)
    c = make_request(eng, "Board minutes", priority=3, period_from=date(2025, 4, 1), period_to=date(2026, 3, 31))
    sub = services.record_submission(session, c.id, "Client", [("m.pdf", b"minutes")])["submission"]
    services.review_submission(session, sub.id, "reject", "Reviewer", reason_code="WRONG_PERIOD")

    # a chase by email creates the link, emails it, and is waiting to be opened
    res = services.record_chase(session, a.id, "Arjun", "Email", template="gentle")
    assert res["chase"].delivered and not res["chase"].opened and res["emailed"]
    token = services.active_token(session, eng.id)
    assert token is not None

    page = client.get(f"/c/{token.token}")
    assert page.status_code == 200
    assert not client.cookies, "no session, no cookie, no login"
    assert "Your bank statements for the year" in page.text and "Payroll register" in page.text
    assert "Draft not yet sent" not in page.text, "a Drafted request is not visible to the client"
    assert "different period" in page.text and "1 April 2025" in page.text  # prior rejection, with the period restated
    assert 'type="password"' not in page.text
    assert page.headers["cache-control"] == "no-store"

    # opening acknowledged the Sent requests (and only those) and opened the chase
    assert states(session, eng) == {a.ref: "Acknowledged", b.ref: "Acknowledged", hidden.ref: "Drafted", c.ref: "Re-requested"}
    ack = [e for e in services.get_request(session, a.id).events if e.event_type == "ACKNOWLEDGED"]
    assert len(ack) == 1 and ack[0].channel == "Tokenised link" and "Client" in ack[0].actor
    session.expire_all()
    assert session.get(Chase, res["chase"].id).opened is True
    # opening again changes nothing
    client.get(f"/c/{token.token}")
    assert len([e for e in services.get_request(session, a.id).events if e.event_type == "ACKNOWLEDGED"]) == 1

    # the upload itself - no account, no password
    up = client.post(f"/c/{token.token}/requests/{a.id}/upload", files={"files": ("statement.pdf", b"%PDF bytes")},
                     data={"submitted_by": "Ravi Menon"})
    assert up.status_code == 200 and up.json()["stored"] == 1
    req = services.get_request(session, a.id)
    assert req.state == "Submitted"
    assert req.submissions[0].channel == "Tokenised link" and req.submissions[0].submitted_by == "Ravi Menon"
    # the same file again through the link is an arrival, not a second submission
    again = client.post(f"/c/{token.token}/requests/{a.id}/upload", files={"files": ("copy.pdf", b"%PDF bytes")})
    assert again.status_code == 200 and again.json()["repeats"][0]["count"] == 2
    assert len(services.get_request(session, a.id).submissions) == 1

    # the link only reaches its own engagement's open requests
    assert client.post(f"/c/{token.token}/requests/{hidden.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 400
    other = make_request(make_engagement(), "Somebody else's")
    assert client.post(f"/c/{token.token}/requests/{other.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 404


def test_10_client_link_refuses_expired_revoked_unknown_and_closed(session, make_engagement, make_request, client):
    eng = make_engagement()
    req = make_request(eng, "Trial balance")
    before = len(services.get_request(session, req.id).events)

    expired = services.create_client_token(session, eng.id, "Priya", expires_at=utcnow() - timedelta(hours=1))
    r = client.get(f"/c/{expired.token}")
    assert r.status_code == 410 and "expired" in r.text
    assert client.post(f"/c/{expired.token}/requests/{req.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 410

    live = services.create_client_token(session, eng.id, "Priya")
    services.revoke_client_token(session, live.id, "Priya")
    r = client.get(f"/c/{live.token}")
    assert r.status_code == 410 and "withdrawn" in r.text
    assert client.post(f"/c/{live.token}/requests/{req.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 410

    assert client.get("/c/not-a-real-token").status_code == 404

    good = services.create_client_token(session, eng.id, "Priya")
    assert good.token != live.token and services.active_token(session, eng.id).id == good.id
    services.close_engagement(session, eng.id, "Partner")
    r = client.get(f"/c/{good.token}")
    assert r.status_code == 410 and "closed" in r.text
    assert client.post(f"/c/{good.token}/requests/{req.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 410
    # refused links never touch the ledger: only the freeze event was added
    events = services.get_request(session, req.id).events
    assert [e.event_type for e in events][before:] == ["UNFULFILLED_AT_REPORT_DATE"]
    with pytest.raises(services.LedgerError):
        services.create_client_token(session, eng.id, "Priya")


# --------------------------------------------------------------------------------------------
# Chase records, email delivery and the Outbox
# --------------------------------------------------------------------------------------------
def test_chase_table_delivery_and_guard(session, engine_db, make_engagement, make_request):
    eng = make_engagement(client_email="accounts@client.example")
    req = make_request(eng, "Ledgers")

    mail = services.record_chase(session, req.id, "Arjun", "Email", template="date")
    chase = mail["chase"]
    assert chase.channel == "Email" and chase.by_whom == "Arjun" and chase.template == "Ask for a date"
    assert chase.delivered and chase.delivered_at and chase.email_id and not chase.opened
    assert chase.event_id == mail["event"].id and mail["event"].event_type == "CHASED"
    sent = session.get(OutboundEmail, chase.email_id)
    assert sent.kind == "chase" and sent.to_address == "accounts@client.example" and sent.status == "logged"
    assert "/c/" in sent.body and "Ledgers" in sent.body

    by_hand = services.record_chase(session, req.id, "Arjun", "WhatsApp", note="Sent a WhatsApp")
    assert by_hand["chase"].delivered is False and by_hand["emailed"] is False

    no_addr = make_request(make_engagement(), "Orphan")
    res = services.record_chase(session, no_addr.id, "Arjun", "Email", note="hello")
    assert res["no_client_email"] and not res["chase"].delivered and res["event"].event_type == "CHASED"

    # the manual "Chased" event also gets its chase row
    manual = services.append_event(session, req.id, "CHASED", "Arjun", channel="Phone", payload={"note": "called"})
    assert session.execute(select(Chase).where(Chase.event_id == manual[0].id)).scalar_one().delivered is False

    # DB guard: flags may be set, nothing else may change, nothing is deleted
    session.rollback()
    with engine_db.begin() as conn:
        conn.execute(text("UPDATE chase SET opened = TRUE WHERE id = :i"), {"i": chase.id})
    for statement in ("UPDATE chase SET channel = 'Phone' WHERE id = :i",
                      "UPDATE chase SET delivered = FALSE WHERE id = :i",
                      "UPDATE chase SET opened = FALSE WHERE id = :i",
                      "DELETE FROM chase WHERE id = :i",
                      "UPDATE outbound_email SET subject = 'x'"):
        with pytest.raises(DBAPIError):
            with engine_db.begin() as conn:
                conn.execute(text(statement), {"i": chase.id} if ":i" in statement else {})


def test_outbox_page_lists_emails_with_clickable_links(session, make_engagement, make_request, client):
    eng = make_engagement(client_email="accounts@client.example")
    d = make_request(eng, "Outbox doc", send=False)
    res = services.release_requests(session, eng.id, [d.id], "Priya")
    page = client.get(f"/outbox?eng={eng.id}")
    assert page.status_code == 200
    assert "Local outbox" in page.text and "Outbox doc" in page.text
    assert f'href="{res["link"]}"' in page.text, "the link in the email is clickable"


# --------------------------------------------------------------------------------------------
# Batch release (Screen 1)
# --------------------------------------------------------------------------------------------
def test_batch_release_is_atomic_and_emails_the_link(session, make_engagement, make_request, client):
    eng = make_engagement(client_email="accounts@client.example")
    drafts = [make_request(eng, f"Batch doc {i}", send=False, priority=i) for i in (1, 2, 3)]
    already_sent = make_request(eng, "Already sent")

    # one bad apple refuses the whole batch
    with pytest.raises(services.LedgerError, match="Nothing was released"):
        services.release_requests(session, eng.id, [drafts[0].id, already_sent.id], "Priya")
    assert states(session, eng)[drafts[0].ref] == "Drafted"
    assert not services.list_outbox(session, eng.id)

    res = services.release_requests(session, eng.id, [d.id for d in drafts], "Priya")
    assert sorted(res["released"]) == sorted(d.ref for d in drafts)
    assert all(states(session, eng)[d.ref] == "Sent" for d in drafts)
    for d in drafts:
        sent = [e for e in services.get_request(session, d.id).events if e.event_type == "SENT"]
        assert len(sent) == 1 and sent[0].channel == "Email" and sent[0].actor == "Priya"
    emails = services.list_outbox(session, eng.id)
    assert len(emails) == 1 and emails[0].kind == "request_batch"
    assert res["link"] in emails[0].body and all(d.document in emails[0].body for d in drafts)
    assert emails[0].status == "logged" and emails[0].to_address == "accounts@client.example"
    assert services.active_token(session, eng.id) is not None

    # needs a client address
    bare = make_engagement()
    d = make_request(bare, "No address", send=False)
    with pytest.raises(services.LedgerError, match="email address"):
        services.release_requests(session, bare.id, [d.id], "Priya")
    assert states(session, bare)[d.ref] == "Drafted"

    # over HTTP
    more = [make_request(eng, f"HTTP doc {i}", send=False) for i in range(2)]
    r = client.post(f"/engagements/{eng.id}/release", data={"request_id": [m.id for m in more], "actor": "Priya"})
    assert r.status_code == 200 and len(r.json()["released"]) == 2
    assert client.post(f"/engagements/{eng.id}/release", data={"actor": "Priya"}).status_code == 400


def test_release_with_smtp_sends_and_rolls_back_on_failure(session, make_engagement, make_request, monkeypatch):
    eng = make_engagement(client_email="accounts@client.example")
    d1 = make_request(eng, "SMTP doc one", send=False)
    d2 = make_request(eng, "SMTP doc two", send=False)
    monkeypatch.setattr(emailer, "smtp_configured", lambda: True)
    delivered = []
    monkeypatch.setattr(emailer, "_smtp_send", lambda msg: delivered.append(msg))

    res = services.release_requests(session, eng.id, [d1.id], "Priya")
    assert res["email"].status == "sent" and delivered and delivered[0]["To"] == "accounts@client.example"
    assert states(session, eng)[d1.ref] == "Sent"

    def boom(msg):
        raise OSError("connection refused")

    monkeypatch.setattr(emailer, "_smtp_send", boom)
    with pytest.raises(services.LedgerError, match="Nothing was released"):
        services.release_requests(session, eng.id, [d2.id], "Priya")
    assert states(session, eng)[d2.ref] == "Drafted", "a request is never marked Sent when the client was not told"
    failed = [m for m in services.list_outbox(session, eng.id) if m.status == "failed"]
    assert failed and "connection refused" in failed[0].error


# --------------------------------------------------------------------------------------------
# Screen 3 - Material and open is pinned in ALL four cuts
# --------------------------------------------------------------------------------------------
def test_material_is_pinned_in_every_board_cut(session, make_engagement, client):
    eng = make_engagement()
    m_young = aged(session, eng, "Material young", 3, significance="Material", owner="Meera Shah (Controller)")
    m_chased = aged(session, eng, "Material chased", 10, significance="Material", owner="Ravi Menon (Accountant)", chases=3)
    r_old = aged(session, eng, "Routine very old", 40, owner="Meera Shah (Controller)")
    r_chased = aged(session, eng, "Routine most chased", 20, owner="Anita Desai (Compliance)", chases=6)
    aged(session, eng, "Routine other", 12, owner="Ravi Menon (Accountant)", chases=1)
    material_ids = {m_young.id, m_chased.id}

    session.rollback()
    for cut in ("age", "owner", "significance", "chase"):
        board = services.outstanding_board(session, eng.id, cut)
        flags = [b["material"] for b in board["rows"]]
        assert flags == sorted(flags, reverse=True), f"{cut}: material rows must come first"
        assert {b["id"] for b in board["pinned"]} == material_ids, cut
        assert all(not b["material"] for b in board["body"]), cut

        html = client.get(f"/engagements/{eng.id}/board?cut={cut}").text
        pinned_at = html.index('id="board-pinned"')
        assert pinned_at < html.index("Routine very old") and pinned_at < html.index("Routine most chased"), cut
        # every pinned row is flagged so the filter can never hide it, and sits above the routine rows
        assert html.count('data-material="1"') == 2 and html.rindex('data-material="1"') < html.index("Routine very old"), cut

    assert [b["id"] for b in services.outstanding_board(session, eng.id, "chase")["pinned"]] == [m_chased.id, m_young.id]
    assert services.outstanding_board(session, eng.id, "chase")["body"][0]["id"] == r_chased.id
    assert services.outstanding_board(session, eng.id, "age")["body"][0]["id"] == r_old.id
    owner = services.outstanding_board(session, eng.id, "owner")
    meera = next(g for g in owner["groups"] if g["name"] == "Meera Shah")
    assert meera["total"] == 2 and meera["material"] == 1 and [b["id"] for b in meera["rows"]] == [r_old.id]


# --------------------------------------------------------------------------------------------
# Configurable ageing thresholds and partner escalation
# --------------------------------------------------------------------------------------------
def test_ageing_thresholds_are_configurable_and_escalate_to_the_partner(session, make_engagement):
    eng = make_engagement(client_email="c@client.example")
    assert (eng.age_warn_days, eng.age_escalate_days) == (14, 21)
    r6 = aged(session, eng, "Six days", 6)
    r9 = aged(session, eng, "Nine days", 9, significance="Material")
    assert {b["age"]: b["band"] for b in services.outstanding_board(session, eng.id)["rows"]} == {6: "ok", 9: "ok"}

    services.update_engagement_settings(session, eng.id, client_email="c@client.example",
                                        partner_email="Partner@Firm.Example", age_warn_days=5, age_escalate_days=8)
    bands = {b["age"]: b["band"] for b in services.outstanding_board(session, eng.id)["rows"]}
    assert bands == {6: "warn", 9: "crit"}
    assert services.age_band(6, 5, 8) == "warn" and services.age_band(8, 5, 8) == "warn" and services.age_band(9, 5, 8) == "crit"

    for bad in (dict(age_warn_days=10, age_escalate_days=10), dict(age_warn_days=0, age_escalate_days=5),
                dict(age_warn_days="x", age_escalate_days=5), dict(age_warn_days=3, age_escalate_days=400)):
        with pytest.raises(services.LedgerError):
            services.update_engagement_settings(session, eng.id, **bad)
    with pytest.raises(services.LedgerError, match="valid email"):
        services.update_engagement_settings(session, eng.id, client_email="not-an-email", age_warn_days=5, age_escalate_days=8)

    res = services.escalate_to_partner(session, eng.id, "Priya")
    assert res["refs"] == [r9.ref] and res["email"].to_address == "partner@firm.example" and res["email"].kind == "escalation"
    notes = [e for e in services.get_request(session, r9.id).events if e.event_type == "NOTE"]
    assert notes and notes[0].payload["escalation"] == "partner@firm.example"
    with pytest.raises(services.LedgerError, match="escalated already|Nothing is past"):
        services.escalate_to_partner(session, eng.id, "Priya")
    assert next(b for b in services.outstanding_board(session, eng.id)["rows"] if b["id"] == r9.id)["escalated"]

    services.close_engagement(session, eng.id, "Partner")
    with pytest.raises(services.LedgerError, match="closed"):
        services.update_engagement_settings(session, eng.id, age_warn_days=5, age_escalate_days=8)


# --------------------------------------------------------------------------------------------
# Section 6 - rejection messages carry the request's own context
# --------------------------------------------------------------------------------------------
def test_rejection_messages_interpolate_request_context(session, make_engagement, make_request):
    eng = make_engagement(client_name="Acme Components Pvt Ltd")
    req = make_request(eng, "Bank statements", description="Statements for every account",
                       period_from=date(2025, 4, 1), period_to=date(2026, 3, 31))

    def reject(code, **kw):
        sub = services.record_submission(session, req.id, "Client", [(f"{code}.pdf", code.encode() + uuid.uuid4().bytes)])["submission"]
        events = services.review_submission(session, sub.id, "reject", "Reviewer", reason_code=code, **kw)
        return events[0].payload["client_message"], events[1].payload["client_message"]

    msg, again = reject("WRONG_PERIOD")
    assert "different period" in msg and "01 April 2025 to 31 March 2026" in msg and again == msg
    assert "Acme Components Pvt Ltd" in reject("WRONG_ENTITY")[0]
    assert reject("INCOMPLETE", pages="pages 4-6 and 9")[0] == "Pages are missing: pages 4-6 and 9."
    assert reject("INCOMPLETE")[0] == "Pages are missing."
    assert "separate route" in reject("PASSWORD_PROTECTED")[0]
    wrong = reject("NOT_THE_DOCUMENT_ASKED_FOR")[0]
    assert wrong.startswith("Something arrived, but not this.") and "Bank statements - Statements for every account" in wrong
    assert reject("ILLEGIBLE")[0].startswith("The scan or photo cannot be read.")
    assert reject("INCOMPLETE", pages="p. 2")[0] == "Pages are missing: p. 2."

    payload = [e for e in services.get_request(session, req.id).events if e.event_type == "REJECTED"][2].payload
    assert payload["pages"] == "pages 4-6 and 9" and payload["reason_code"] == "INCOMPLETE"


# --------------------------------------------------------------------------------------------
# Section 4 - template label editor and immutable versions
# --------------------------------------------------------------------------------------------
def test_template_publisher_creates_immutable_versions(session, make_engagement, client):
    etype = "Internal audit"
    v1 = services.get_template(session, etype)
    first = next(i for i in v1.items if not i.condition_expr)  # asked on every engagement
    original_label = first.label_text
    old_version = v1.version

    eng_before = make_engagement(engagement_type=etype, client_name="Template Client A")
    services.generate_requests(session, engagement_id=eng_before.id, engagement_type=etype, actor="P", default_owner="O (Acc)")
    first_req = next(r for r in services.engagement_requests(session, eng_before.id) if r.template_item_id == first.id)
    desc_before = first_req.description

    with pytest.raises(services.LedgerError, match="nothing to publish"):
        services.publish_template_version(session, etype, "Priya", {first.id: original_label})

    v2 = services.publish_template_version(
        session, etype, "Priya", {first.id: "  Plain words the accountant uses  "}, note="Plainer wording")
    assert v2.version == old_version + 1 and v2.published_by == "Priya" and v2.change_note == "Plainer wording"
    v2_first = next(i for i in v2.items if i.document == first.document)
    assert v2_first.label_text == "Plain words the accountant uses"
    assert [i.document for i in v2.items] == [i.document for i in v1.items]
    assert [i.condition_expr for i in v2.items] == [i.condition_expr for i in v1.items]
    assert [i.label_text for i in v2.items if i.document != first.document] == [
        i.label_text for i in v1.items if i.document != first.document]

    session.expire_all()
    assert next(i for i in services.get_template(session, etype, old_version).items if i.document == first.document).label_text == original_label, "v1 is untouched"
    assert services.get_template(session, etype).version == v2.version
    assert services.get_request(session, first_req.id).description == desc_before, "requests already raised keep their wording"

    # published versions are immutable
    stored = session.get(TemplateItem, v2_first.id)
    stored.label_text = "tampered"
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    # new engagements use the latest wording; items already generated from v1 are not asked twice
    again = services.generate_requests(session, engagement_id=eng_before.id, engagement_type=etype, actor="P", default_owner="O (Acc)")
    assert not again["created"], "items raised from v1 are recognised in v2"
    fresh = make_engagement(engagement_type=etype, client_name="Template Client B")
    services.generate_requests(session, engagement_id=fresh.id, engagement_type=etype, actor="P", default_owner="O (Acc)")
    got = next(r for r in services.engagement_requests(session, fresh.id) if r.document == first.document)
    assert got.description == "Plain words the accountant uses"

    other_item = next(i for i in v2.items if i.document != first.document)
    v3 = services.publish_template_version(session, etype, "Priya", {other_item.id: "Another item, reworded"})
    assert v3.version == v2.version + 1

    session.rollback()
    page = client.get(f"/templates/manager?type={etype}")
    assert page.status_code == 200 and f"Publish v{v3.version + 1}" in page.text
    old = client.get(f"/templates/manager?type={etype}&version={old_version}")
    assert "earlier version (read-only)" in old.text
    pub = client.post("/templates/publish", data={"type": etype, "actor": "Priya", "item_id": [v3.items[1].id],
                                                  "label": ["Third, reworded"]})
    assert pub.status_code == 200 and pub.json()["version"] == v3.version + 1


# --------------------------------------------------------------------------------------------
# Section 4 - carry-forward, and the exported thread's SHA-256 fingerprint
# --------------------------------------------------------------------------------------------
def test_carry_forward_tags_items_with_last_years_history(session, make_engagement):
    client_name = f"Carry Client {uuid.uuid4().hex[:6]}"
    prior = make_engagement(client_name=client_name, period_from=date(2024, 4, 1), period_to=date(2025, 3, 31))
    services.generate_requests(session, engagement_id=prior.id, engagement_type="Statutory audit",
                               actor="P", default_owner="Last Year Owner (Accountant)")
    reqs = {r.document: r for r in services.engagement_requests(session, prior.id)}
    tb = reqs["Trial balance as at 31 March 2025"]
    bank = reqs["Bank statements - all current accounts"]
    tds = reqs["TDS returns and challans"]
    for r in (tb, bank, tds):
        services.append_event(session, r.id, "SENT", "P", channel="Email")
    sub = services.record_submission(session, tb.id, "Client", [("tb.xlsx", b"tb v1")])["submission"]
    services.review_submission(session, sub.id, "reject", "R", reason_code="UNSIGNED")
    sub = services.record_submission(session, tb.id, "Client", [("tb2.xlsx", b"tb v2")])["submission"]
    services.review_submission(session, sub.id, "accept", "R")
    services.append_event(session, bank.id, "WAIVED", "Partner", payload={"reason": "Bank confirmations obtained directly"})
    services.close_engagement(session, prior.id, "Partner")

    current = make_engagement(client_name=client_name, profile={"has_fixed_assets": True})
    assert services.find_prior_engagement(session, current).id == prior.id
    assert services.find_prior_engagement(session, prior) is None

    plan = services.build_requirement_list(session, current.id, "Statutory audit", carry_forward=True)
    rows = {r["item"].document: r for r in plan["rows"] if r["status"] == "create"}
    assert rows["Trial balance as at {period_end}"]["carry_tag"] == "Same as last year"
    assert rows["Trial balance as at {period_end}"]["carry"]["attempts"] == 2
    assert rows["Trial balance as at {period_end}"]["carry"]["rejections"] == ["Unsigned"]
    assert rows["Bank statements - all current accounts"]["carry_tag"] == "Waived last year"
    assert rows["Bank statements - all current accounts"]["default_include"] is False
    assert rows["TDS returns and challans"]["carry_tag"] == "Unfulfilled last year"
    assert rows["Fixed asset register"]["carry_tag"] == "New this year", "suppressed last year, applies now"
    assert plan["carry_counts"]["Same as last year"] >= 1
    # without carry-forward nothing is tagged
    assert all(r["carry_tag"] is None for r in services.build_requirement_list(session, current.id, "Statutory audit")["rows"])

    out = services.generate_requests(session, engagement_id=current.id, engagement_type="Statutory audit",
                                     actor="P", default_owner="Fallback (Accountant)", carry_forward=True)
    made = {r.document: r for r in services.engagement_requests(session, current.id)}
    new_tb = made["Trial balance as at 31 March 2026"]
    assert new_tb.carried_from_request_id == tb.id
    assert new_tb.owner_person == "Last Year Owner (Accountant)", "last year's owner is carried over"
    note = new_tb.events[0].payload["carried_forward"]
    assert note.startswith("Same as last year") and tb.ref in note and "2 attempts" in note
    assert made["Fixed asset register"].events[0].payload["carried_forward"].startswith("New this year")
    assert any(c["carry_tag"] == "Unfulfilled last year" for c in out["created"])
    with pytest.raises(services.LedgerError, match="no prior-year"):
        services.generate_requests(session, engagement_id=make_engagement(client_name="Brand New Client").id,
                                   engagement_type="Statutory audit", actor="P", default_owner="X", carry_forward=True)


def test_export_thread_fingerprint(session, make_engagement, make_request, client):
    eng = make_engagement(client_email="c@client.example")
    req = make_request(eng, "Fingerprinted")
    other = make_request(eng, "Bystander")
    services.record_submission(session, req.id, "Client", [("a.pdf", b"content")])

    first = services.export_context(session, eng.id, req.ref)
    fp = first["fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{64}", fp) and first["seq_ok"]
    assert services.export_context(session, eng.id, req.ref)["fingerprint"] == fp, "stable"
    services.append_event(session, other.id, "NOTE", "T", payload={"note": "another request"})
    assert services.export_context(session, eng.id, req.ref)["fingerprint"] == fp, "other requests do not affect it"

    session.rollback()
    page = client.get(f"/engagements/{eng.id}/requests/{req.ref}/export?by=Priya")
    assert page.status_code == 200 and fp in page.text and "Thread fingerprint" in page.text
    assert hashlib_sha256_of("content") in page.text, "file hashes are shown in full"

    services.append_event(session, req.id, "NOTE", "T", payload={"note": "a new event"})
    assert services.export_context(session, eng.id, req.ref)["fingerprint"] != fp, "any new event changes it"


def hashlib_sha256_of(text_value: str) -> str:
    import hashlib

    return hashlib.sha256(text_value.encode()).hexdigest()


# ============================================================================================
# Pass 2 / Screen 4 - the mobile client portal
# ============================================================================================
def _cards(html):
    """The request ids rendered as cards on the client page, in order."""
    return [int(i) for i in re.findall(r'class="req-card[^"]*" data-id="(\d+)"', html)]


def test_client_page_shows_five_at_a_time_in_priority_order(session, make_engagement, make_request, client):
    eng = make_engagement()
    spec = [("Item P3 routine", 3, "Routine"), ("Item P1 routine", 1, "Routine"), ("Item P1 material", 1, "Material"),
            ("Item P2 material", 2, "Material"), ("Item P2 routine", 2, "Routine"), ("Item P4 routine", 4, "Routine"),
            ("Item P5 routine", 5, "Routine")]
    reqs = {doc: make_request(eng, doc, priority=p, significance=sig) for doc, p, sig in spec}
    docs = {r.id: d for d, r in reqs.items()}
    token = services.create_client_token(session, eng.id, "Priya").token

    page = client.get(f"/c/{token}")
    assert page.status_code == 200
    cards = _cards(page.text)
    assert len(cards) == 5, "7 items are open but only 5 are shown"
    assert [docs[i] for i in cards] == ["Item P1 material", "Item P1 routine", "Item P2 material",
                                         "Item P2 routine", "Item P3 routine"], "strict priority order, Material first within a priority"
    assert "<b>5 of 7</b>" in page.text and "complete these to unlock the rest" in page.text
    assert 'role="progressbar"' in page.text
    assert "Item P4 routine" not in page.text and "Item P5 routine" not in page.text
    assert 'capture="environment"' in page.text and "I don't have this" in page.text  # phone camera + escape hatch

    # the locked items cannot be reached by guessing their ids
    locked = reqs["Item P4 routine"].id
    r = client.post(f"/c/{token}/requests/{locked}/upload", files={"files": ("x.pdf", b"x")})
    assert r.status_code == 400 and "unlocks" in r.text
    assert client.post(f"/c/{token}/requests/{locked}/unavailable", data={"reason": "none"}).status_code == 400

    # finishing one shown item unlocks the next
    ok = client.post(f"/c/{token}/requests/{reqs['Item P1 material'].id}/upload", files={"files": ("a.pdf", b"first")})
    assert ok.status_code == 200
    cards = _cards(client.get(f"/c/{token}").text)
    assert len(cards) == 5 and docs[cards[-1]] == "Item P4 routine" and reqs["Item P1 material"].id not in cards
    assert client.post(f"/c/{token}/requests/{locked}/upload", files={"files": ("y.pdf", b"y")}).status_code == 200

    # a short queue shows no "unlock" message
    small = make_engagement()
    for i in range(3):
        make_request(small, f"Small {i}", priority=i + 1)
    page = client.get(f"/c/{services.create_client_token(session, small.id, 'P').token}")
    assert len(_cards(page.text)) == 3 and "unlock the rest" not in page.text


def test_owner_scoped_link_shows_and_acknowledges_only_that_owners_requests(session, make_engagement, make_request, client):
    eng = make_engagement()
    meera = "Meera Shah (Finance Controller)"
    m1 = make_request(eng, "Meera one", owner=meera, priority=1)
    m2 = make_request(eng, "Meera two", owner=meera, priority=2)
    ravi = make_request(eng, "Ravi one", owner="Ravi Menon (Accountant)")
    draft = make_request(eng, "Meera draft", owner=meera, send=False)
    services.record_chase(session, m2.id, "Arjun", "WhatsApp", note="nudge Meera")
    services.record_chase(session, ravi.id, "Arjun", "WhatsApp", note="nudge Ravi")

    assert services.engagement_owner_names(session, eng.id) == ["Meera Shah", "Ravi Menon"]
    with pytest.raises(services.LedgerError, match="No request in this engagement"):
        services.create_client_token(session, eng.id, "Priya", owner_person="Nobody Here")
    whole = services.create_client_token(session, eng.id, "Priya")
    scoped = services.create_client_token(session, eng.id, "Priya", owner_person="meera shah")
    assert scoped.owner_person == "Meera Shah" and whole.owner_person is None
    assert services.active_token(session, eng.id).id == whole.id, "the whole-engagement link is untouched"
    assert services.active_token(session, eng.id, "Meera Shah").id == scoped.id
    assert {t.id for t in services.live_tokens(session, eng.id)} == {whole.id, scoped.id}

    page = client.get(f"/c/{scoped.token}")
    assert page.status_code == 200 and "Hello Meera" in page.text
    assert "Meera one" in page.text and "Meera two" in page.text
    assert "Ravi one" not in page.text and "Meera draft" not in page.text
    assert len(_cards(page.text)) == 2
    assert states(session, eng) == {m1.ref: "Acknowledged", m2.ref: "Acknowledged", ravi.ref: "Sent", draft.ref: "Drafted"}
    chases = {c.request_id: c.opened for c in session.execute(select(Chase)).scalars() if c.request_id in (m2.id, ravi.id)}
    assert chases == {m2.id: True, ravi.id: False}, "only the chases on Meera's requests were opened"

    # her link cannot reach anyone else's request
    assert client.post(f"/c/{scoped.token}/requests/{ravi.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 404
    assert client.post(f"/c/{scoped.token}/requests/{ravi.id}/unavailable", data={"reason": "not mine"}).status_code == 404
    assert client.post(f"/c/{scoped.token}/requests/{m1.id}/upload", files={"files": ("m.pdf", b"m")}).status_code == 200

    # the whole-engagement link still shows everything and acknowledges the rest
    page = client.get(f"/c/{whole.token}")
    assert "Ravi one" in page.text and "Meera two" in page.text
    assert states(session, eng)[ravi.ref] == "Acknowledged"

    # regenerating Meera's link withdraws only Meera's previous one
    again = services.create_client_token(session, eng.id, "Priya", owner_person="Meera Shah")
    assert client.get(f"/c/{scoped.token}").status_code == 410
    assert client.get(f"/c/{whole.token}").status_code == 200 and client.get(f"/c/{again.token}").status_code == 200

    page = client.get(f"/engagements/{eng.id}/client-link")
    assert page.status_code == 200 and "Only Meera Shah" in page.text and "Whole engagement" in page.text


def test_multi_page_upload_preserves_page_order_and_hashes(session, make_engagement, make_request, client):
    eng = make_engagement()
    token = services.create_client_token(session, eng.id, "Priya").token
    req = make_request(eng, "Twelve-page ledger")
    pages = [("zebra.jpg", b"page one bytes"), ("apple.jpg", b"page two bytes"), ("mango.jpg", b"page three bytes")]

    res = client.post(f"/c/{token}/requests/{req.id}/upload",
                      files=[("files", (n, c, "image/jpeg")) for n, c in pages], data={"submitted_by": "Ravi Menon"})
    assert res.status_code == 200 and res.json()["stored"] == 3
    got = services.get_request(session, req.id)
    assert len(got.submissions) == 1, "all the pages are ONE submission"
    files = got.submissions[0].files
    assert [(f.page_order, f.filename) for f in files] == [(1, "zebra.jpg"), (2, "apple.jpg"), (3, "mango.jpg")], \
        "the order the client arranged, not alphabetical"
    assert [f.content_hash for f in files] == [services.sha256_hex(c) for _, c in pages]
    assert (services.UPLOAD_DIR / files[1].storage_path).read_bytes() == b"page two bytes"
    assert got.submissions[0].channel == "Tokenised link" and got.state == "Submitted"

    # reversing the order reverses the pages; a repeated photo is an arrival, not another page
    other = make_request(eng, "Another ledger")
    res = client.post(f"/c/{token}/requests/{other.id}/upload", files=[
        ("files", ("c.jpg", b"CCC", "image/jpeg")), ("files", ("b.jpg", b"BBB", "image/jpeg")),
        ("files", ("c-again.jpg", b"CCC", "image/jpeg")), ("files", ("a.jpg", b"AAA", "image/jpeg"))])
    assert res.status_code == 200 and res.json()["stored"] == 3 and res.json()["repeats"][0]["count"] == 2
    sub = services.get_request(session, other.id).submissions[0]
    assert [(f.page_order, f.filename) for f in sub.files] == [(1, "c.jpg"), (2, "b.jpg"), (3, "a.jpg")]
    assert len(sub.files[0].arrivals) == 1 and len(services.get_request(session, other.id).submissions) == 1


def test_client_can_say_i_dont_have_this(session, engine_db, make_engagement, make_request, client):
    eng = make_engagement()
    asked = make_request(eng, "Rental agreements", priority=1)
    other = make_request(eng, "Insurance schedule", priority=2)
    token = services.create_client_token(session, eng.id, "Priya").token

    for bad in ({"reason": ""}, {"reason": "no"}):
        assert client.post(f"/c/{token}/requests/{asked.id}/unavailable", data=bad).status_code == 400
    res = client.post(f"/c/{token}/requests/{asked.id}/unavailable",
                      data={"reason": "Not applicable to our business", "submitted_by": "Ravi Menon"})
    assert res.status_code == 200

    req = services.get_request(session, asked.id)
    ev = req.events[-1]
    assert ev.event_type == "CLIENT_REPORTED_UNAVAILABLE"
    assert ev.payload["reason"] == "Not applicable to our business"
    assert ev.channel == "Tokenised link" and ev.actor == "Ravi Menon (client)"
    assert req.state == "Sent", "it is information: the firm decides what happens to the request"
    assert req.reported_unavailable.id == ev.id

    # it leaves the client's queue and is listed as answered
    page = client.get(f"/c/{token}")
    assert _cards(page.text) == [other.id] and "You told us you don't have these" in page.text
    assert "Not applicable to our business" in page.text and "1 of 2 done" in page.text
    assert client.post(f"/c/{token}/requests/{asked.id}/upload", files={"files": ("x.pdf", b"x")}).status_code == 400

    # the firm sees it on the dashboard row, the board and in the drawer
    assert "Client says they don't have this" in client.get(f"/?eng={eng.id}").text
    assert "Client says they" in client.get(f"/engagements/{eng.id}/board?cut=age").text
    drawer = client.get(f"/requests/{asked.id}/trail")
    assert 'id="unavailable-alert"' in drawer.text and "Not applicable to our business" in drawer.text
    assert "Client reported unavailable" in drawer.text

    # staff cannot forge it, the DB knows the type, and a later submission supersedes it
    with pytest.raises(services.LedgerError):
        services.append_event(session, asked.id, "CLIENT_REPORTED_UNAVAILABLE", "Staff", payload={"reason": "x"})
    session.rollback()
    with pytest.raises(DBAPIError):
        with engine_db.begin() as conn:
            conn.execute(text("INSERT INTO request_event (request_id, seq, event_type, actor, at, payload) "
                              "VALUES (:i, 99, 'NOT_A_REAL_EVENT', 'x', now(), CAST('{}' AS jsonb))"), {"i": asked.id})
    services.record_submission(session, asked.id, "Staff", [("found.pdf", b"found it")])
    assert services.get_request(session, asked.id).reported_unavailable is None

    # refused once the engagement is closed
    services.close_engagement(session, eng.id, "Partner")
    assert client.post(f"/c/{token}/requests/{other.id}/unavailable", data={"reason": "too late"}).status_code == 410


def test_email_reminders_carry_the_owners_own_link(session, make_engagement, make_request):
    eng = make_engagement(client_email="accounts@client.example")
    meera = make_request(eng, "Meera's ledger", owner="Meera Shah (Finance Controller)")
    ravi = make_request(eng, "Ravi's challans", owner="Ravi Menon (Accountant)")
    whole = services.create_client_token(session, eng.id, "Priya")
    scoped = services.create_client_token(session, eng.id, "Priya", owner_person="Meera Shah")

    to_meera = services.record_chase(session, meera.id, "Arjun", "Email", template="gentle")
    to_ravi = services.record_chase(session, ravi.id, "Arjun", "Email", template="gentle")
    body = lambda res: session.get(OutboundEmail, res["chase"].email_id).body  # noqa: E731
    assert f"/c/{scoped.token}" in body(to_meera) and f"/c/{whole.token}" not in body(to_meera)
    assert f"/c/{whole.token}" in body(to_ravi) and f"/c/{scoped.token}" not in body(to_ravi)

    mail = services.send_client_link(session, eng.id, "Priya", scoped.id)
    assert "for Meera Shah" in mail.subject and "Meera's ledger" in mail.body and "Ravi's challans" not in mail.body
    assert f"/c/{scoped.token}" in mail.body
    whole_mail = services.send_client_link(session, eng.id, "Priya")
    assert "Ravi's challans" in whole_mail.body and "Meera's ledger" in whole_mail.body


# ============================================================================================
# Screen 4 audit fixes
# ============================================================================================
def test_client_page_uses_the_templates_client_facing_label(session, make_engagement, client):
    eng = make_engagement()
    services.generate_requests(session, engagement_id=eng.id, engagement_type="Statutory audit",
                               actor="P", default_owner="Ravi Menon (Accountant)")
    tb = next(r for r in services.engagement_requests(session, eng.id) if r.document.startswith("Trial balance"))
    item = session.get(TemplateItem, tb.template_item_id)
    assert item.label_text and tb.description == item.label_text, "the request carries the template's label_text"
    services.append_event(session, tb.id, "SENT", "P", channel="Email")
    token = services.create_client_token(session, eng.id, "P").token

    page = client.get(f"/c/{token}")
    assert page.status_code == 200
    assert item.label_text in page.text, "the client reads the firm-maintained wording"
    assert page.text.index(item.label_text) < page.text.index(tb.document), "plain label first, document name as the subtitle"
    for internal in ("v1 ·", "Suppressed", "Included:", "condition", "has_inventory", "Material", "UNFULFILLED", "REQ-"):
        assert internal not in page.text, f"internal audit term leaked to the client: {internal}"

    # a newly published wording reaches only requests raised afterwards
    v2 = services.publish_template_version(session, "Statutory audit", "Priya", {item.id: "Your year-end trial balance, please"})
    later = make_engagement(client_name="Another client")
    services.generate_requests(session, engagement_id=later.id, engagement_type="Statutory audit", actor="P", default_owner="X (Acc)")
    new_tb = next(r for r in services.engagement_requests(session, later.id) if r.document.startswith("Trial balance"))
    assert new_tb.description == "Your year-end trial balance, please"
    assert services.get_request(session, tb.id).description == item.label_text and v2.version >= 2


def test_firm_can_reopen_and_client_can_take_back_i_dont_have_this(session, make_engagement, make_request, client):
    eng = make_engagement()
    req = make_request(eng, "Rental agreements", priority=1)
    other = make_request(eng, "Insurance", priority=2)
    token = services.create_client_token(session, eng.id, "Priya").token

    def actions():
        return dict(services.available_actions(services.get_request(session, req.id)))

    assert "CLIENT_UNAVAILABLE_CLEARED" not in actions(), "nothing to reopen yet"
    with pytest.raises(services.InvalidTransition, match="nothing to reopen"):
        services.append_event(session, req.id, "CLIENT_UNAVAILABLE_CLEARED", "Priya")

    assert client.post(f"/c/{token}/requests/{req.id}/unavailable", data={"reason": "Not applicable to our business"}).status_code == 200
    assert services.get_request(session, req.id).reported_unavailable is not None
    assert actions()["CLIENT_UNAVAILABLE_CLEARED"] == "Reopen - ask the client again"
    assert req.id not in _cards(client.get(f"/c/{token}").text)

    # the firm reopens it (e.g. after a phone call): back in the client's queue, history intact
    drawer = client.get(f"/requests/{req.id}/trail").text
    assert "data-reopen" in drawer and "Reopen - ask the client again" in drawer
    res = client.post(f"/requests/{req.id}/events", data={"event_type": "CLIENT_UNAVAILABLE_CLEARED", "actor": "Priya",
                                                          "note": "Client confirmed by phone that it exists"})
    assert res.status_code == 200
    got = services.get_request(session, req.id)
    kinds = [e.event_type for e in got.events]
    assert kinds[-1] == "CLIENT_UNAVAILABLE_CLEARED"
    assert kinds.index("CLIENT_REPORTED_UNAVAILABLE") < kinds.index("CLIENT_UNAVAILABLE_CLEARED"), "the original report stays on the ledger"
    assert got.events[-1].payload["note"] == "Client confirmed by phone that it exists" and got.events[-1].actor == "Priya"
    assert got.state == "Acknowledged" and got.reported_unavailable is None  # state unchanged by the report and the reopening
    assert req.id in _cards(client.get(f"/c/{token}").text)
    assert "data-reopen" not in client.get(f"/requests/{req.id}/trail").text
    assert "Client says they don't have this" not in client.get(f"/?eng={eng.id}").text

    # or the client clicked it by mistake and takes it back themselves
    client.post(f"/c/{token}/requests/{req.id}/unavailable", data={"reason": "Pressed the wrong button"})
    page = client.get(f"/c/{token}")
    assert "I found it" in page.text and req.id not in _cards(page.text)
    assert client.post(f"/c/{token}/requests/{other.id}/found").status_code == 400, "only a flagged request can be taken back"
    assert client.post(f"/c/{token}/requests/{req.id}/found", data={"submitted_by": "Ravi Menon"}).status_code == 200
    got = services.get_request(session, req.id)
    assert got.events[-1].event_type == "CLIENT_UNAVAILABLE_CLEARED" and got.events[-1].actor == "Ravi Menon (client)"
    assert got.reported_unavailable is None and req.id in _cards(client.get(f"/c/{token}").text)
    assert len([e for e in got.events if e.event_type == "CLIENT_REPORTED_UNAVAILABLE"]) == 2

    # a client can then simply upload it, and a closed engagement refuses everything
    assert client.post(f"/c/{token}/requests/{req.id}/upload", files={"files": ("r.pdf", b"agreement")}).status_code == 200
    services.close_engagement(session, eng.id, "Partner")
    assert client.post(f"/c/{token}/requests/{other.id}/found").status_code == 410


def _png(width, height, colour):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "PNG")
    return buf.getvalue()


def test_pages_assemble_into_one_pdf_in_page_order(session, make_engagement, make_request, client):
    import io

    from PIL import Image
    from pypdf import PdfReader

    eng = make_engagement()
    token = services.create_client_token(session, eng.id, "Priya").token
    req = make_request(eng, "Twelve-page ledger")
    # three photos of different sizes, uploaded in a deliberate order: wide, tall-ish, small
    pics = [("p1.png", _png(300, 150, "red")), ("p2.png", _png(200, 400, "green")), ("p3.png", _png(100, 80, "blue"))]
    # a phone photo stored sideways with EXIF orientation 6 must come out upright
    sideways = Image.new("RGB", (300, 100), "yellow")
    exif = sideways.getexif()
    exif[274] = 6
    buf = io.BytesIO()
    sideways.save(buf, "JPEG", exif=exif)
    pics.append(("p4.jpg", buf.getvalue()))
    # and one page that is already a PDF
    pdf_buf = io.BytesIO()
    Image.new("RGB", (150, 150), "black").save(pdf_buf, "PDF")
    pics.append(("p5.pdf", pdf_buf.getvalue()))

    assert client.post(f"/c/{token}/requests/{req.id}/upload",
                       files=[("files", (n, c)) for n, c in pics]).status_code == 200
    got = services.get_request(session, req.id)
    sub = got.submissions[0]
    assert services.can_assemble(sub) and len(got.submissions) == 1

    res = client.get(f"/submissions/{sub.id}/document.pdf")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert "attachment" in res.headers["content-disposition"] and f"{got.ref}_attempt1.pdf" in res.headers["content-disposition"]
    pages = PdfReader(io.BytesIO(res.content)).pages
    assert len(pages) == 5, "five pages, one document"
    sizes = [(round(float(p.mediabox.width)), round(float(p.mediabox.height))) for p in pages]
    assert sizes[0][0] > sizes[0][1] and sizes[1][1] > sizes[1][0], "page 1 is the wide photo, page 2 the tall one"
    assert sizes[0][0] > sizes[2][0] and sizes[3][1] > sizes[3][0], "the sideways phone photo was turned upright"
    assert sizes[4] == (72, 72) or sizes[4][0] == sizes[4][1], "the PDF page was appended last"
    # the originals are untouched
    assert (services.UPLOAD_DIR / sub.files[1].storage_path).read_bytes() == pics[1][1]

    # the firm finds the link in the drawer
    assert f"/submissions/{sub.id}/document.pdf" in client.get(f"/requests/{req.id}/trail").text

    # a spreadsheet cannot be merged: clear refusal, and no link is offered
    sheet = make_request(eng, "Trial balance")
    client.post(f"/c/{token}/requests/{sheet.id}/upload", files=[("files", ("tb.xlsx", b"PK-not-an-image"))])
    xs = services.get_request(session, sheet.id).submissions[0]
    assert not services.can_assemble(xs)
    assert client.get(f"/submissions/{xs.id}/document.pdf").status_code == 400
    assert f"/submissions/{xs.id}/document.pdf" not in client.get(f"/requests/{sheet.id}/trail").text
    assert client.get("/submissions/999999/document.pdf").status_code == 404


# ============================================================================================
# Pass 2 - the two-way WhatsApp channel
# ============================================================================================
import base64
import hashlib
from datetime import datetime, timezone
import hmac as _hmac
import json as _json

import whatsapp
from models import WhatsAppMessage

import itertools
import types

_wa_counter = itertools.count(1)


@pytest.fixture()
def wa():
    """Phone numbers unique to one test (every test gets its own client and owner numbers), in the
    messy forms people type them and in their normalised form."""
    n = next(_wa_counter)
    c10, m10 = f"99{n:08d}", f"98{n:08d}"
    return types.SimpleNamespace(
        c=f"+91{c10}", m=f"+91{m10}", c_raw=f"+91 {c10[:5]} {c10[5:]}", m_raw=f"+91 {m10[:5]} {m10[5:]}",
        c_trunk=f"0{c10[:5]} {c10[5:]}", m_trunk=f"0{m10[:5]} {m10[5:]}")


def _wa_eng(make_engagement, wa, **kw):
    """An engagement whose client number is written the messy way a person types it."""
    return make_engagement(client_whatsapp=wa.c_raw, **kw)


def _wa_messages(session, phone):
    session.expire_all()
    return list(session.execute(select(WhatsAppMessage).where(WhatsAppMessage.phone == phone)
                                .order_by(WhatsAppMessage.id)).scalars())


def _photo(n):
    return (f"IMG-{n}.jpg", "image/jpeg", f"photo bytes number {n}".encode())


# --------------------------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------------------------
def test_whatsapp_chase_is_sent_logged_and_tracked(session, wa, make_engagement, make_request, client):
    eng = _wa_eng(make_engagement, wa, whatsapp_contacts={"meera shah": wa.m_trunk})
    ravi = make_request(eng, "Bank statements", description="Your bank statements for the year",
                        owner="Ravi Menon (Accountant)", period_from=date(2025, 4, 1), period_to=date(2026, 3, 31))
    meera = make_request(eng, "Payroll register", owner="Meera Shah (Finance Controller)")

    res = services.record_chase(session, ravi.id, "Arjun", "WhatsApp", template="gentle")
    assert res["whatsapped"] and res["chase"].delivered and res["chase"].whatsapp_id and not res["no_client_whatsapp"]
    assert res["event"].event_type == "CHASED" and res["event"].channel == "WhatsApp"
    msg = session.get(WhatsAppMessage, res["chase"].whatsapp_id)
    assert (msg.direction, msg.kind, msg.status, msg.phone) == ("out", "chase", "simulated", wa.c)
    assert msg.provider_message_id.startswith("sim-") and msg.request_id == ravi.id
    assert "Your bank statements for the year" in msg.body, "the client's own wording"
    assert "01 April 2025 to 31 March 2026" in msg.body and ravi.ref in msg.body
    assert re.search(r"http://testserver/c/\S+", msg.body), "the no-login link"

    # the owner's own number when she has one
    to_meera = services.record_chase(session, meera.id, "Arjun", "WhatsApp", note="Payroll please, Meera")
    assert session.get(WhatsAppMessage, to_meera["chase"].whatsapp_id).phone == wa.m
    assert "Payroll please, Meera" in session.get(WhatsAppMessage, to_meera["chase"].whatsapp_id).body

    # no number: still recorded as a chase made by hand, but nothing is sent
    bare = make_request(make_engagement(), "Orphan")
    res = services.record_chase(session, bare.id, "Arjun", "WhatsApp", note="called and messaged")
    assert res["no_client_whatsapp"] and not res["whatsapped"] and not res["chase"].delivered
    assert res["event"].channel == "WhatsApp"

    # the simulator screen shows it
    page = client.get(f"/whatsapp?phone={wa.c.replace('+', '%2B')}")
    assert page.status_code == 200 and "Your bank statements for the year" in page.text and "simulated" in page.text
    assert "Simulate Client WhatsApp Reply" in page.text


def test_release_on_whatsapp_sends_one_message_and_logs_sent_events(session, wa, make_engagement, make_request, client, monkeypatch):
    eng = _wa_eng(make_engagement, wa)
    drafts = [make_request(eng, f"Doc number {i}", send=False, priority=(i % 3) + 1) for i in range(1, 8)]
    with pytest.raises(services.LedgerError, match="WhatsApp number"):
        services.release_requests(session, make_engagement().id, [make_request(make_engagement(), "x", send=False).id],
                                  "Priya", "WhatsApp")

    res = services.release_requests(session, eng.id, [d.id for d in drafts], "Priya", "WhatsApp")
    assert res["channel"] == "WhatsApp" and res["email"] is None and len(res["released"]) == 7
    for d in drafts:
        sent = [e for e in services.get_request(session, d.id).events if e.event_type == "SENT"]
        assert len(sent) == 1 and sent[0].channel == "WhatsApp" and "WhatsApp" in sent[0].payload["note"]
    batch = [m for m in _wa_messages(session, wa.c) if m.kind == "request_batch"]
    assert len(batch) == 1, "one message, not seven"
    body = batch[0].body
    assert res["link"] in body and "7 document requests" in body and "...and 2 more" in body
    assert sum(1 for d in drafts if f"[{d.ref}]" in body) == 5, "five at a time, with their codes"
    assert not services.list_outbox(session, eng.id), "no email was involved"

    # the provider refusing it: nothing is released
    more = [make_request(eng, f"Late doc {i}", send=False) for i in range(2)]

    def boom(to, body):
        raise whatsapp.WhatsAppDeliveryError("provider down")

    monkeypatch.setattr(whatsapp, "send_text", boom)
    with pytest.raises(services.LedgerError, match="Nothing was released"):
        services.release_requests(session, eng.id, [m.id for m in more], "Priya", "WhatsApp")
    assert all(states(session, eng)[m.ref] == "Drafted" for m in more)
    assert [m for m in _wa_messages(session, wa.c) if m.status == "failed" and "provider down" in (m.note or "")]
    with pytest.raises(services.LedgerError, match="chase was not recorded"):
        services.record_chase(session, drafts[0].id, "Arjun", "WhatsApp", note="x")
    assert len([e for e in services.get_request(session, drafts[0].id).events if e.event_type == "CHASED"]) == 0
    monkeypatch.undo()

    # over HTTP
    r = client.post(f"/engagements/{eng.id}/release", data={"request_id": [m.id for m in more], "actor": "Priya", "channel": "WhatsApp"})
    assert r.status_code == 200 and r.json()["channel"] == "WhatsApp" and r.json()["to"] == wa.c


# --------------------------------------------------------------------------------------------
# Inbound: photos and files come back
# --------------------------------------------------------------------------------------------
def test_whatsapp_reply_with_photos_is_one_submission_in_order(session, wa, make_engagement, make_request):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Twelve-page ledger")
    chase = services.record_chase(session, req.id, "Arjun", "WhatsApp", template="gentle")
    asked = session.get(WhatsAppMessage, chase["chase"].whatsapp_id)
    t0 = utcnow() + timedelta(seconds=5)

    out = services.receive_whatsapp(
        session, phone=wa.c_trunk, text="here you go", media=[_photo(1), _photo(2), _photo(3)],
        reply_to=asked.provider_message_id, name="Ravi Menon", received_at=t0, final=True)
    assert out["status"] == "submitted" and out["matched_by"] == "reply"

    got = services.get_request(session, req.id)
    kinds = [(e.event_type, e.channel) for e in got.events]
    assert kinds[-2:] == [("ACKNOWLEDGED", "WhatsApp"), ("SUBMITTED", "WhatsApp")], "acknowledged first, then submitted"
    assert got.events[-2].actor == "Ravi Menon (client)" and "here you go" in got.events[-2].payload["note"]
    assert got.state == "Submitted" and len(got.submissions) == 1
    sub = got.submissions[0]
    assert sub.channel == "WhatsApp" and sub.submitted_by == "Ravi Menon"
    assert [(f.page_order, f.filename) for f in sub.files] == [(1, "IMG-1.jpg"), (2, "IMG-2.jpg"), (3, "IMG-3.jpg")]
    assert got.events[-1].payload["note"] == "here you go", "the caption travels with the submission"
    assert services.can_assemble(sub)
    inbound = [m for m in _wa_messages(session, wa.c) if m.direction == "in"]
    assert len(inbound) == 1 and inbound[0].status == "submitted" and inbound[0].submission_id == sub.id
    assert inbound[0].created_at == t0, "the exact arrival time is kept"
    assert any("Received 3 pages" in (m.body or "") for m in _wa_messages(session, wa.c) if m.direction == "out"), "the client is thanked"


def test_whatsapp_photos_sent_one_by_one_are_assembled_with_their_own_times(session, wa, make_engagement, make_request):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Bank statements")
    t0 = utcnow() + timedelta(seconds=5)

    a = services.receive_whatsapp(session, phone=wa.c, text=f"{req.ref} page by page", media=[_photo(1)],
                                  received_at=t0, name="Ravi Menon")
    assert a["status"] == "buffered" and a["matched_by"] == "code"
    b = services.receive_whatsapp(session, phone=wa.c, media=[_photo(2)], received_at=t0 + timedelta(seconds=30))
    c = services.receive_whatsapp(session, phone=wa.c, media=[_photo(3)], received_at=t0 + timedelta(seconds=50))
    assert b["matched_by"] == "continued" and c["matched_by"] == "continued", "later photos join the run - no guessing"
    assert services.get_request(session, req.id).submissions == [], "nothing is filed while photos are still arriving"
    assert services.flush_whatsapp_batches(session, now=t0 + timedelta(seconds=60)) == [], "still inside the quiet window"

    done = services.flush_whatsapp_batches(session, now=t0 + timedelta(seconds=50 + services.WHATSAPP_ASSEMBLY_SECONDS + 1))
    assert len(done) == 1 and done[0]["status"] == "submitted" and done[0]["pages"] == 3
    got = services.get_request(session, req.id)
    sub = got.submissions[0]
    assert [(f.page_order, f.filename) for f in sub.files] == [(1, "IMG-1.jpg"), (2, "IMG-2.jpg"), (3, "IMG-3.jpg")]
    assert sub.received_at == t0, "the submission began when the first photo arrived"
    assert got.events[-1].event_type == "SUBMITTED" and got.events[-1].at == t0 + timedelta(seconds=50)
    assert [m.status for m in _wa_messages(session, wa.c) if m.direction == "in"] == ["submitted"] * 3

    # saying "done" assembles straight away
    other = make_request(eng, "Payroll")
    t1 = utcnow() + timedelta(minutes=5)
    services.receive_whatsapp(session, phone=wa.c, text=other.ref, media=[_photo(8)], received_at=t1)
    res = services.receive_whatsapp(session, phone=wa.c, text="Done!", received_at=t1 + timedelta(seconds=5))
    assert res["assembled"][0]["status"] == "submitted"
    assert len(services.get_request(session, other.id).submissions) == 1


def test_whatsapp_resend_is_an_arrival_not_a_second_submission(session, wa, make_engagement, make_request, client):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Trial balance")
    token = services.create_client_token(session, eng.id, "Priya").token
    t0 = utcnow() + timedelta(seconds=5)

    # the same photo twice in one WhatsApp message is one page
    out = services.receive_whatsapp(session, phone=wa.c, text=req.ref, media=[_photo(1), _photo(2), _photo(1)],
                                    received_at=t0, final=True)
    assert out["status"] == "submitted"
    sub = services.get_request(session, req.id).submissions[0]
    assert [f.filename for f in sub.files] == ["IMG-1.jpg", "IMG-2.jpg"] and len(sub.files[0].arrivals) == 1

    # the client sends photo 2 again later: an arrival on WhatsApp, no new submission
    t1 = t0 + timedelta(minutes=2)
    again = services.receive_whatsapp(session, phone=wa.c, text=f"{req.ref} sorry, again", media=[_photo(2)],
                                      received_at=t1, final=True)
    assert again["status"] == "duplicate"
    got = services.get_request(session, req.id)
    assert len(got.submissions) == 1, "still one submission"
    f2 = got.submissions[0].files[1]
    assert len(f2.arrivals) == 1 and f2.arrivals[0].channel == "WhatsApp" and f2.arrivals[0].arrived_at == t1
    assert 1 + len(f2.arrivals) == 2, "two arrivals of that photo"
    assert got.events[-1].event_type == "ARRIVAL_RECORDED" and got.events[-1].channel == "WhatsApp"
    assert [m for m in _wa_messages(session, wa.c) if m.status == "duplicate"]

    # content already received by another channel is matched too (one engine for every channel)
    other = make_request(eng, "Payroll")
    assert client.post(f"/c/{token}/requests/{other.id}/upload", files={"files": ("p.jpg", b"shared bytes")}).status_code == 200
    res = services.receive_whatsapp(session, phone=wa.c, text=other.ref, media=[("copy.jpg", "image/jpeg", b"shared bytes")],
                                    received_at=utcnow() + timedelta(minutes=3), final=True)
    assert res["status"] == "duplicate" and len(services.get_request(session, other.id).submissions) == 1


def test_whatsapp_unavailable_notes_unmatched_and_unknown_senders(session, wa, make_engagement, make_request):
    eng = _wa_eng(make_engagement, wa, whatsapp_contacts={"meera shah": wa.m})
    ravi = make_request(eng, "Rental agreements", owner="Ravi Menon (Accountant)", priority=1)
    meera = make_request(eng, "Payroll", owner="Meera Shah (Finance Controller)", priority=2)
    t = utcnow() + timedelta(seconds=5)

    # "I don't have this" -> acknowledged, then CLIENT_REPORTED_UNAVAILABLE on WhatsApp
    out = services.receive_whatsapp(session, phone=wa.c, text=f"{ravi.ref} I don't have this, sorry",
                                    received_at=t, name="Ravi Menon")
    assert out["status"] == "unavailable"
    got = services.get_request(session, ravi.id)
    assert [e.event_type for e in got.events][-2:] == ["ACKNOWLEDGED", "CLIENT_REPORTED_UNAVAILABLE"]
    assert got.events[-1].channel == "WhatsApp" and "don't have this" in got.events[-1].payload["reason"]
    assert got.reported_unavailable is not None and got.state == "Acknowledged"

    # plain text from an acknowledged request: a note on the ledger
    services.receive_whatsapp(session, phone=wa.c, text=f"{meera.ref} will send tomorrow", received_at=t + timedelta(seconds=1))
    out = services.receive_whatsapp(session, phone=wa.c, text=f"{meera.ref} sorry, Monday actually", received_at=t + timedelta(seconds=2))
    note = services.get_request(session, meera.id).events[-1]
    assert out["status"] == "noted" and note.event_type == "NOTE" and note.channel == "WhatsApp" and "Monday" in note.payload["note"]

    # no code, no reply -> never guessed: stored as unmatched, and the client is asked
    stray = services.receive_whatsapp(session, phone=wa.c, media=[_photo(9)], received_at=t + timedelta(seconds=3), final=True)
    assert stray["status"] == "unmatched"
    asks = [m for m in _wa_messages(session, wa.c) if m.kind == "auto_reply" and m.note == "which_request"]
    assert len(asks) == 1 and f"[{meera.ref}]" in asks[0].body
    services.receive_whatsapp(session, phone=wa.c, media=[_photo(10)], received_at=t + timedelta(seconds=4), final=True)
    assert len([m for m in _wa_messages(session, wa.c) if m.note == "which_request"]) == 1, "asked once, not nagged"
    assert not services.get_request(session, meera.id).submissions

    # a person assigns it: processed exactly as if the client had sent the code
    lone = stray["message"]
    with pytest.raises(services.LedgerError, match="not one this WhatsApp number"):
        services.assign_whatsapp_message(session, lone.id, make_request(make_engagement(), "Elsewhere").id, "Priya")
    assigned = services.assign_whatsapp_message(session, lone.id, meera.id, "Priya")
    assert assigned["status"] == "submitted"
    assert [f.filename for f in services.get_request(session, meera.id).submissions[0].files] == ["IMG-9.jpg"]
    with pytest.raises(services.LedgerError, match="already handled"):
        services.assign_whatsapp_message(session, lone.id, meera.id, "Priya")

    # Meera's own number can only answer for Meera's requests
    wrong = services.receive_whatsapp(session, phone=wa.m, text=f"{ravi.ref} here", media=[_photo(5)], final=True)
    assert wrong["status"] == "unmatched" and "someone else" in wrong["reason"]
    right = services.receive_whatsapp(session, phone=wa.m, text=f"{meera.ref} more pages", media=[_photo(6)], final=True)
    assert right["status"] == "submitted" or right["status"] == "failed"  # Submitted already: new content refused politely

    # a stranger's number: stored, never answered, nothing touched
    before = len(services.get_request(session, ravi.id).events)
    stranger = services.receive_whatsapp(session, phone="+14155550100", text=f"{ravi.ref} hello")
    assert stranger["status"] == "unknown_sender"
    assert not _wa_messages(session, "+14155550100")[0].request_id
    assert [m for m in _wa_messages(session, "+14155550100") if m.direction == "out"] == []
    assert len(services.get_request(session, ravi.id).events) == before

    # a webhook retry with the same provider id is ignored; a closed engagement refuses politely
    first = services.receive_whatsapp(session, phone=wa.c, text=f"{meera.ref} hi again", provider_message_id="wamid.1")
    retry = services.receive_whatsapp(session, phone=wa.c, text=f"{meera.ref} hi again", provider_message_id="wamid.1")
    assert retry["duplicate_delivery"] and retry["message"].id == first["message"].id
    services.close_engagement(session, eng.id, "Partner")
    late = services.receive_whatsapp(session, phone=wa.c, text=f"{ravi.ref} still here", media=[_photo(7)], final=True)
    assert late["status"] == "ignored"
    assert [m.body for m in _wa_messages(session, wa.c) if m.direction == "out"][-1].startswith("Thank you - this engagement is closed")


# --------------------------------------------------------------------------------------------
# Unified timeline: ordering, badges and timestamps in the drawer and the exported thread
# --------------------------------------------------------------------------------------------
def test_whatsapp_events_form_one_ordered_timeline_with_badges(session, wa, make_engagement, make_request, client):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Ledgers", send=False)
    services.release_requests(session, eng.id, [req.id], "Priya", "WhatsApp")
    services.record_chase(session, req.id, "Arjun", "WhatsApp", template="date")
    t = utcnow() + timedelta(seconds=10)
    services.receive_whatsapp(session, phone=wa.c, text=f"{req.ref} attached", media=[_photo(1), _photo(2)],
                              received_at=t, name="Ravi Menon", final=True)

    got = services.get_request(session, req.id)
    assert [(e.event_type, e.channel) for e in got.events] == [
        ("DRAFTED", "Manual"), ("SENT", "WhatsApp"), ("CHASED", "WhatsApp"), ("ACKNOWLEDGED", "WhatsApp"), ("SUBMITTED", "WhatsApp")]
    assert [e.seq for e in got.events] == [1, 2, 3, 4, 5]
    stamps = [e.at for e in got.events]
    assert stamps == sorted(stamps), "outbound and inbound events interleave in time order"
    assert got.events[3].at == t and got.events[4].at == t

    session.rollback()
    drawer = client.get(f"/requests/{req.id}/trail").text
    assert drawer.count("WhatsApp</span>") >= 5, "every WhatsApp event (and the submission) carries a badge"
    assert "Replied on WhatsApp: " in drawer and "attached" in drawer
    export = client.get(f"/engagements/{eng.id}/requests/{req.ref}/export").text
    assert export.count('class="pill wa">WhatsApp') >= 5 and "Thread fingerprint" in export
    assert "Replied on WhatsApp" in export
    # the exported thread lists the events in the same order
    timeline = export[export.index("Timeline"):]
    positions = [timeline.index(f'<td class="ev">{label}') for label in ("Drafted", "Sent", "Chased", "Acknowledged", "Submitted")]
    assert positions == sorted(positions)


# --------------------------------------------------------------------------------------------
# The webhook and the simulator endpoints
# --------------------------------------------------------------------------------------------
def test_whatsapp_webhook_simulator_and_signature(session, wa, make_engagement, make_request, client, monkeypatch):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Ledger one")
    other = make_request(eng, "Ledger two")
    third = make_request(eng, "Ledger three")

    # generic JSON webhook: a message with two attachments is one submission
    payload = {"from": wa.c_raw, "text": f"{req.ref} attached", "name": "Ravi Menon",
               "media": [{"filename": "a.jpg", "content_type": "image/jpeg", "data_base64": base64.b64encode(b"AAA").decode()},
                         {"filename": "b.jpg", "content_type": "image/jpeg", "data_base64": base64.b64encode(b"BBB").decode()}]}
    r = client.post("/webhooks/whatsapp/inbound", json=payload)
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "submitted"
    assert [f.filename for f in services.get_request(session, req.id).submissions[0].files] == ["a.jpg", "b.jpg"]
    assert client.post("/webhooks/whatsapp/inbound", content=b"not json").status_code == 400
    assert client.post("/webhooks/whatsapp/inbound", json={"from": "x", "text": "hi"}).status_code == 400

    # Meta Cloud API format: one message per photo, media fetched from the provider
    monkeypatch.setattr(whatsapp, "fetch_media", lambda media_id: (f"bytes of {media_id}".encode(), "image/jpeg"))

    def meta(msg_id, media_id, caption=None, ts="1893456000"):
        return {"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": wa.c.lstrip("+"), "profile": {"name": "Ravi"}}],
            "messages": [{"from": wa.c.lstrip("+"), "id": msg_id, "timestamp": ts, "type": "image",
                          "image": {"id": media_id, "mime_type": "image/jpeg", **({"caption": caption} if caption else {})}}]}}]}]}

    assert client.post("/webhooks/whatsapp/inbound", json=meta("wamid.A", "media-1", f"{other.ref} first")).json()["results"][0]["status"] == "buffered"
    assert client.post("/webhooks/whatsapp/inbound", json=meta("wamid.B", "media-2", None, "1893456010")).json()["results"][0]["status"] == "buffered"
    assert client.post("/webhooks/whatsapp/inbound", json=meta("wamid.A", "media-1", f"{other.ref} first")).json()["results"][0]["status"] == "buffered", "a retried delivery changes nothing"
    flushed = client.post("/whatsapp/flush").json()["assembled"]
    assert len(flushed) == 1 and flushed[0]["pages"] == 2
    session.expire_all()
    sub = services.get_request(session, other.id).submissions[0]
    assert sub.received_at == datetime.fromtimestamp(1893456000, tz=timezone.utc), "the provider's timestamp is the arrival time"

    # the simulator: separate messages, "I don't have this", and assigning
    sim = client.post("/whatsapp/simulate", data={"phone": wa.c, "text": third.ref, "separate": "1"},
                      files=[("files", ("s1.jpg", b"S1", "image/jpeg")), ("files", ("s2.jpg", b"S2", "image/jpeg"))])
    assert sim.status_code == 200 and sim.json()["statuses"] == ["buffered", "buffered"]
    assert client.post("/whatsapp/flush").json()["assembled"][0]["pages"] == 2
    un = make_request(eng, "Needs a no")
    assert client.post("/whatsapp/simulate", data={"phone": wa.c, "text": un.ref, "action": "unavailable"}).json()["statuses"] == ["unavailable"]
    stray = client.post("/whatsapp/simulate", data={"phone": wa.c}, files=[("files", ("lost.jpg", b"LOST", "image/jpeg"))])
    assert stray.json()["statuses"] == ["unmatched"]
    target = make_request(eng, "Assign me")
    lone = next(m for m in _wa_messages(session, wa.c) if m.status == "unmatched")
    assert client.post(f"/whatsapp/messages/{lone.id}/assign", data={"request_id": target.id, "actor": "Priya"}).status_code == 200
    # attachments download; photos can show inline
    assert client.get(f"/whatsapp/media/{lone.id}/0").headers["content-disposition"].startswith("attachment")
    inline = client.get(f"/whatsapp/media/{lone.id}/0?inline=1")
    assert inline.headers["content-type"] == "image/jpeg" and inline.headers["content-security-policy"] == "sandbox"
    assert client.get(f"/whatsapp/media/{lone.id}/5").status_code == 404

    # live mode: signature required, verification handshake, simulator off
    monkeypatch.setattr(whatsapp, "is_live", lambda: True)
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: {"id": "wamid.out", "mode": "sent"})
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    body = _json.dumps({"from": wa.c, "text": f"{target.ref} note"}).encode()
    assert client.post("/webhooks/whatsapp/inbound", content=body).status_code == 403
    assert client.post("/webhooks/whatsapp/inbound", content=body, headers={"x-hub-signature-256": "sha256=deadbeef"}).status_code == 403
    sig = "sha256=" + _hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/whatsapp/inbound", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert client.get("/webhooks/whatsapp/inbound", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"}).text == "1234"
    assert client.get("/webhooks/whatsapp/inbound", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403
    assert client.post("/whatsapp/simulate", data={"phone": wa.c, "text": "hi"}).status_code == 403
    assert whatsapp.normalise_phone("98765 43210") == "+919876543210" and whatsapp.normalise_phone("0044 20 7946 0958") == "+442079460958"
    assert whatsapp.normalise_phone("12") is None and whatsapp.normalise_phone("") is None
    monkeypatch.undo()


def test_engagement_whatsapp_settings_are_validated_and_normalised(session, wa, make_engagement, make_request):
    eng = make_engagement(client_email="c@client.example")
    services.update_engagement_settings(
        session, eng.id, client_email="c@client.example", client_whatsapp=wa.c_trunk,
        whatsapp_contacts={"Meera Shah": wa.m_raw, "Nobody": ""})
    session.expire_all()
    eng = services.get_engagement(session, eng.id)
    assert eng.client_whatsapp == wa.c and eng.whatsapp_contacts == {"meera shah": wa.m}
    with pytest.raises(services.LedgerError, match="not a valid WhatsApp number"):
        services.update_engagement_settings(session, eng.id, client_whatsapp="12")
    # settings saved without the WhatsApp fields leave the numbers alone
    services.update_engagement_settings(session, eng.id, client_email="c@client.example")
    session.expire_all()
    assert services.get_engagement(session, eng.id).client_whatsapp == wa.c


# ============================================================================================
# End-to-end audit against the specification
# ============================================================================================
SPEC_STATES = ["Drafted", "Sent", "Acknowledged", "Submitted", "Under review", "Accepted", "Rejected",
               "Re-requested", "Waived", "Not applicable", "Unfulfilled at report date"]


def test_eleven_spec_states_and_strict_transitions(session, make_engagement, make_request):
    from models import ALL_EVENT_TYPES, EVENT_STATE, STATES

    # Section 3: exactly these eleven states (there is no "Partially received" in the specification)
    assert STATES == SPEC_STATES and set(EVENT_STATE.values()) == set(SPEC_STATES)
    assert services.ALLOWED_NEXT["Rejected"] == {"RE_REQUESTED"}, "a rejection always leads to the re-request"

    def build(state):
        eng = make_engagement()
        req = make_request(eng, f"Doc for {state}", send=state != "Drafted")
        if state in ("Acknowledged", "Submitted", "Under review"):
            services.append_event(session, req.id, "ACKNOWLEDGED", "T", channel="Email")
        if state in ("Submitted", "Under review", "Accepted", "Re-requested"):
            sub = services.record_submission(session, req.id, "C", [("a.pdf", state.encode())])["submission"]
            if state == "Under review":
                services.append_event(session, req.id, "UNDER_REVIEW", "T")
            if state == "Accepted":
                services.review_submission(session, sub.id, "accept", "R")
            if state == "Re-requested":
                services.review_submission(session, sub.id, "reject", "R", reason_code="UNSIGNED")
        if state == "Waived":
            services.append_event(session, req.id, "WAIVED", "P", payload={"reason": "not needed"})
        if state == "Not applicable":
            services.append_event(session, req.id, "NOT_APPLICABLE", "P", payload={"reason": "no such account"})
        if state == "Unfulfilled at report date":
            services.close_engagement(session, eng.id, "P")
        assert services.get_request(session, req.id).state == state
        assert re.fullmatch(r"REQ-\d{3}-\d{3}", req.ref), "the unit of record is the request, REQ-XXX-NNN"
        return req

    checked = 0
    for state in SPEC_STATES:
        if state == "Rejected":
            continue  # never a resting state: the REJECTED event is followed at once by RE_REQUESTED
        req = build(state)
        before = len(services.get_request(session, req.id).events)
        allowed = services.ALLOWED_NEXT[state] - services.SYSTEM_ONLY
        for et in ALL_EVENT_TYPES:
            if et in allowed:
                continue
            with pytest.raises(services.InvalidTransition):
                services.append_event(session, req.id, et, "T", channel="Email",
                                      payload={"reason": "x", "note": "x", "reason_code": "UNSIGNED"})
            checked += 1
        assert len(services.get_request(session, req.id).events) == before, f"{state}: a refused move writes nothing"
    assert checked > 60

    # the moves the specification singles out
    eng = make_engagement()
    r = make_request(eng, "One")
    with pytest.raises(services.InvalidTransition):
        services.append_event(session, r.id, "ACCEPTED", "T")  # nothing has arrived to accept
    sub = services.record_submission(session, r.id, "C", [("x.pdf", b"x")])["submission"]
    ev = services.review_submission(session, sub.id, "reject", "R", reason_code="ILLEGIBLE")
    assert [e.event_type for e in ev] == ["REJECTED", "RE_REQUESTED"] and services.get_request(session, r.id).attempt_count == 2


def test_closed_engagement_is_locked_by_the_database_itself(session, engine_db, make_engagement, make_request, client):
    eng = make_engagement(client_email="c@client.example")
    req = make_request(eng, "Ledgers")
    services.record_chase(session, req.id, "Arjun", "Email", template="gentle")
    services.record_submission(session, req.id, "Client", [("a.pdf", b"same")])
    services.record_submission(session, req.id, "Client", [("b.pdf", b"same")])  # an arrival
    link = services.create_client_token(session, eng.id, "Priya")
    other_link = services.create_client_token(session, eng.id, "Priya", owner_person="Owner One")
    ids = {"e": eng.id, "r": req.id, "t": link.id}
    sub_id = services.get_request(session, req.id).submissions[0].id
    file_id = services.get_request(session, req.id).submissions[0].files[0].id

    services.close_engagement(session, eng.id, "Partner")
    session.expire_all()
    assert session.get(ClientToken, link.id).status() == "revoked" and session.get(ClientToken, other_link.id).status() == "revoked", \
        "closing withdraws every client link"
    assert client.get(f"/c/{link.token}").status_code == 410

    inserts = {
        "request": "INSERT INTO request (engagement_id, ref, document, significance, priority, owner_person, created_by, created_at) "
                   "VALUES (:e, 'REQ-X-1', 'd', 'Routine', 3, 'o', 'c', now())",
        "request_event": "INSERT INTO request_event (request_id, seq, event_type, actor, at, payload) "
                         "VALUES (:r, 99, 'NOTE', 'x', now(), CAST('{}' AS jsonb))",
        "submission": "INSERT INTO submission (request_id, attempt_number, received_at, channel, outcome) "
                      "VALUES (:r, 9, now(), 'Email', 'pending')",
        "submission_file": "INSERT INTO submission_file (submission_id, request_id, filename, content_hash, file_bytes_size, "
                           "storage_path, page_order, retention_basis) VALUES (:s, :r, 'f', 'h', 1, 'p', 9, 'none')",
        "arrival": "INSERT INTO arrival (submission_file_id, arrived_at, channel) VALUES (:f, now(), 'Email')",
        "chase": 'INSERT INTO chase (request_id, at, channel, "by", delivered, opened) VALUES (:r, now(), \'Email\', \'x\', false, false)',
        "client_token": "INSERT INTO client_token (engagement_id, token, expires_at, created_by, created_at) "
                        "VALUES (:e, 'sneaky', now() + interval '1 day', 'x', now())",
    }
    params = {**ids, "s": sub_id, "f": file_id}
    session.rollback()
    for table, statement in inserts.items():
        with pytest.raises(DBAPIError, match="closed"):
            with engine_db.begin() as conn:
                conn.execute(text(statement), params)
    for statement in ("UPDATE client_token SET revoked_at = NULL WHERE id = :t",
                      "UPDATE engagement SET status = 'Active' WHERE id = :e",
                      "UPDATE request_event SET actor = 'x' WHERE request_id = :r"):
        with pytest.raises(DBAPIError):
            with engine_db.begin() as conn:
                conn.execute(text(statement), params)
    # and the application says so in words
    for attempt in (lambda: services.create_client_token(session, eng.id, "P"),
                    lambda: services.create_request(session, engagement_id=eng.id, document="n", owner_person="o", actor="a"),
                    lambda: services.record_submission(session, req.id, "C", [("c.pdf", b"same")])):
        with pytest.raises(services.LedgerError):
            attempt()


def test_content_hash_dedup_is_one_engine_for_every_channel(session, wa, make_engagement, make_request, client):
    eng = _wa_eng(make_engagement, wa)
    req = make_request(eng, "Bank statement")
    token = services.create_client_token(session, eng.id, "Priya").token
    data = b"%PDF-1.4 the very same statement"

    assert client.post(f"/c/{token}/requests/{req.id}/upload", files={"files": ("portal.pdf", data)}).status_code == 200
    services.record_submission(session, req.id, "Staff", [("emailed-copy.pdf", data)], channel="Email", submitted_by="Ravi (by email)")
    client.post(f"/c/{token}/requests/{req.id}/upload", files={"files": ("again.pdf", data)})
    out = services.receive_whatsapp(session, phone=wa.c, text=req.ref, media=[("whatsapp-copy.pdf", "application/pdf", data)],
                                    received_at=utcnow() + timedelta(seconds=5), final=True)
    assert out["status"] == "duplicate"

    got = services.get_request(session, req.id)
    assert len(got.submissions) == 1 and len(got.submissions[0].files) == 1, "one submission, one stored file"
    f = got.submissions[0].files[0]
    assert [a.channel for a in f.arrivals] == ["Email", "Tokenised link", "WhatsApp"], "every re-send is an arrival, whatever the channel"
    assert [e.event_type for e in got.events].count("ARRIVAL_RECORDED") == 3
    assert [e.event_type for e in got.events].count("SUBMITTED") == 1


def test_carry_forward_is_explicit_linked_and_never_a_blind_copy(session, make_engagement):
    client_name = f"Carry Audit {uuid.uuid4().hex[:6]}"
    prior = make_engagement(client_name=client_name, period_from=date(2024, 4, 1), period_to=date(2025, 3, 31))
    services.generate_requests(session, engagement_id=prior.id, engagement_type="Statutory audit", actor="P",
                               default_owner="Old Owner (Accountant)")
    p = {r.document: r for r in services.engagement_requests(session, prior.id)}
    tb, bank = p["Trial balance as at 31 March 2025"], p["Bank statements - all current accounts"]
    for r in (tb, bank):
        services.append_event(session, r.id, "SENT", "P", channel="Email")
    sub = services.record_submission(session, tb.id, "C", [("tb.xlsx", b"tb")])["submission"]
    services.review_submission(session, sub.id, "accept", "R")
    services.append_event(session, bank.id, "WAIVED", "Partner", payload={"reason": "confirmed directly"})
    services.close_engagement(session, prior.id, "Partner")
    prior_events = {r.id: len(services.get_request(session, r.id).events) for r in services.engagement_requests(session, prior.id)}

    # without the explicit choice nothing is linked or tagged
    plain = make_engagement(client_name=client_name)
    services.generate_requests(session, engagement_id=plain.id, engagement_type="Statutory audit", actor="P", default_owner="X (Acc)")
    assert all(r.carried_from_request_id is None and "carried_forward" not in r.events[0].payload
               for r in services.engagement_requests(session, plain.id))

    # with it: the user picks the items; waived-last-year items start unticked; nothing is copied blindly
    current = make_engagement(client_name=client_name)
    plan = services.build_requirement_list(session, current.id, "Statutory audit", carry_forward=True)
    rows = {r["item"].document: r for r in plan["rows"] if r["status"] == "create"}
    assert rows["Bank statements - all current accounts"]["default_include"] is False
    assert rows["Trial balance as at {period_end}"]["default_include"] is True
    only_tb = {rows["Trial balance as at {period_end}"]["item"].id: {"include": True, "owner_person": "", "significance": "Material", "priority": 1}}
    out = services.generate_requests(session, engagement_id=current.id, engagement_type="Statutory audit", actor="P",
                                     default_owner="Fallback (Acc)", selections=only_tb, carry_forward=True)
    assert [c["document"] for c in out["created"]] == ["Trial balance as at 31 March 2026"], "only what was selected"
    new = services.get_request(session, out["created"][0]["id"])
    assert new.carried_from_request_id == tb.id and new.ref != tb.ref and new.engagement_id == current.id
    assert new.state == "Drafted" and len(new.events) == 1 and new.submissions == [], "a fresh request, not last year's history"
    assert new.events[0].payload["carried_forward"].startswith("Same as last year")
    # last year is untouched
    assert services.get_request(session, tb.id).state == "Accepted" and services.get_request(session, bank.id).state == "Waived"
    assert {r.id: len(services.get_request(session, r.id).events) for r in services.engagement_requests(session, prior.id)} == prior_events


def test_eqr_readiness_and_unfulfilled_apis_and_report_effects(session, make_engagement, make_request, client):
    eng = make_engagement()
    material = make_request(eng, "Trial balance", significance="Material")
    routine = make_request(eng, "Board minutes")
    never_sent = make_request(eng, "Draft only", significance="Material", send=False)

    r = client.get(f"/api/engagements/{eng.id}/eqr-readiness").json()
    assert r["blocked"] and not r["passed"] and r["message"].startswith("BLOCKED: 2 material requests")
    assert {b["ref"] for b in r["blockers"]} == {material.ref, never_sent.ref}
    chip = client.get(f"/?eng={eng.id}").text
    assert 'id="eqr-chip"' in chip and "EQR readiness: BLOCKED" in chip

    services.close_engagement(session, eng.id, "Partner")
    out = client.get(f"/api/engagements/{eng.id}/unfulfilled").json()
    assert out["frozen"] and out["counts"]["unfulfilled"] == 3 and out["report_date"]
    effects = {i["ref"]: i["effect"] for i in out["items"]}
    assert effects == {material.ref: "scope", routine.ref: "representation", never_sent.ref: "nothing"}
    assert out["effects"] == {"scope": 1, "representation": 1, "nothing": 1}
    page = client.get(f"/engagements/{eng.id}/close-out").text
    for label in ("Scope limitation to consider", "Representation to obtain", "Nothing", "What it does to the report"):
        assert label in page
    assert client.get(f"/api/engagements/{eng.id}/eqr-readiness").json()["message"].startswith("BLOCKED: 2 material")

    # nothing material outstanding: the gate passes
    ok = make_engagement()
    make_request(ok, "Minutes only")
    assert client.get(f"/api/engagements/{ok.id}/eqr-readiness").json()["passed"] is True
    assert client.get(f"/api/engagements/{ok.id}/unfulfilled").json()["frozen"] is False
    assert client.get("/api/engagements/999999/eqr-readiness").status_code == 404


def test_the_firm_maintains_templates_beyond_wording(session, make_engagement, client):
    etype = "Bank audit"
    v1 = services.get_template(session, etype)
    first = next(i for i in v1.items if i.document == "Sanction letter and loan terms")
    drop = next(i for i in v1.items if i.document == "Insurance policies covering stock and assets")
    snapshot = [(i.document, i.label_text, i.significance_default, i.priority_default, i.condition_expr) for i in v1.items]

    for bad, msg in [
        (dict(item_edits={first.id: {"condition": "__import__('os').system('x')"}}), "."),
        (dict(item_edits={first.id: {"priority": 9}}), "priority"),
        (dict(item_edits={first.id: {"significance": "Huge"}}), "significance"),
        (dict(additions=[{"document": "insurance POLICIES covering stock and assets"}]), "already in this template"),
        (dict(additions=[{"document": "Fresh", "condition": "no_such_flag"}]), "."),
        (dict(removals={i.id for i in v1.items}), "at least one item"),
        (dict(removals={10 ** 9}), "not part of the latest"),
        (dict(), "nothing to publish"),
    ]:
        with pytest.raises(services.LedgerError, match=msg):
            services.publish_template_version(session, etype, "Priya", **bad)

    v2 = services.publish_template_version(
        session, etype, "Priya", {}, "Reworked for stock-heavy borrowers",
        item_edits={first.id: {"significance": "Material", "priority": 1, "condition": "has_inventory and has_borrowings"}},
        removals={drop.id},
        additions=[{"document": "Insurance schedule and renewal receipts", "label": "Your current insurance schedule",
                    "significance": "Routine", "priority": 4, "condition": "has_fixed_assets", "period": "none"}],
    )
    assert v2.version == v1.version + 1 and v2.change_note == "Reworked for stock-heavy borrowers"
    docs = [i.document for i in v2.items]
    assert drop.document not in docs and docs[-1] == "Insurance schedule and renewal receipts" and len(docs) == len(v1.items)
    assert [i.position for i in v2.items] == list(range(1, len(docs) + 1))
    edited = next(i for i in v2.items if i.document == first.document)
    assert (edited.significance_default, edited.priority_default, edited.condition_expr) == ("Material", 1, "has_inventory and has_borrowings")
    added = v2.items[-1]
    assert (added.label_text, added.priority_default, added.condition_expr, added.period_mode) == \
        ("Your current insurance schedule", 4, "has_fixed_assets", "none")
    session.expire_all()
    assert [(i.document, i.label_text, i.significance_default, i.priority_default, i.condition_expr)
            for i in services.get_template(session, etype, v1.version).items] == snapshot, "v1 is untouched"

    # new engagements use the new list; entity conditioning applies to the edited and added items
    heavy = make_engagement(engagement_type=etype, client_name="Heavy", profile={"has_inventory": True, "has_borrowings": True, "has_fixed_assets": True})
    services.generate_requests(session, engagement_id=heavy.id, engagement_type=etype, actor="P", default_owner="O (Acc)")
    made = {r.document: r for r in services.engagement_requests(session, heavy.id)}
    assert made[first.document].significance == "Material" and made[first.document].priority == 1
    assert drop.document not in made and "Insurance schedule and renewal receipts" in made
    light = make_engagement(engagement_type=etype, client_name="Light", profile={"has_borrowings": True})
    res = services.generate_requests(session, engagement_id=light.id, engagement_type=etype, actor="P", default_owner="O (Acc)")
    assert first.document not in {r.document for r in services.engagement_requests(session, light.id)}
    assert any(s["document"] == first.document and s["basis"].startswith("Suppressed:") for s in res["suppressed"])

    # the manager screen and its endpoint
    session.rollback()
    page = client.get(f"/templates/manager?type={etype}")
    assert "Add an item" in page.text and 'name="remove"' in page.text and 'name="condition"' in page.text
    old = client.get(f"/templates/manager?type={etype}&version={v1.version}")
    assert "read-only" in old.text and 'name="remove"' not in old.text
    latest = services.get_template(session, etype)
    ids = [i.id for i in latest.items]
    form = {"type": etype, "actor": "Priya", "item_id": ids, "label": [i.label_text or "" for i in latest.items],
            "significance": [i.significance_default for i in latest.items], "priority": [str(i.priority_default) for i in latest.items],
            "condition": [i.condition_expr or "" for i in latest.items], "add_document": ["Via the form"], "add_label": ["Typed in the manager"],
            "add_significance": ["Material"], "add_priority": ["2"], "add_condition": [""], "add_period": ["engagement"]}
    res = client.post("/templates/publish", data=form)
    assert res.status_code == 200 and res.json()["version"] == v2.version + 1
    session.expire_all()
    newest = services.get_template(session, etype)
    form2 = {"type": etype, "actor": "Priya", "item_id": [i.id for i in newest.items], "label": [i.label_text or "" for i in newest.items],
             "significance": [i.significance_default for i in newest.items], "priority": [str(i.priority_default) for i in newest.items],
             "condition": [i.condition_expr or "" for i in newest.items]}
    nothing = client.post("/templates/publish", data=form2)
    assert nothing.status_code == 400 and "nothing to publish" in nothing.json()["error"]
    assert client.post("/templates/publish", data={**form2, "add_document": ["Bad"], "add_condition": ["x y z"]}).status_code == 400
