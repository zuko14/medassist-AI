"""Cases and the coordinator queue (W7, ADR-CC-14; TEST-PLAN P7).

One open case per (person, enrollment, reason) — seen again raises the tier, never duplicates; the SLA counts only
the clinic's reply hours; a breached case is escalated once; transitions are compare-and-set (409 for the loser);
'verified' can never be chosen by staff (X2); notes go to the append-only event log.
"""

import asyncio
from datetime import datetime

import pytest

from app.services import cc_cases
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

IST = cc_cases.IST
RH = {"days": "Mon-Sat", "from": "09:00", "to": "18:00"}


@pytest.mark.parametrize("start,hours,want", [
    ("2026-11-25 10:00", 2, "2026-11-25 12:00"),             # Wed, inside hours
    ("2026-11-25 17:30", 2, "2026-11-26 10:30"),             # spills into Thursday
    ("2026-11-28 17:00", 2, "2026-11-30 10:00"),             # Sat → Mon (Sunday closed)
    ("2026-11-29 11:00", 1, "2026-11-30 10:00"),             # opened on Sunday
    ("2026-11-25 06:00", 16, "2026-11-26 16:00"),            # before opening; 9 h Wed + 7 h Thu
])
def test_working_deadline(start, hours, want):
    t = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    assert cc_cases.working_deadline(t, hours, RH).strftime("%Y-%m-%d %H:%M") == want
    assert cc_cases.working_deadline(t, hours, {"days": "Mon,Wed", "from": "09:00", "to": "18:00"}).strftime("%a") in ("Mon", "Wed")


def _clinic(care):
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _open(care, e, pid, tier="watch", reason="t2dm:a1c_above_target"):
    return asyncio.run(cc_cases.open_case(_clinic(care), patient_id=pid, enrollment_id=e["id"], reason=reason, tier=tier))


def test_one_open_case_per_reason_and_tier_only_rises(care):  # noqa: F811
    e, pid = care.enroll()
    a = _open(care, e, pid, "watch")
    assert _open(care, e, pid, "routine") == a
    assert _q(care.conn, "SELECT tier FROM cc_cases WHERE id = %s", a) == "watch"
    assert _open(care, e, pid, "urgent") == a
    assert _q(care.conn, "SELECT tier FROM cc_cases WHERE id = %s", a) == "urgent"
    assert _q(care.conn, "SELECT count(*) FROM cc_cases WHERE enrollment_id = %s", e["id"]) == 1
    kinds = _q(care.conn, "SELECT string_agg(kind, ',' ORDER BY created_at) FROM cc_case_events WHERE case_id = %s", a)
    assert kinds == "opened,reason_seen_again,reason_seen_again"
    assert _open(care, e, pid, "watch", "t2dm:missed_doses") != a                         # another reason: its own case


def test_queue_order_and_coordinator_transitions(care):  # noqa: F811
    api = care.api
    e, pid = care.enroll()
    w = _open(care, e, pid, "watch")
    u = _open(care, e, pid, "urgent", "t2dm:hypo")
    api.as_(care.u.coord)
    cases = api.call("GET", "/admin/cc/cases", care.clinic).json()["cases"]
    assert [c["id"] for c in cases if c["id"] in (w, u)] == [u, w]                        # urgent first
    assert cases[0]["phone_masked"].startswith("+91") and "XXXX" in cases[0]["phone_masked"]
    stale = cases[0]["updated_at"]
    r = api.call("POST", f"/admin/cc/cases/{u}/acknowledge", care.clinic, {"expected_updated_at": stale})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged" and r.json()["assigned_to_name"] == "coord"
    r2 = api.call("POST", f"/admin/cc/cases/{u}/assign", care.clinic, {"expected_updated_at": stale})
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "cc_conflict"     # a second editor loses
    assert api.call("POST", f"/admin/cc/cases/{u}/acknowledge", care.clinic, {}).status_code == 409
    assert api.call("POST", f"/admin/cc/cases/{u}/note", care.clinic, {"note": "Called, no answer"}).status_code == 200
    assert api.call("POST", f"/admin/cc/cases/{u}/note", care.clinic, {"note": " "}).status_code == 422
    assert api.call("POST", f"/admin/cc/cases/{u}/request-doctor", care.clinic, {}).json()["status"] == "awaiting_doctor"
    api.as_(care.u.doctor)
    inbox = api.call("GET", "/admin/cc/inbox", care.clinic).json()["cases"]
    assert [c["id"] for c in inbox] == [u] and inbox[0]["mine"] is True and inbox[0]["protocol_code"] == "t2dm"
    api.as_(care.u.coord)
    assert api.call("POST", f"/admin/cc/cases/{w}/close", care.clinic, {}).status_code == 422
    assert api.call("POST", f"/admin/cc/cases/{w}/close", care.clinic, {"closed_reason": "verified"}).status_code == 422
    r = api.call("POST", f"/admin/cc/cases/{w}/close", care.clinic, {"closed_reason": "unreachable"})
    assert r.status_code == 200 and r.json()["closed_reason"] == "unreachable"
    assert api.call("POST", f"/admin/cc/cases/{w}/escalate", care.clinic, {}).status_code == 409
    detail = api.call("GET", f"/admin/cc/cases/{u}", care.clinic).json()
    assert [ev["kind"] for ev in detail["events"]] == ["opened", "assigned", "note", "status_change"]
    api.as_(care.u.nurse)
    assert api.call("POST", f"/admin/cc/cases/{u}/note", care.clinic, {"note": "x"}).status_code == 403


def test_sla_sweep_escalates_once(care):  # noqa: F811
    e, pid = care.enroll()
    c = _open(care, e, pid, "urgent")
    _q(care.conn, "UPDATE cc_cases SET due_by = now() - interval '1 minute' WHERE id = %s", c)
    clinic = _clinic(care)
    assert asyncio.run(cc_cases.sla_sweep(clinic)) == 1
    assert asyncio.run(cc_cases.sla_sweep(clinic)) == 0
    assert _q(care.conn, "SELECT escalated_at IS NOT NULL FROM cc_cases WHERE id = %s", c)
    assert _q(care.conn, "SELECT count(*) FROM admin_notifications WHERE clinic_id = %s AND title LIKE '[Care] Cases past%%'",
              care.clinic) == 1
    care.api.as_(care.u.coord)
    row = next(x for x in care.api.call("GET", "/admin/cc/cases", care.clinic).json()["cases"] if x["id"] == c)
    assert row["sla_breached"] is True
