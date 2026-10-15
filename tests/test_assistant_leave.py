"""KK-P4-02: PB-01 doctor leave (playbooks-spec PB-01; KK-FR-040..044; owner decisions 2026-10-14).

- The bookings a leave hits are selected by ONE function, database.leave_affected(),
  shared by the 08:00 sweep and Kriya Ketu's preview, so the preview count is the
  sweep's selection by construction (KK-FR-040).
- POST /admin/leaves already cancels today's/tomorrow's bookings itself (KK-P0-01), so
  appointment.cancel_bulk is not built (owner: dropped). Such a leave needs an explicit
  tick naming the number of bookings (owner: R2 + tick).
- An ambiguous doctor is never auto-picked (KK-FR-043); dates already on leave are
  skipped and reported (KK-FR-044).
"""

import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import database
from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)          # 09:00 IST
TODAY, TOMORROW = NOW.date(), NOW.date() + timedelta(days=1)
LATER = date(2026, 10, 20)
RAO, REDDY, ANITHA = "Dr. Srinivas Rao", "Dr. Srinivas Reddy", "Dr. Anitha"
DOCTORS = [
    {"id": "d1", "name": RAO, "morning_slots": ["09:00", "09:30", "10:00"], "evening_slots": ["17:00", "17:30"]},
    {"id": "d2", "name": REDDY, "morning_slots": ["09:00"], "evening_slots": ["17:00"]},
    {"id": "d3", "name": ANITHA, "morning_slots": ["10:00"], "evening_slots": ["18:00"]},
]


def _appt(i, day, time, doctor=RAO, status="confirmed", walk_in=False):
    return {"id": f"a{i}", "doctor_name": doctor, "appointment_date": day.isoformat(), "appointment_time": time,
            "status": status, "is_walk_in": walk_in, "patient_name": f"Patient {i}", "booking_ref": f"KR-{i}",
            "patient_phone": "919876500000"}


# ── the one selection (KK-FR-040) ────────────────────────────────────────────

ROWS = [
    _appt(1, LATER, "09:00"), _appt(2, LATER, "17:00"), _appt(3, LATER, "10:00", walk_in=True),
    _appt(4, LATER, "09:30", walk_in=None), _appt(5, LATER, "09:00", status="cancelled"),
    _appt(6, LATER, "09:00", doctor=REDDY), _appt(7, TODAY, "08:30"), _appt(8, TODAY, "11:00"),
    _appt(9, TODAY, None),
]


@pytest.mark.parametrize("leave_date, leave_type, expected", [
    (LATER, "full", ["a1", "a2"]),                 # confirmed, walk-in exactly False, this doctor, that day
    (LATER, "half_morning", ["a1"]),                # only the morning session's slots
    (LATER, "half_evening", ["a2"]),
    (TODAY, "full", ["a8", "a9"]),                 # today: visits already past are left alone
])
def test_leave_affected_is_the_sweeps_selection(leave_date, leave_type, expected):
    leave = {"doctor_name": RAO, "leave_date": leave_date.isoformat(), "leave_type": leave_type}
    selected = [r for r in ROWS if database.leave_query_match(r, leave)]   # what the sweep's query returns
    got = database.leave_affected(selected, leave, DOCTORS[0], NOW)
    assert [a["id"] for a in got] == expected


def test_half_day_with_an_unknown_doctor_cancels_nothing():
    leave = {"doctor_name": RAO, "leave_date": LATER.isoformat(), "leave_type": "half_morning"}
    assert database.leave_affected([r for r in ROWS if database.leave_query_match(r, leave)], leave, None, NOW) == []


def test_the_sweep_uses_the_shared_selection():
    import inspect

    from app.services.scheduler import SchedulerService
    src = inspect.getsource(SchedulerService._cancel_bookings_for_leaves)
    assert "leave_affected(" in src and "doctor_session_slots(" not in src
    # leave_query_match() mirrors exactly the sweep's SQL filter
    for clause in ('.eq("status", "confirmed")', '.eq("is_walk_in", False)', '.eq("doctor_name", leave["doctor_name"])',
                   '.eq("appointment_date", leave["leave_date"])'):
        assert clause in src, clause


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("Dr Rao leave 14 to 16 Oct", (date(2026, 10, 14), date(2026, 10, 16))),
    ("leave from 14 october to 16 october", (date(2026, 10, 14), date(2026, 10, 16))),
    ("Rao 20 se 22 oct tak chutti", (date(2026, 10, 20), date(2026, 10, 22))),
    ("Rao 20 nundi 22 oct varaku selavu", (date(2026, 10, 20), date(2026, 10, 22))),
    ("Dr Rao on leave tomorrow", None),
    ("16 to 14 oct", None),
])
def test_resolve_range(text, expected):
    assert resolve.resolve_range(text, now=NOW) == expected


@pytest.mark.parametrize("text, expected", [
    ("on leave tomorrow morning only", "half_morning"), ("leave evening", "half_evening"),
    ("leave in the afternoon", "half_evening"), ("on leave tomorrow", None),
])
def test_resolve_leave_type(text, expected):
    assert resolve.resolve_leave_type(text) == expected


# ── routing (KK-FR-043) ──────────────────────────────────────────────────────

def _route(text):
    return rules.route(text, doctors=DOCTORS, now=NOW)


def test_create_resolves_the_exact_doctor_name_and_date():
    r = _route("Dr Rao on leave tomorrow")
    assert (r.kind, r.action_id) == ("action", "doctor_leave.create")
    assert r.params == {"doctor": RAO, "date": TOMORROW.isoformat()}


def test_range_and_half_day_fill_their_slots():
    r = _route("Dr Rao on leave 14 to 16 Oct morning only")
    assert r.params == {"doctor": RAO, "date": "2026-10-14", "date_end": "2026-10-16", "leave_type": "half_morning"}


def test_an_ambiguous_doctor_is_never_picked():
    r = _route("Srinivas on leave tomorrow")
    assert r.kind == "clarify" and r.missing == ("doctor",)
    assert sorted(c["id"] for c in r.candidates) == ["d1", "d2"]


def test_an_unknown_doctor_offers_the_whole_list():
    r = _route("Dr Zebedee on leave tomorrow")
    assert r.kind == "clarify" and sorted(c["id"] for c in r.candidates) == ["d1", "d2", "d3"]


@pytest.mark.parametrize("text, expected", [
    ("cancel Dr Rao's leave on 20 oct", "doctor_leave.delete"),
    ("remove the leave for Dr Anitha tomorrow", "doctor_leave.delete"),
    ("bookings affected by Dr Rao leave tomorrow", "doctor_leave.impact"),
    ("డాక్టర్ రావు రేపు సెలవు", "doctor_leave.create"),
    ("Dr Rao kal chutti pe hain", "doctor_leave.create"),
])
def test_leave_actions_route(text, expected):
    r = _route(text)
    assert r.action_id == expected, r


@pytest.mark.parametrize("text, kind", [
    ("how do I add a leave", "help"), ("Dr Rao is not on leave tomorrow", "rejected"),
    ("what if Dr Rao is on leave tomorrow", "rejected"), ("is Dr Rao on leave tomorrow?", "help"),
])
def test_questions_negations_and_what_ifs_never_write(text, kind):
    assert _route(text).kind == kind


def test_doctors_are_fetched_only_for_doctor_actions():
    assert rules.wants_doctors("Dr Rao on leave tomorrow")
    assert not rules.wants_doctors("today's appointments")


# ── through the API, against a fake panel with state ─────────────────────────

fake = FastAPI()
STATE: dict = {}


def _reset(appts=(), leaves=(), branch=None, write_ok=True):
    STATE.clear()
    STATE.update(appts=[dict(a) for a in appts], leaves=[dict(x) for x in leaves], branch=branch, posts=[],
                 deletes=[], write_ok=write_ok)


@fake.get("/admin/me")
async def _me():
    return {"username": "desk1", "branch_id": STATE["branch"]}


@fake.get("/admin/doctors")
async def _doctors():
    return DOCTORS


@fake.get("/admin/appointments")
async def _appointments(request: Request):
    q = request.query_params
    rows = [a for a in STATE["appts"] if q["date_from"] <= a["appointment_date"] <= q["date_to"]
            and (not q.get("status") or a["status"] == q["status"])]
    off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
    return {"appointments": rows[off: off + lim], "total": len(rows)}


@fake.get("/admin/leaves")
async def _leaves(doctor: str = None):
    return [x for x in STATE["leaves"] if not doctor or x["doctor_name"] == doctor]


@fake.post("/admin/leaves")
async def _add_leave(request: Request):
    body = await request.json()
    STATE["posts"].append(body)
    if STATE["write_ok"]:
        start = date.fromisoformat(body["leave_date"])
        end = date.fromisoformat(body.get("end_date") or body["leave_date"])
        have = {x["leave_date"] for x in STATE["leaves"] if x["doctor_name"] == body["doctor_name"]}
        while start <= end:
            if start.isoformat() not in have:
                STATE["leaves"].append({"id": str(uuid.uuid4()), "doctor_name": body["doctor_name"],
                                        "leave_date": start.isoformat(), "leave_type": body["leave_type"]})
            start += timedelta(days=1)
    return {"status": "success"}


@fake.delete("/admin/leaves/{leave_id}")
async def _del_leave(leave_id: str):
    STATE["deletes"].append(leave_id)
    if not any(x["id"] == leave_id for x in STATE["leaves"]):
        raise HTTPException(status_code=404)
    STATE["leaves"] = [x for x in STATE["leaves"] if x["id"] != leave_id]
    return {"status": "deleted"}


client = TestClient(app)


@pytest.fixture
def leave(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "leaves"}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm",
                       json={"plan_hash": out["plan_hash"], **extra})


def _card(out):
    return out["cards"][0]


def test_future_leave_previews_then_writes_once_and_verifies(db, leave):  # noqa: F811
    _reset(appts=[_appt(1, LATER, "09:00"), _appt(2, LATER, "17:00"), _appt(3, LATER, "10:00", walk_in=True)])
    out = _ask("Dr Rao on leave on 20 october")
    assert out["state"] == "NEEDS_CONFIRMATION" and STATE["posts"] == [], out
    card = _card(out)
    text = json.dumps(card, ensure_ascii=False)
    assert "2 confirmed booking" in text and "08:00" in text          # KK-FR-041: count + consequence
    assert card["requires_ack"] is False
    assert "919876500000" not in json.dumps(db.tables["assistant_runs"])
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert STATE["posts"] == [{"doctor_name": RAO, "leave_date": "2026-10-20", "end_date": None,
                               "leave_type": "full"}]


def test_a_leave_for_tomorrow_needs_a_tick_naming_the_bookings(db, leave):  # noqa: F811
    _reset(appts=[_appt(1, TOMORROW, "09:00"), _appt(2, TOMORROW, "10:00")])
    out = _ask("Dr Rao on leave tomorrow")
    card = _card(out)
    assert card["requires_ack"] is True and "2 booking" in card["ack_text"]
    assert any("now" in w for w in card["warnings"])                 # KK-FR-042 (D-01, as fixed by KK-P0-01)
    r = _confirm(out)
    assert r.status_code == 422 and "2 booking" in r.json()["detail"]["message"] and STATE["posts"] == []
    assert _confirm(out, ack_duplicate=True).json()["state"] == "COMPLETED_VERIFIED"


def test_dates_already_on_leave_are_skipped_and_reported(db, leave):  # noqa: F811
    _reset(leaves=[{"id": "L1", "doctor_name": RAO, "leave_date": "2026-10-20", "leave_type": "full"}])
    out = _ask("Dr Rao on leave 20 to 22 Oct")
    text = json.dumps(_card(out), ensure_ascii=False)
    assert "Already on leave" in text and "20 Oct" in text and _card(out)["affected"] == 2


def test_nothing_to_add_is_refused_without_a_confirm(db, leave):  # noqa: F811
    _reset(leaves=[{"id": "L1", "doctor_name": RAO, "leave_date": "2026-10-20", "leave_type": "full"}])
    out = _ask("Dr Rao on leave on 20 october")
    assert (out["state"], out["error_code"]) == ("REJECTED", "nothing_to_do") and "already" in out["message"]
    assert STATE["posts"] == []


def test_more_than_31_days_is_refused(db, leave):  # noqa: F811
    out = _ask("Dr Rao on leave 1 nov to 15 dec")
    assert (out["state"], out["error_code"]) == ("REJECTED", "too_many")


def test_half_day_counts_only_that_sessions_bookings(db, leave):  # noqa: F811
    _reset(appts=[_appt(1, LATER, "09:00"), _appt(2, LATER, "17:00"), _appt(3, LATER, "17:30")])
    out = _ask("Dr Rao on leave on 20 october evening")
    assert "2 confirmed booking" in json.dumps(_card(out), ensure_ascii=False)


def test_a_pinned_user_is_told_the_count_is_their_branch_only(db, leave):  # noqa: F811
    _reset(branch="b-1")
    out = _ask("Dr Rao on leave on 20 october")
    assert any("branch" in w for w in _card(out)["warnings"])


def test_a_booking_made_after_the_preview_means_a_new_preview(db, leave):  # noqa: F811
    _reset(appts=[_appt(1, LATER, "09:00")])
    out = _ask("Dr Rao on leave on 20 october")
    STATE["appts"].append(_appt(2, LATER, "10:00"))
    again = _confirm(out).json()
    assert (again["state"], again["error_code"]) == ("NEEDS_CONFIRMATION", "plan_changed") and STATE["posts"] == []


def test_a_write_the_read_back_cannot_see_is_unknown_not_done(db, leave):  # noqa: F811
    _reset(write_ok=False)
    out = _ask("Dr Rao on leave on 20 october")
    assert _confirm(out).json()["state"] == "UNKNOWN_OUTCOME" and len(STATE["posts"]) == 1


def test_impact_is_a_read_that_writes_nothing(db, leave):  # noqa: F811
    _reset(appts=[_appt(1, TOMORROW, "09:00"), _appt(2, TOMORROW, "17:00", doctor=ANITHA)])
    out = _ask("bookings affected by Dr Rao leave tomorrow")
    assert out["state"] == "COMPLETED_VERIFIED" and out["action_id"] == "doctor_leave.impact", out
    assert _card(out)["total"] == 1 and STATE["posts"] == []


def test_delete_removes_that_days_leave_and_verifies(db, leave):  # noqa: F811
    _reset(leaves=[{"id": "L1", "doctor_name": RAO, "leave_date": "2026-10-20", "leave_type": "full"},
                   {"id": "L2", "doctor_name": RAO, "leave_date": "2026-10-21", "leave_type": "full"}])
    out = _ask("cancel Dr Rao's leave on 20 oct")
    card = _card(out)
    assert card["affected"] == 1 and any("not restored" in w for w in card["warnings"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert STATE["deletes"] == ["L1"] and [x["id"] for x in STATE["leaves"]] == ["L2"]


def test_delete_with_no_leave_is_refused(db, leave):  # noqa: F811
    out = _ask("cancel Dr Rao's leave on 20 oct")
    assert (out["state"], out["error_code"]) == ("REJECTED", "nothing_to_do") and STATE["deletes"] == []


def test_staff_without_the_leave_permission_is_refused(db, leave):  # noqa: F811
    leave["user"] = _user()
    out = _ask("Dr Rao on leave on 20 october")
    assert out["state"] == "REJECTED" and "Doctor Leaves Create" in out["message"] and STATE["posts"] == []


def test_registry_shape():
    create, delete, impact = (REGISTRY[a] for a in ("doctor_leave.create", "doctor_leave.delete",
                                                     "doctor_leave.impact"))
    assert (create.risk, create.permission, create.endpoint) == ("R2", "DOCTOR_LEAVES_CREATE", ("POST", "/admin/leaves"))
    assert (delete.risk, delete.permission) == ("R2", "DOCTOR_LEAVES_DELETE")
    assert delete.endpoint == ("DELETE", "/admin/leaves/{leave_id}")
    assert impact.risk == "R0" and create.max_items == 31
    assert "appointment.cancel_bulk" not in REGISTRY                 # owner: dropped (create_leave does it)


# ── corpus (registry spec §6: >= 5 phrases incl. te-Latin + hi-Latin, >= 2 negatives) ──

@pytest.mark.parametrize("aid", ["doctor_leave.create", "doctor_leave.delete", "doctor_leave.impact"])
def test_corpus_covers_each_leave_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2
