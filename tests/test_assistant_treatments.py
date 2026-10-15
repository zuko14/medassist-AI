"""KK-P4-10: PB-10 treatments (price single/bulk, show/hide, doctor mapping, AI description hand-off)
and PB-15 dental (plan + first sitting, next sitting).

- Treatments: TREATMENTS_MANAGE route dependencies, `specialty_enabled` (the route's own gate).
  Price 1..1 crore (TreatmentUpdate allows 0, but 0 = no price; KK-FR-071 refuses <= 0). Bulk = signed
  percent, typed count, cap 100 (CAT-04). Read-back GET /admin/treatments (CAT-09: status and doctor
  mapping equal the read-back).
- AI description: the executor never stores response bodies, so a draft could not be shown; it is a
  hand-off to the Treatments page's own AI button (CAT-08: nothing is saved by Kriya).
- Dental: DENTAL_PLANS_MANAGE, `dental_plans_enabled`. WhatsApp consent only from the yes/no form
  (never from text). A first sitting is checked against GET /admin/dental/slots (holiday, leave,
  off-day, taken: CAT-10) before the preview; booking itself goes through the endpoint's
  book_appointment rules. Read-back GET /admin/dental/plans (+ /{id}).
"""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser
from app.voice.dates import IST
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)  # Thursday
MON = "2026-10-19"
D_RAO, D_MEERA = str(uuid.UUID(int=901)), str(uuid.UUID(int=902))
DOCTORS = [{"id": D_RAO, "name": "Dr. Srinivas Rao", "is_active": True},
           {"id": D_MEERA, "name": "Dr. Meera Iyer", "is_active": True}]
CLINIC = {"id": CID, "plan": "dental", "features": {"kriya_ketu": True}}


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("change PRP price to ₹5000", "prp"), ("only Dr Meera does PRP", "prp"),
    ("hide LASIK", "lasik"), ("publish chemical peel", "chemical peel"),
    ("raise hydrafacial treatment price by 10%", "hydrafacial"),
])
def test_resolve_treatment(text, expected):
    assert resolve.resolve_treatment(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("hide LASIK", "hide"), ("unpublish LASIK", "hide"), ("publish chemical peel", "show"),
    ("unhide chemical peel", "show"), ("show chemical peel to patients", "show"), ("LASIK", None),
])
def test_resolve_show_hide(text, expected):
    assert resolve.resolve_show_hide(text) == expected


@pytest.mark.parametrize("text, name, treatment, sittings, clock", [
    ("Root canal plan for Suresh Kumar 9876543210, 3 sittings, ₹9000, first sitting Monday at 10:30 with Dr Rao",
     "Suresh Kumar", "root canal", 3, "10:30"),
    ("braces plan for Anitha 98765 43210 12 sittings", "Anitha", "braces", 12, None),
    ("book Suresh's next sitting on Monday at 11 am with Dr Rao", None, None, None, "11:00"),
    ("next sitting for Suresh at 4:30 pm", "Suresh", None, None, "16:30"),
])
def test_dental_resolvers(text, name, treatment, sittings, clock):
    assert resolve.resolve_patient_for(text) == name
    assert resolve.resolve_plan_treatment(text) == treatment
    assert resolve.resolve_sittings(text) == sittings
    assert resolve.resolve_clock(text) == clock


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("change PRP price to ₹5000", "treatment.price"),
    ("HydraFacial price from 2500 rupees ki marchu", "treatment.price"),
    ("PRP ka rate 5000 rupees badlo", "treatment.price"),
    ("raise all laser treatments by 5%", "treatment.price_bulk"),
    ("anni treatments 5% penchu", "treatment.price_bulk"),
    ("sab treatments ke daam 5% badhao", "treatment.price_bulk"),
    ("hide LASIK", "treatments.status"),
    ("publish chemical peel", "treatments.status"),
    ("LASIK treatment hide cheyyi", "treatments.status"),
    ("LASIK treatment chupao", "treatments.status"),
    ("only Dr Meera does PRP", "treatment.doctors"),
    ("PRP only Dr Meera chestaru", "treatment.doctors"),
    ("PRP sirf Dr Meera karti hain", "treatment.doctors"),
    ("Root canal plan for Suresh 9876543210, 3 sittings, ₹9000, first sitting Monday at 10:30 with Dr Rao",
     "dental.plan_create"),
    ("braces plan for Anitha 9876543210 12 sittings", "dental.plan_create"),
    ("book Suresh's next sitting on Monday at 11 am with Dr Rao", "dental.sitting_book"),
    ("Suresh ki next sitting Monday 11 am Dr Rao tho book cheyyi", "dental.sitting_book"),
    ("Suresh ki agli sitting Monday 11 am Dr Rao ke saath book karo", "dental.sitting_book"),
])
def test_routes(text, action):
    r = rules.route(text, ui_page="treatments" if action.startswith("treatment") else "dentalplans",
                    doctors=DOCTORS, now=NOW)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


def test_on_the_treatments_page_a_price_change_is_a_treatment():
    assert rules.route("change PRP price to ₹5000", ui_page="treatments").action_id == "treatment.price"
    assert rules.route("change CBC price to ₹350", ui_page="labtests").action_id == "lab_test.price"


@pytest.mark.parametrize("text, page, button", [
    ("write a description for microneedling", "treatments", "Generate all 3 languages"),
    ("write a description for the CBC test", "labtests", "Add Lab Test"),
])
def test_ai_description_is_a_hand_off_to_the_pages_button(text, page, button):
    r = rules.route(text, ui_page=page)
    assert (r.kind, r.page, r.help.button) == ("help", page, button), r


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}
T = [str(uuid.UUID(int=i)) for i in range(1, 300)]


def _tr(i, name, price=1000, active=True, category="Skin", doctors=()):
    return {"id": T[i], "name": name, "price_from_rupees": price, "is_active": active, "category": category,
            "doctor_ids": list(doctors)}


def _reset(treatments=(), slots=("10:00", "10:30", "11:00"), reason=None, plans=(), sitting_ok=True):
    S.clear()
    S.update(treatments=[dict(t) for t in treatments], slots=list(slots), reason=reason, writes=[],
             plans=[dict(p) for p in plans], sitting_ok=sitting_ok)


@fake.get("/admin/me")
async def _me():
    return {"branch_id": None}


@fake.get("/admin/doctors")
async def _doctors():
    return DOCTORS


@fake.get("/admin/treatments")
async def _list():
    return S["treatments"]


@fake.put("/admin/treatments/{tid}")
async def _put(tid: str, request: Request):
    body = await request.json()
    S["writes"].append(("PUT", tid, body))
    row = next(t for t in S["treatments"] if t["id"] == tid)
    row.update(body)
    return row


@fake.post("/admin/treatments/status")
async def _status(request: Request):
    body = await request.json()
    S["writes"].append(("STATUS", body))
    for t in S["treatments"]:
        if t["id"] in body["treatment_ids"]:
            t["is_active"] = body["is_active"]
    return {"updated": len(body["treatment_ids"])}


@fake.put("/admin/treatments/{tid}/doctors")
async def _docs(tid: str, request: Request):
    body = await request.json()
    S["writes"].append(("DOCTORS", tid, body))
    next(t for t in S["treatments"] if t["id"] == tid)["doctor_ids"] = body["doctor_ids"]
    return {"treatment_id": tid, "doctor_ids": body["doctor_ids"]}


@fake.get("/admin/dental/slots")
async def _slots(doctor_id: str, date: str):
    S.setdefault("slot_reads", []).append((doctor_id, date))
    return {"success": True, "slots": S["slots"], "reason": S["reason"]}


@fake.get("/admin/dental/plans")
async def _plans(q: str = "", status: str = None):
    rows = [p for p in S["plans"] if q.lower() in p["patient_name"].lower() or q in p["patient_phone"]]
    if status:
        rows = [p for p in rows if p["status"] == status]
    return {"success": True, "plans": rows, "total": len(rows)}


@fake.get("/admin/dental/plans/{pid}")
async def _plan(pid: str):
    p = next(p for p in S["plans"] if p["id"] == pid)
    return {"success": True, "plan": p, "sittings": p.get("sittings", [])}


@fake.post("/admin/dental/plans")
async def _create_plan(request: Request):
    body = await request.json()
    S["writes"].append(("PLAN", body))
    plan = {"id": "plan-new", "patient_name": body["patient_name"], "patient_phone": body["patient_phone"],
            "treatment_name": "Root Canal", "status": "active", "sittings": []}
    if body.get("first_sitting") and S["sitting_ok"]:
        fs = body["first_sitting"]
        plan["sittings"].append({"appointment_date": fs["date"], "appointment_time": fs["time"] + ":00",
                                 "status": "confirmed", "sitting_number": 1})
    S["plans"].append(plan)
    return {"success": True, "plan": plan}


@fake.post("/admin/dental/plans/{pid}/sittings")
async def _book(pid: str, request: Request):
    body = await request.json()
    S["writes"].append(("SITTING", pid, body))
    p = next(p for p in S["plans"] if p["id"] == pid)
    p.setdefault("sittings", []).append({"appointment_date": body["date"], "appointment_time": body["time"] + ":00",
                                         "status": "confirmed", "sitting_number": len(p["sittings"]) + 1})
    return {"success": True, "sitting": p["sittings"][-1]}


client = TestClient(app)


def _staff(*perms):
    return AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="FRONT_DESK",
                     permissions=list(perms))


@pytest.fixture
def trt(as_user):  # noqa: F811
    as_user["user"] = _staff("TREATMENTS_MANAGE", "DENTAL_PLANS_MANAGE")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch.object(playbooks, "_now", lambda: NOW), patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="treatments"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": answers}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def test_treatment_price_single(db, trt):  # noqa: F811
    _reset([_tr(0, "PRP", 4000)])
    out = _ask("change PRP price to ₹5000")
    assert out["state"] == "NEEDS_CONFIRMATION" and "₹4,000" in str(out["cards"]) and "₹5,000" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("PUT", T[0], {"price_from_rupees": 5000})]


def test_treatment_price_above_one_crore_is_refused(db, trt):  # noqa: F811
    _reset([_tr(0, "PRP", 9_500_000)])
    assert _ask("increase PRP price by 10%")["error_code"] == "nothing_to_do" and S["writes"] == []


def test_treatment_bulk_by_category_typed_count(db, trt):  # noqa: F811
    _reset([_tr(0, "Laser Hair Removal", 2000, category="Laser"), _tr(1, "Laser Toning", 3000, category="Laser"),
            _tr(2, "PRP", 4000, category="Hair")])
    out = _ask("raise all laser treatments by 5%")
    assert out["cards"][0]["typed_count"] == 2, out
    assert _confirm(out, typed_count=2).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("PUT", T[0], {"price_from_rupees": 2100}), ("PUT", T[1], {"price_from_rupees": 3150})]


def test_cat04_treatment_bulk_cap_100(db, trt):  # noqa: F811
    _reset([_tr(i, f"T{i}", 1000) for i in range(101)])
    assert _ask("raise all treatments by 5%")["error_code"] == "too_many" and S["writes"] == []


def test_hide_and_publish_verify_by_read_back(db, trt):  # noqa: F811
    _reset([_tr(0, "LASIK"), _tr(1, "Chemical Peel", active=False)])
    out = _ask("hide LASIK")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    out = _ask("publish chemical peel")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("STATUS", {"treatment_ids": [T[0]], "is_active": False}),
                           ("STATUS", {"treatment_ids": [T[1]], "is_active": True})]


def test_hiding_a_hidden_treatment_is_nothing_to_do(db, trt):  # noqa: F811
    _reset([_tr(0, "LASIK", active=False)])
    assert _ask("hide LASIK")["error_code"] == "nothing_to_do"


def test_doctor_mapping_previews_current_vs_new(db, trt):  # noqa: F811
    _reset([_tr(0, "PRP", doctors=[D_RAO])])
    out = _ask("only Dr Meera does PRP")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    text = str(out["cards"])
    assert "Srinivas Rao" in text and "Meera Iyer" in text
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("DOCTORS", T[0], {"doctor_ids": [D_MEERA]})]


def test_same_doctor_mapping_is_nothing_to_do(db, trt):  # noqa: F811
    _reset([_tr(0, "PRP", doctors=[D_MEERA])])
    assert _ask("only Dr Meera does PRP")["error_code"] == "nothing_to_do"


def test_treatments_need_the_permission(db, trt):  # noqa: F811
    trt["user"] = _staff("DENTAL_PLANS_MANAGE")
    assert _ask("hide LASIK")["state"] == "REJECTED"


# dental

_PLAN_TEXT = "Root canal plan for Suresh Kumar 9876543210, 3 sittings, ₹9000, first sitting Monday at 10:30 with Dr Rao"


def test_dental_plan_asks_consent_then_books_the_first_sitting(db, trt):  # noqa: F811
    _reset([_tr(0, "Root Canal", 3000, category="Endodontics")])
    out = _ask(_PLAN_TEXT, page="dentalplans")
    assert out["state"] == "NEEDS_CLARIFICATION" and "whatsapp_consent" in str(out["cards"]), out
    out = _answer(out, whatsapp_consent="yes")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    text = str(out["cards"])
    assert "Suresh Kumar" in text and "10:30" in text and "3 sittings" in text and "3210" in text
    assert "9876543210" not in text  # last 4 digits only
    assert S["writes"] == [] and S["slot_reads"] == [(D_RAO, MON)]
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body["patient_phone"] == "9876543210" and body["patient_name"] == "Suresh Kumar"
    assert body["treatment_id"] == T[0] and body["planned_sittings"] == 3 and body["quoted_amount_rupees"] == 9000
    assert body["whatsapp_consent"] is True
    assert body["first_sitting"] == {"doctor_id": D_RAO, "date": MON, "time": "10:30", "send_patient": True,
                                     "notify_doctor": True}


def test_consent_no_is_sent_as_false_and_never_inferred(db, trt):  # noqa: F811
    _reset([_tr(0, "Root Canal")])
    out = _answer(_ask(_PLAN_TEXT + " and send whatsapp", page="dentalplans"), whatsapp_consent="no")
    _confirm(out)
    assert S["writes"][0][1]["whatsapp_consent"] is False


def test_cat10_a_leave_day_is_refused_before_any_write(db, trt):  # noqa: F811
    _reset([_tr(0, "Root Canal")], slots=(), reason="Dr. Srinivas Rao is on leave that day.")
    out = _answer(_ask(_PLAN_TEXT, page="dentalplans"), whatsapp_consent="yes")
    assert out["error_code"] == "nothing_to_do" and "on leave" in out["message"] and S["writes"] == []


def test_a_taken_time_lists_the_free_times(db, trt):  # noqa: F811
    _reset([_tr(0, "Root Canal")], slots=("09:00", "11:30"))
    out = _answer(_ask(_PLAN_TEXT, page="dentalplans"), whatsapp_consent="yes")
    assert out["error_code"] == "nothing_to_do" and "11:30" in out["message"] and S["writes"] == []


def test_plan_without_a_first_sitting(db, trt):  # noqa: F811
    _reset([_tr(0, "Braces", category="Orthodontics")])
    out = _answer(_ask("braces plan for Anitha 9876543210 12 sittings", page="dentalplans"), whatsapp_consent="no")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert "first_sitting" not in S["writes"][0][1]


def test_unknown_treatment_is_refused(db, trt):  # noqa: F811
    _reset([_tr(0, "Braces")])
    out = _answer(_ask(_PLAN_TEXT, page="dentalplans"), whatsapp_consent="yes")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_a_sitting_that_was_not_booked_is_not_verified(db, trt):  # noqa: F811
    _reset([_tr(0, "Root Canal")], sitting_ok=False)
    out = _answer(_ask(_PLAN_TEXT, page="dentalplans"), whatsapp_consent="yes")
    assert _confirm(out).json()["state"] != "COMPLETED_VERIFIED"


def test_next_sitting_for_an_existing_plan(db, trt):  # noqa: F811
    _reset(plans=[{"id": "p1", "patient_name": "Suresh Kumar", "patient_phone": "9876543210",
                   "treatment_name": "Root Canal", "status": "active", "sittings": []}])
    out = _ask("book Suresh's next sitting on Monday at 11 am with Dr Rao", page="dentalplans")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("SITTING", "p1", {"doctor_id": D_RAO, "date": MON, "time": "11:00",
                                              "send_patient": True, "notify_doctor": True})]


def test_dental_needs_the_permission(db, trt):  # noqa: F811
    trt["user"] = _staff("TREATMENTS_MANAGE")
    assert _ask(_PLAN_TEXT, page="dentalplans")["state"] == "REJECTED"


def test_registered():
    for aid, risk, perm in (("treatment.price", "R2", "TREATMENTS_MANAGE"),
                            ("treatment.price_bulk", "R3", "TREATMENTS_MANAGE"),
                            ("treatments.status", "R2", "TREATMENTS_MANAGE"),
                            ("treatment.doctors", "R2", "TREATMENTS_MANAGE"),
                            ("dental.plan_create", "R2", "DENTAL_PLANS_MANAGE"),
                            ("dental.sitting_book", "R2", "DENTAL_PLANS_MANAGE")):
        spec = REGISTRY[aid]
        assert (spec.risk, spec.permission, spec.perm_source) == (risk, perm, "dependency"), aid
    assert REGISTRY["treatment.price_bulk"].max_items == 100


# ── a lab-or-treatment price tie, outside both pages ─────────────────────────

def test_a_lab_only_clinic_resolves_the_tie_to_the_lab_test(db, trt):  # noqa: F811
    trt["user"] = _staff("LAB_TESTS_MANAGE", "TREATMENTS_MANAGE")
    lab_only = {"id": CID, "plan": "diagbooking", "features": {"kriya_ketu": True}}
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=lab_only)):
        out = _ask("change PRP price to ₹5000", page="dashboard")
    assert out["action_id"] == "lab_test.price", out  # treatments are off here, so no question is asked


def test_a_clinic_with_both_is_asked_which(db, trt):  # noqa: F811
    trt["user"] = _staff("LAB_TESTS_MANAGE", "TREATMENTS_MANAGE")
    both = {"id": CID, "plan": "diagbooking", "features": {"kriya_ketu": True, "specialty_treatments": True}}
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=both)):
        out = _ask("change PRP price to ₹5000", page="dashboard")
    assert out["state"] == "NEEDS_CLARIFICATION" and "action" in str(out["cards"]), out
