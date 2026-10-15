"""KK-P4-11: PB-11 doctor schedule change, doctor create, branch doctor assignment.

- `doctor.schedule` (R2, DOCTORS_UPDATE): one session's hours. The PUT carries the doctor's full current
  shift fields and slot duration with only that session replaced (the endpoint regenerates both shifts
  from what it is sent, and blanks a shift that is missing: D-34), and never a branch field (SCH-02).
  Confirmed bookings from today whose time falls outside the new hours are listed, not moved (SCH-01).
  The slot preview uses the server's own `generate_slots` (SCH-03). Read-back GET /admin/doctors.
- `doctor.create` (R2, DOCTORS_CREATE): name from "add Dr X", fee in rupees, days, hours per session;
  specialization and department from the form. Slot preview = generate_slots. Read-back GET /admin/doctors.
- `branch.doctor_assign` (R2, DOCTOR_BRANCH_ASSIGN): POST /admin/branches/{id}/doctors; read-back
  GET /admin/branches/{id}/doctors.
"""

import uuid
from datetime import datetime, time
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser
from app.utils.helpers import generate_slots
from app.voice.dates import IST
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)
D1 = str(uuid.UUID(int=701))
B1 = str(uuid.UUID(int=801))


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, session, window", [
    ("Dr Anitha's evening is now 5 to 8", "evening", "17:00-20:00"),
    ("Dr Anitha evening hours 5:30 pm to 8:30 pm", "evening", "17:30-20:30"),
    ("Dr Rao morning 9 to 1", "morning", "09:00-13:00"),
    ("change Dr Rao's morning to 8:30 to 12:30", "morning", "08:30-12:30"),
    ("Dr Rao timings 9 to 1", None, "09:00-13:00"),
])
def test_resolve_session_and_window(text, session, window):
    assert resolve.resolve_session(text) == session
    assert resolve.resolve_session_window(text, session or "morning") == window


@pytest.mark.parametrize("text, days", [
    ("Mon-Sat", "Mon,Tue,Wed,Thu,Fri,Sat"), ("monday to friday", "Mon,Tue,Wed,Thu,Fri"),
    ("weekdays", "Mon,Tue,Wed,Thu,Fri"), ("all days", "Mon,Tue,Wed,Thu,Fri,Sat,Sun"),
    ("Mon, Wed and Fri", "Mon,Wed,Fri"), ("no days here", None),
])
def test_resolve_days(text, days):
    assert resolve.resolve_days(text) == days


@pytest.mark.parametrize("text, name", [
    ("Add Dr Kiran Kumar, dermatologist, ₹600, Mon-Sat mornings 9 to 1", "Dr Kiran Kumar"),
    ("add a new doctor Dr. Priya, ₹500", "Dr. Priya"), ("add doctor", None),
])
def test_resolve_new_doctor(text, name):
    assert resolve.resolve_new_doctor(text) == name


@pytest.mark.parametrize("text, branch", [
    ("assign Dr Rao to Kukatpally branch mornings", "kukatpally"),
    ("Dr Rao ni Madhapur branch ki assign cheyyi", "madhapur"), ("assign Dr Rao", None),
])
def test_resolve_branch(text, branch):
    assert resolve.resolve_branch(text) == branch


# ── routing ──────────────────────────────────────────────────────────────────

DOCTORS = [{"id": D1, "name": "Dr. Anitha"}]


@pytest.mark.parametrize("text, action", [
    ("Dr Anitha's evening is now 5 to 8", "doctor.schedule"),
    ("change Dr Anitha's morning hours to 9 to 1", "doctor.schedule"),
    ("Dr Anitha evening timings 5 to 8 ki marchu", "doctor.schedule"),
    ("Dr Anitha ki evening timing 5 se 8 karo", "doctor.schedule"),
    ("Add Dr Kiran Kumar, dermatologist, ₹600, Mon-Sat mornings 9 to 1", "doctor.create"),
    ("new doctor Dr Kiran ₹600 weekdays evenings 5 to 8", "doctor.create"),
    ("assign Dr Anitha to Kukatpally branch mornings", "branch.doctor_assign"),
    ("Dr Anitha ni Kukatpally branch ki assign cheyyi", "branch.doctor_assign"),
    ("Dr Anitha ko Kukatpally branch mein assign karo", "branch.doctor_assign"),
])
def test_routes(text, action):
    r = rules.route(text, doctors=DOCTORS, now=NOW)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _doc(**kw):
    d = {"id": D1, "name": "Dr. Anitha", "specialization": "Dermatology", "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
         "morning_start": "09:00:00", "morning_end": "13:00:00", "evening_start": "17:00:00", "evening_end": "19:00:00",
         "slot_duration_minutes": 15, "consultation_fee": 500, "is_active": True, "branch_id": B1,
         "branch_session": "both"}
    d.update(kw)
    return d


def _reset(doctors=None, bookings=(), branches=None, assigned=()):
    S.clear()
    S.update(doctors=[dict(d) for d in (doctors if doctors is not None else [_doc()])], bookings=list(bookings),
             branches=branches if branches is not None else [{"id": B1, "name": "Kukatpally"}],
             assigned=list(assigned), writes=[])


@fake.get("/admin/me")
async def _me():
    return {"branch_id": None}


@fake.get("/admin/doctors")
async def _doctors():
    return S["doctors"]


@fake.put("/admin/doctors/{did}")
async def _put(did: str, request: Request):
    body = await request.json()
    S["writes"].append(("PUT", did, body))
    row = next(d for d in S["doctors"] if d["id"] == did)
    row.update({k: (v + ":00" if k.endswith(("_start", "_end")) and v else v) for k, v in body.items()})
    return row


@fake.post("/admin/doctors")
async def _create(request: Request):
    body = await request.json()
    S["writes"].append(("POST", body))
    S["doctors"].append({"id": "new-doc", **body})
    return S["doctors"][-1]


@fake.get("/admin/appointments")
async def _appts(request: Request):
    return {"appointments": S["bookings"], "total": len(S["bookings"])}


@fake.get("/admin/branches")
async def _branches():
    return S["branches"]


@fake.get("/admin/branches/{bid}/doctors")
async def _branch_doctors(bid: str):
    return {"doctor_branches": [{"doctor_id": d, "branch_id": bid} for d in S["assigned"]]}


@fake.post("/admin/branches/{bid}/doctors")
async def _assign(bid: str, request: Request):
    body = await request.json()
    S["writes"].append(("ASSIGN", bid, body))
    S["assigned"].append(body["doctor_id"])
    return {"doctor_id": body["doctor_id"], "branch_id": bid, "session": body["session"]}


client = TestClient(app)


def _staff(*perms):
    return AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="FRONT_DESK",
                     permissions=list(perms))


@pytest.fixture
def docs(as_user):  # noqa: F811
    as_user["user"] = _staff("DOCTORS_UPDATE", "DOCTORS_CREATE", "DOCTOR_BRANCH_ASSIGN")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="doctors"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": answers}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def _booking(day, at, name="Ravi"):
    return {"id": str(uuid.uuid4()), "appointment_date": day, "appointment_time": at, "patient_name": name,
            "doctor_name": "Dr. Anitha", "status": "confirmed", "booking_ref": f"KR-{at}"}


def test_sch01_sch02_schedule_change_keeps_the_rest_and_lists_bookings_outside(db, docs):  # noqa: F811
    _reset(bookings=[_booking("2026-10-16", "17:15", "Ravi"), _booking("2026-10-16", "19:30", "Sita"),
                     _booking("2026-10-17", "10:00", "Gita")])
    out = _ask("Dr Anitha's evening is now 6 to 8")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    text = str(out["cards"])
    assert "17:00" in text and "18:00" in text  # old -> new hours
    assert "Ravi" in text and "Sita" not in text and "Gita" not in text  # 17:15 is outside 18:00-20:00
    assert S["writes"] == []
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, did, body), = S["writes"]
    assert did == D1
    assert body == {"morning_start": "09:00", "morning_end": "13:00", "evening_start": "18:00", "evening_end": "20:00",
                    "slot_duration_minutes": 15}
    assert "branch_id" not in body and "branch_session" not in body  # SCH-02


def test_sch03_the_slot_preview_is_the_servers_own(db, docs):  # noqa: F811
    out = _ask("Dr Anitha's evening is now 6 to 8")
    expected = len(generate_slots(time(18), time(20), 15))
    assert f"{expected} slots" in str(out["cards"])


def test_same_hours_is_nothing_to_do(db, docs):  # noqa: F811
    assert _ask("Dr Anitha's evening is now 5 to 7")["error_code"] == "nothing_to_do"


def test_schedule_needs_the_permission(db, docs):  # noqa: F811
    docs["user"] = _staff("DOCTORS_CREATE")
    assert _ask("Dr Anitha's evening is now 6 to 8")["state"] == "REJECTED"


def test_doctor_create_asks_specialization_and_department_then_previews_slots(db, docs):  # noqa: F811
    out = _ask("Add Dr Kiran Kumar, ₹600, Mon-Sat mornings 9 to 1")
    assert out["state"] == "NEEDS_CLARIFICATION", out
    out = _answer(out, specialization="Dermatology", department="Skin")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    n = len(generate_slots(time(9), time(13), 30))
    assert f"{n} slots" in str(out["cards"])  # SCH-03, default 30 minutes
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body == {"name": "Dr Kiran Kumar", "specialization": "Dermatology", "department": "Skin",
                    "consultation_fee": 600, "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
                    "morning_start": "09:00", "morning_end": "13:00", "slot_duration_minutes": 30}


def test_doctor_create_without_hours_is_refused(db, docs):  # noqa: F811
    out = _answer(_ask("Add Dr Kiran Kumar, ₹600, Mon-Sat"), specialization="Dermatology", department="Skin")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_doctor_create_existing_name_is_refused(db, docs):  # noqa: F811
    out = _answer(_ask("Add Dr Anitha, ₹600, Mon-Sat mornings 9 to 1"), specialization="Derm", department="Skin")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_doctor_create_fee_bounds(db, docs):  # noqa: F811
    out = _answer(_ask("Add Dr Kiran, ₹2,00,000, Mon-Sat mornings 9 to 1"), specialization="Derm", department="Skin")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_branch_assign(db, docs):  # noqa: F811
    out = _ask("assign Dr Anitha to Kukatpally branch mornings", page="branches")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("ASSIGN", B1, {"doctor_id": D1, "session": "morning"})]


def test_branch_assign_already_assigned_is_nothing_to_do(db, docs):  # noqa: F811
    _reset(assigned=[D1])
    assert _ask("assign Dr Anitha to Kukatpally branch", page="branches")["error_code"] == "nothing_to_do"


def test_branch_assign_unknown_branch_lists_branches(db, docs):  # noqa: F811
    out = _ask("assign Dr Anitha to Madhapur branch", page="branches")
    assert out["error_code"] == "nothing_to_do" and "Kukatpally" in out["message"]


def test_registered():
    for aid, method, perm in (("doctor.schedule", "PUT", "DOCTORS_UPDATE"), ("doctor.create", "POST", "DOCTORS_CREATE"),
                              ("branch.doctor_assign", "POST", "DOCTOR_BRANCH_ASSIGN")):
        spec = REGISTRY[aid]
        assert (spec.risk, spec.endpoint[0], spec.permission, spec.perm_source) == ("R2", method, perm, "dependency")
