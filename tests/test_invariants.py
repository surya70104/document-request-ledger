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
