"""Acceptance tests for the ledger's invariants (spec section 12).

Each test builds its own engagement, so the tests are independent and order-free.
"""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import services
from models import AppendOnlyViolation, Arrival, RequestEvent, RequestSuppression


def states(session, eng):
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
