"""Voice-first entry (owner, 2026-10-30): what staff typed, said once and confirmed.

- `opd.vitals` (R2): "token 5, BP 120 over 80, pulse 88". The encounter endpoint blanks every vital it is not
  sent, so the body is the chart's current vitals with only the ones said replaced.
- `opd.record_payment` (R3, typed rupees): "Ravi paid 500 cash". One receipt per payment: the idempotency key
  comes from the invoice, what it had been paid and this payment. UPI / card need their reference.
- `doctor.active` (R2): "remove Dr Rao" switches the doctor off (is_active); the hard delete stays on the page.
- `ipd.vitals` (R2): "bed 12, BP 110/70, sugar 140" -> one observation, timed when it was said.
- `ipd.transfer` (R2): "shift bed 12 to bed 15": only to a free bed, with the bed the preview saw (expected_bed_id).
"""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser
from app.voice.dates import IST
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=IST)
CLINIC = {"id": CID, "plan": "polyclinic", "account_type": "tenant", "opd_state": "READY",
          "features": {"kriya_ketu": True, "opd_enabled": True, "ipd_enabled": True}}


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, vitals", [
    ("token 5 BP 120 over 80, pulse 88, temp 99.5, SpO2 97, weight 62 kg",
     {"bp_systolic": 120, "bp_diastolic": 80, "temperature_c": "37.5", "pulse_bpm": 88, "spo2_pct": 97,
      "weight_kg": 62}),
    ("BP 130/85 temperature 37.2 celsius", {"bp_systolic": 130, "bp_diastolic": 85, "temperature_c": "37.2"}),
    ("temp 101 F weight 54.5", {"temperature_c": "38.3", "weight_kg": "54.5"}),
    ("bed 12 bp 110 by 70, sugar 140, pain 3, rr 18",
     {"bp_systolic": 110, "bp_diastolic": 70, "blood_glucose_mg_dl": 140, "pain_score": 3, "resp_rate": 18}),
    ("bp 80/120", None),                 # systolic must be above diastolic: asked, never sent
    ("pulse 400", None),                 # outside the endpoint's bounds
    ("token 5", None),
])
def test_resolve_vitals(text, vitals):
    assert resolve.resolve_vitals(text) == vitals


@pytest.mark.parametrize("text, target", [
    ("token 5 BP 120/80", "#5"), ("Ravi Kumar vitals BP 130/85 pulse 72", "ravi kumar"),
    ("record BP 120/80 for Lakshmi", "lakshmi"), ("bed 12 bp 110/70", "bed:12"),
    ("shift bed 12 patient to bed 15", "bed:12"), ("move Ravi to bed ICU 3", "ravi"),
    ("Sita paid 1200 by UPI UTR 412345678901", "sita"), ("token 7 paid ₹300 card", "#7"),
])
def test_resolve_patient_target(text, target):
    assert resolve.resolve_patient_target(text) == target


@pytest.mark.parametrize("text, amount, mode, ref", [
    ("Ravi paid 500 cash", "500", "cash", None), ("token 7 paid ₹300 card", "300", "card", None),
    ("Sita paid 1200 by UPI UTR 412345678901", "1200", "upi", "412345678901"),
    ("record 450 rupees from Lakshmi", "450", None, None), ("token 4 cash", None, "cash", None),
    ("paid 500 cash and upi", "500", None, None),        # two modes: asked
])
def test_resolve_payment(text, amount, mode, ref):
    assert (resolve.resolve_paid_amount(text), resolve.resolve_pay_mode(text), resolve.resolve_pay_ref(text)) == \
        (amount, mode, ref)


@pytest.mark.parametrize("text, action", [
    ("token 5 BP 120 over 80, pulse 88", "opd.vitals"), ("bed 12 BP 110/70 sugar 140", "ipd.vitals"),
    ("shift bed 12 patient to bed 15", "ipd.transfer"), ("Ravi paid 500 cash", "opd.record_payment"),
    ("remove doctor Anitha", "doctor.active"), ("reactivate Dr Anitha", "doctor.active"),
    ("move token 5 to vitals", "opd.stage"), ("remove leave for Dr Anitha tomorrow", "doctor_leave.delete"),
    ("refund 500 to Ravi", "!opd.record_payment"),
])
def test_routes(text, action):
    r = rules.route(text, doctors=[{"id": "d1", "name": "Dr. Anitha"}], now=NOW)
    if action.startswith("!"):
        assert r.action_id != action[1:], r
    else:
        assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


def test_the_time_said_is_fixed_in_the_params():
    r = rules.route("bed 12 BP 110/70", now=NOW)
    assert r.params["observed_at"] == "2026-10-30T10:00:00+05:30"


def test_registered():
    assert {REGISTRY[a].risk for a in ("opd.vitals", "ipd.vitals", "ipd.transfer", "doctor.active")} == {"R2"}
    assert REGISTRY["opd.record_payment"].risk == "R3"
    assert "doctor.delete" not in REGISTRY and REGISTRY["ipd.nursing"].risk == "H"


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}
D1, A1, A2 = str(uuid.UUID(int=901)), str(uuid.UUID(int=911)), str(uuid.UUID(int=912))
BED12, BED15, BED16 = "b12", "b15", "b16"


def _reset():
    S.clear()
    S.update(
        writes=[],
        doctors=[{"id": D1, "name": "Dr. Anitha", "is_active": True}],
        queue=[{"appointment_id": A1, "token_number": 5, "patient_name": "Ravi Kumar", "stage": "vitals_pending",
                "doctor_name": "Dr. Anitha"}],
        encounter={"status": "draft", "bp_systolic": 118, "bp_diastolic": 76, "weight_kg": 60.0},
        invoices=[{"id": "inv1", "appointment_id": A1, "status": "issued", "invoice_number": "OPD-1",
                   "total_paise": 80000, "paid_paise": 0, "credited_paise": 0, "patient_snapshot": {"name": "Ravi Kumar"}}],
        receipts={},
        beds=[{"id": BED12, "code": "12", "status": "occupied", "is_active": True,
               "occupant": {"admission_id": A2, "patient_name": "Sita Devi"}},
              {"id": BED15, "code": "15", "status": "available", "is_active": True},
              {"id": BED16, "code": "16", "status": "cleaning", "is_active": True}],
        observations=[],
        patients=[{"patient_id": "p1", "family_member_id": None, "name": "Ravi Kumar", "mrn": "MRN-1",
                   "phone": "9876543210"},
                  {"patient_id": "p2", "family_member_id": None, "name": "Ravi Teja", "mrn": "MRN-2", "phone": "9000000002"}],
        admissions={A2: {"id": A2, "status": "admitted", "patient_id": "pat9"}},
        services=[{"id": "s1", "code": "XR1", "name": "X Ray Chest", "price_paise": 45000, "active": True,
                   "source": "master", "updated_at": "2026-10-01T00:00:00+00:00"},
                  {"code": "OLD", "name": "ECG", "price_paise": 30000, "active": True, "source": "legacy"}],
    )


@fake.get("/admin/me")
async def _me():
    return {"branch_id": None}


@fake.get("/admin/doctors")
async def _doctors():
    return S["doctors"]


@fake.put("/admin/doctors/{did}")
async def _put_doctor(did: str, request: Request):
    body = await request.json()
    S["writes"].append(("doctor", did, body))
    row = next(d for d in S["doctors"] if d["id"] == did)
    row.update(body)
    return row


@fake.get("/admin/appointments")
async def _appts():
    return {"appointments": [], "total": 0}


@fake.get("/admin/opd/queue")
async def _queue():
    return {"doctors": [{"doctor_id": D1, "rows": S["queue"]}]}


@fake.get("/admin/opd/encounters/by-appointment/{aid}")
async def _encounter(aid: str):
    return S["encounter"]


@fake.put("/admin/opd/encounters/by-appointment/{aid}/vitals")
async def _vitals(aid: str, request: Request):
    body = await request.json()
    S["writes"].append(("vitals", aid, body))
    # the real endpoint blanks every vital it is not sent
    for k in playbooks._OPD_VITALS:
        S["encounter"][k] = body.get(k)
    return S["encounter"]


@fake.get("/admin/opd/invoices")
async def _invoices(request: Request):
    q = (request.query_params.get("q") or "").lower()
    return [i for i in S["invoices"] if q in i["patient_snapshot"]["name"].lower()]


@fake.get("/admin/opd/invoices/{iid}")
async def _invoice(iid: str):
    return next(i for i in S["invoices"] if i["id"] == iid)


@fake.post("/admin/opd/invoices/{iid}/receipts", status_code=201)
async def _receipt(iid: str, request: Request):
    body = await request.json()
    if body["idempotency_key"] in S["receipts"]:  # the real endpoint replays the first receipt
        return S["receipts"][body["idempotency_key"]]
    inv = next(i for i in S["invoices"] if i["id"] == iid)
    if body["amount_paise"] > inv["total_paise"] - inv["paid_paise"]:
        raise HTTPException(422, "exceeds balance")
    S["writes"].append(("receipt", iid, body))
    inv["paid_paise"] += body["amount_paise"]
    inv["status"] = "paid" if inv["paid_paise"] == inv["total_paise"] else "partially_paid"
    S["receipts"][body["idempotency_key"]] = {"id": f"r{len(S['receipts'])}", **body}
    return S["receipts"][body["idempotency_key"]]


@fake.get("/admin/ipd/board")
async def _board():
    return {"wards": [{"id": "w1", "name": "General", "code": "GEN", "beds": S["beds"]}, *S.get("more_wards", [])]}


@fake.get("/admin/ipd/admissions/{adm}")
async def _admission(adm: str):
    return {"admission": S["admissions"].get(adm, {"id": adm}), "observations": S["observations"]}


@fake.post("/admin/ipd/admissions/{adm}/observations", status_code=201)
async def _observe(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("observation", adm, body))
    S["observations"].append({"id": f"o{len(S['observations'])}", **body})
    return S["observations"][-1]


@fake.post("/admin/ipd/admissions/{adm}/transfer")
async def _transfer(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("transfer", adm, body))
    every = S["beds"] + [b for w in S.get("more_wards", []) for b in w["beds"]]
    old = next(b for b in every if b["id"] == body["expected_bed_id"])
    new = next(b for b in every if b["id"] == body["to_bed_id"])
    if old.get("occupant", {}).get("admission_id") != adm or new["status"] != "available":
        raise HTTPException(409, "stale")
    new.update(status="occupied", occupant=old.pop("occupant"))
    old["status"] = "cleaning"
    return {"id": adm}


client = TestClient(app)


@pytest.fixture
def ve(as_user):  # noqa: F811
    as_user["user"] = AdminUser(username="admin1", role="clinic_admin", clinic_id=CID, user_id=str(uuid.uuid4()))
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch.object(playbooks, "_now", lambda: NOW), patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="dashboard"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "voice",
        "ui_context": {"page": page}}).json()


def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": answers}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def test_opd_vitals_keep_what_was_charted_before(db, ve):  # noqa: F811
    out = _ask("token 5 BP 120 over 80, pulse 88")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "118/76 → 120/80" in str(out["cards"]) and S["writes"] == []
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, aid, body), = S["writes"]
    assert aid == A1 and body["weight_kg"] == 60.0  # the weight taken earlier is sent back, not blanked
    assert (body["bp_systolic"], body["bp_diastolic"], body["pulse_bpm"]) == (120, 80, 88)


def test_opd_vitals_already_charted_is_nothing_to_do(db, ve):  # noqa: F811
    assert _ask("token 5 BP 118/76")["error_code"] == "nothing_to_do"


def test_opd_vitals_on_a_signed_visit_are_refused(db, ve):  # noqa: F811
    S["encounter"]["status"] = "signed"
    out = _ask("token 5 pulse 90")
    assert out["state"] != "NEEDS_CONFIRMATION" and S["writes"] == []


def test_opd_vitals_ask_for_the_numbers_when_none_were_heard(db, ve):  # noqa: F811
    out = _ask("record vitals for token 5", page="opdqueue")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["vitals"], out
    out = _answer(out, vitals="BP 124/82 pulse 76")
    assert out["state"] == "NEEDS_CONFIRMATION", out


def test_payment_is_typed_once_and_recorded_once(db, ve):  # noqa: F811
    out = _ask("Ravi paid 500 cash")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert out["cards"][0]["typed_count"] == 500 and "₹300" in str(out["cards"])  # still due after this
    assert _confirm(out, typed_count=50).status_code == 422 and S["writes"] == []
    assert _confirm(out, typed_count=500).json()["state"] == "COMPLETED_VERIFIED"
    (_, iid, body), = S["writes"]
    assert iid == "inv1" and body["mode"] == "cash" and body["amount_paise"] == 50000
    again = _ask("Ravi paid 300 cash")  # the next payment gets its own key
    assert again["state"] == "NEEDS_CONFIRMATION"
    assert _confirm(again, typed_count=300).json()["state"] == "COMPLETED_VERIFIED"
    assert len(S["writes"]) == 2 and S["writes"][0][2]["idempotency_key"] != S["writes"][1][2]["idempotency_key"]


def test_payment_by_token(db, ve):  # noqa: F811
    out = _ask("token 5 paid 800 cash")
    assert out["state"] == "NEEDS_CONFIRMATION", out


def test_payment_more_than_due_or_upi_without_reference_is_refused(db, ve):  # noqa: F811
    assert _ask("Ravi paid 900 cash")["state"] != "NEEDS_CONFIRMATION"
    assert _ask("Ravi paid 500 by UPI")["state"] != "NEEDS_CONFIRMATION"
    assert _ask("Ravi paid 500 by UPI UTR 412345678901")["state"] == "NEEDS_CONFIRMATION"
    assert S["writes"] == []


def test_payment_needs_the_billing_permission(db, ve):  # noqa: F811
    ve["user"] = AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="FRONT_DESK", permissions=["OPD_FRONT_DESK"])
    assert _ask("Ravi paid 500 cash")["state"] == "REJECTED"


def test_removing_a_doctor_switches_them_off(db, ve):  # noqa: F811
    out = _ask("remove doctor Anitha")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("doctor", D1, {"is_active": False})]
    assert _ask("remove doctor Anitha")["error_code"] == "nothing_to_do"
    out = _ask("reactivate Dr Anitha")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED" and S["doctors"][0]["is_active"] is True


def test_ipd_vitals_chart_one_observation(db, ve):  # noqa: F811
    out = _ask("bed 12 BP 110/70 sugar 140 weight 60")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "Weight" in str(out["cards"])  # said, but not on the inpatient chart: named, not sent
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, adm, body), = S["writes"]
    assert adm == A2 and body == {"kind": "vitals", "observed_at": "2026-10-30T10:00:00+05:30", "bp_systolic": 110,
                                  "bp_diastolic": 70, "blood_glucose_mg_dl": 140}


def test_ipd_vitals_for_an_empty_bed_are_refused(db, ve):  # noqa: F811
    assert _ask("bed 15 BP 110/70")["state"] != "NEEDS_CONFIRMATION" and S["writes"] == []


def test_bed_transfer_only_to_a_free_bed(db, ve):  # noqa: F811
    assert _ask("shift bed 12 patient to bed 16")["state"] != "NEEDS_CONFIRMATION"  # cleaning
    out = _ask("move Sita to bed 15")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, adm, body), = S["writes"]
    assert adm == A2 and (body["to_bed_id"], body["expected_bed_id"]) == (BED15, BED12)


def test_bed_transfer_preview_goes_stale_when_the_bed_is_taken(db, ve):  # noqa: F811
    out = _ask("shift bed 12 patient to bed 15")
    S["beds"][1]["status"] = "reserved"   # someone else took bed 15 after the preview
    assert _confirm(out).json()["state"] != "COMPLETED_VERIFIED" and S["writes"] == []


# ── batch 2: admit, discharge start, bed housekeeping, service prices ───────────

@fake.get("/admin/opd/patients/search")
async def _patients(q: str):
    digits = "".join(c for c in q if c.isdigit())
    return [p for p in S["patients"] if (digits and digits in p["phone"]) or
            (not digits and all(w in p["name"].lower() for w in q.lower().split()))]


@fake.post("/admin/ipd/admissions", status_code=201)
async def _admit(request: Request):
    body = await request.json()
    S["writes"].append(("admit", body))
    adm = f"adm{len(S['admissions'])}"
    S["admissions"][adm] = {"id": adm, "status": "admitted" if body.get("bed_id") else "pending",
                            "admission_number": f"IP-{len(S['admissions'])}"}
    return S["admissions"][adm]


@fake.post("/admin/ipd/admissions/{adm}/discharge/initiate")
async def _discharge(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("discharge", adm, body))
    S["admissions"][adm]["status"] = "discharge_pending"
    return S["admissions"][adm]


@fake.post("/admin/ipd/beds/{bed_id}/status")
async def _bed_status(bed_id: str, request: Request):
    body = await request.json()
    S["writes"].append(("bed", bed_id, body))
    bed = next(b for b in S["beds"] if b["id"] == bed_id)
    bed["status"] = body["status"]
    return bed


@fake.get("/admin/masters/services")
async def _services():
    return {"services": S["services"], "can_manage": True}


@fake.patch("/admin/masters/services/{sid}")
async def _service_patch(sid: str, request: Request):
    body = await request.json()
    S["writes"].append(("service", sid, body))
    row = next(x for x in S["services"] if x.get("id") == sid)
    row["price_paise"] = body["standard_price_paise"]
    return row


@pytest.mark.parametrize("text, who, bed, reason, priority", [
    ("admit Ravi Kumar under Dr Rao to bed 15 for fever and vomiting", "Ravi Kumar", "15", "fever and vomiting",
     "elective"),
    ("admit patient Lakshmi Devi for dengue, bed ICU 2, urgent", "Lakshmi Devi", "ICU-2", "dengue", "urgent"),
    ("admit 98765 43210 under Dr Anitha as an emergency", "9876543210", None, None, "emergency"),
])
def test_resolve_admission(text, who, bed, reason, priority):
    assert (resolve.resolve_admit_patient(text), resolve.resolve_admit_bed(text), resolve.resolve_reason(text),
            resolve.resolve_priority(text)) == (who, bed, reason, priority)


@pytest.mark.parametrize("text, kind", [
    ("discharge bed 12", "regular"), ("bed 12 patient left against medical advice", "lama"),
    ("bed 12 DAMA", "dama"), ("bed 4 patient absconded", "absconded"), ("refer bed 7 to another hospital", "transfer_out"),
    ("bed 4 patient expired", "death"),
])
def test_resolve_discharge_type(text, kind):
    assert resolve.resolve_discharge_type(text) == kind


@pytest.mark.parametrize("text, state", [
    ("bed 16 is clean", "available"), ("bed 16 cleaning done", "available"), ("bed 9 needs cleaning", "cleaning"),
    ("bed 7 under maintenance because the AC is broken", "maintenance"), ("block bed 3", "blocked"),
    ("bed 3", None),
])
def test_resolve_bed_state(text, state):
    assert resolve.resolve_bed_state(text) == state


@pytest.mark.parametrize("text, action", [
    ("admit Ravi Kumar under Dr Anitha to bed 15 for fever", "ipd.admit"), ("discharge bed 12", "ipd.discharge_start"),
    ("complete discharge for bed 12", "ipd.discharge_complete"), ("discharge list", "ipd.discharges"),
    ("bed 16 is clean", "ipd.bed_status"), ("how many free beds", "ipd.census"),
    ("increase room charges by 10%", "services.price"), ("add new service ECG", "services.create"),
    ("what is the price of x ray chest", "services.prices"),
])
def test_routes_batch2(text, action):
    r = rules.route(text, doctors=[{"id": "d1", "name": "Dr. Anitha"}], now=NOW)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


def test_a_death_is_never_recorded_by_voice(db, ve):  # noqa: F811
    assert _ask("discharge bed 12, patient expired")["state"] != "NEEDS_CONFIRMATION" and S["writes"] == []


def test_admit_to_a_free_bed_once(db, ve):  # noqa: F811
    out = _ask("admit Ravi Kumar under Dr Anitha to bed 15 for fever and vomiting")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "fever and vomiting" in str(out["cards"]) and S["writes"] == []
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert (body["patient_id"], body["admitting_doctor_id"], body["bed_id"], body["reason"]) == \
        ("p1", D1, BED15, "fever and vomiting")
    again = _ask("admit Ravi Kumar under Dr Anitha for fever")  # same patient, same day: same key (server replays)
    assert _confirm(again).json()["state"] in ("COMPLETED_VERIFIED", "COMPLETED_UNVERIFIED", "FAILED")
    assert S["writes"][1][1]["idempotency_key"] == body["idempotency_key"]


def test_admit_without_a_bed_goes_to_the_waiting_list(db, ve):  # noqa: F811
    out = _ask("admit Ravi Kumar under Dr Anitha for chest pain")
    assert out["state"] == "NEEDS_CONFIRMATION" and "waiting list" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert "bed_id" not in S["writes"][0][1]


def test_admit_refuses_a_busy_bed_an_unknown_or_ambiguous_patient(db, ve):  # noqa: F811
    assert _ask("admit Ravi Kumar under Dr Anitha to bed 16 for fever")["state"] != "NEEDS_CONFIRMATION"  # cleaning
    assert _ask("admit Mohan Das under Dr Anitha for fever")["state"] != "NEEDS_CONFIRMATION"           # not registered
    assert _ask("admit Ravi under Dr Anitha for fever")["state"] != "NEEDS_CONFIRMATION"                # two Ravis
    assert S["writes"] == []


def test_admit_asks_the_reason_it_was_not_given(db, ve):  # noqa: F811
    out = _ask("admit Ravi Kumar under Dr Anitha to bed 15")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["reason"], out
    assert _answer(out, reason="Dengue with low platelets")["state"] == "NEEDS_CONFIRMATION"


def test_discharge_start_and_lama(db, ve):  # noqa: F811
    out = _ask("bed 12 patient left against medical advice")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("discharge", A2, {"discharge_type": "lama"})]


def test_bed_housekeeping(db, ve):  # noqa: F811
    out = _ask("bed 16 is clean")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("bed", BED16, {"status": "available"})]
    assert _ask("bed 12 needs cleaning")["state"] != "NEEDS_CONFIRMATION"           # occupied: never touched
    assert _ask("bed 15 under maintenance")["state"] != "NEEDS_CONFIRMATION"        # out of service needs a reason
    out = _ask("bed 15 under maintenance because the oxygen point is broken")
    assert out["state"] == "NEEDS_CONFIRMATION", out


def test_service_price_change(db, ve):  # noqa: F811
    out = _ask("change X-ray chest price to 500", page="tariffs")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "₹450 → ₹500" in str(out["cards"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("service", "s1", {"standard_price_paise": 50000,
                                              "expected_updated_at": "2026-10-01T00:00:00+00:00"})]


def test_service_price_on_a_legacy_entry_is_refused(db, ve):  # noqa: F811
    assert _ask("change ECG service price to 400", page="tariffs")["state"] != "NEEDS_CONFIRMATION"
    assert S["writes"] == []


# ── batch 3: refunds, voids, deposits, casualty, theatre booking ────────────

def _paid_invoice(paid=50000, status="partially_paid"):
    S["invoices"][0].update(paid_paise=paid, status=status)


@fake.post("/admin/opd/invoices/{iid}/refunds")
async def _refund(iid: str, request: Request):
    body = await request.json()
    if body["idempotency_key"] in S["receipts"]:
        return S["receipts"][body["idempotency_key"]]
    inv = next(i for i in S["invoices"] if i["id"] == iid)
    S["writes"].append(("refund", iid, body))
    inv["paid_paise"] -= body["amount_paise"]
    S["receipts"][body["idempotency_key"]] = {"id": "rf", "kind": "refund", **body}
    return S["receipts"][body["idempotency_key"]]


@fake.post("/admin/opd/invoices/{iid}/void")
async def _void(iid: str, request: Request):
    body = await request.json()
    S["writes"].append(("void", iid, body))
    inv = next(i for i in S["invoices"] if i["id"] == iid)
    inv["status"] = "void"
    return inv


@fake.get("/admin/ipd/admissions/{adm}/deposits")
async def _deposits(adm: str):
    return {"deposits": S.setdefault("deposits", [])}


@fake.post("/admin/ipd/admissions/{adm}/deposits", status_code=201)
async def _deposit(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("deposit", adm, body))
    S.setdefault("deposits", []).append({"id": f"d{len(S['deposits'])}", "kind": "deposit", **body})
    return S["deposits"][-1]


@fake.post("/admin/emergency/register", status_code=201)
async def _er(request: Request):
    body = await request.json()
    S["writes"].append(("er", body))
    S.setdefault("er", []).append({"id": f"er{len(S['er'])}", "visit_number": "ER-1", **body})
    return S["er"][-1]


@fake.get("/admin/emergency/visits")
async def _er_board():
    return {"visits": S.get("er", [])}


@fake.get("/admin/ot/options")
async def _ot_options():
    return {"admissions": [{"id": A2, "patient_name": "Sita Devi", "bed": "12"}],
            "theatres": [{"id": "t1", "name": "OT 1"}, {"id": "t2", "name": "OT 2"}],
            "doctors": [{"id": D1, "name": "Dr. Anitha"}], "services": []}


@fake.post("/admin/ot/surgeries", status_code=201)
async def _ot_book(request: Request):
    body = await request.json()
    S["writes"].append(("ot", body))
    S.setdefault("ot", []).append({"id": "s1", "surgery_number": "OT-1", **body})
    return S["ot"][-1]


@fake.get("/admin/ot/schedule")
async def _ot_schedule():
    return {"surgeries": S.get("ot", [])}


def test_refund_typed_and_never_more_than_paid(db, ve):  # noqa: F811
    _paid_invoice()
    assert _ask("refund 600 to Ravi in cash because the test was not done")["state"] != "NEEDS_CONFIRMATION"
    out = _ask("refund 200 to Ravi in cash because the test was not done")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 200, out
    assert _confirm(out, typed_count=200).json()["state"] == "COMPLETED_VERIFIED"
    (_, iid, body), = S["writes"]
    assert (iid, body["mode"], body["amount_paise"], body["reason"]) == ("inv1", "cash", 20000, "the test was not done")


def test_refund_asks_the_reason(db, ve):  # noqa: F811
    _paid_invoice()
    out = _ask("refund 200 to Ravi in cash")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["reason"], out


def test_refund_and_void_are_for_opd_admins(db, ve):  # noqa: F811
    _paid_invoice()
    ve["user"] = AdminUser(username="cash1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="CASHIER", permissions=["OPD_BILLING"])
    assert _ask("refund 200 to Ravi in cash because the test was not done")["state"] == "REJECTED"
    assert _ask("void Ravi's bill because it was made twice")["state"] == "REJECTED"


def test_void_only_an_unpaid_bill(db, ve):  # noqa: F811
    _paid_invoice()
    assert _ask("void Ravi's bill because it was made twice")["state"] != "NEEDS_CONFIRMATION"
    _paid_invoice(0, "issued")
    out = _ask("void Ravi's bill because it was made twice")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 800, out
    assert _confirm(out, typed_count=800).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("void", "inv1", {"reason": "it was made twice"})]


def test_deposit_for_an_inpatient(db, ve):  # noqa: F811
    assert _ask("bed 12 advance 5,000 by UPI")["state"] != "NEEDS_CONFIRMATION"  # UPI needs its reference
    out = _ask("deposit 10000 cash for bed 12")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 10000, out
    assert _confirm(out, typed_count=10000).json()["state"] == "COMPLETED_VERIFIED"
    (_, adm, body), = S["writes"]
    assert (adm, body["mode"], body["amount_paise"]) == (A2, "cash", 1000000)


def test_emergency_unknown_patient_with_vitals(db, ve):  # noqa: F811
    out = _ask("emergency: road accident, unknown male about 30, triage red, by ambulance, BP 90/60 pulse 120")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body["is_unknown"] is True and body["triage_category"] == "red" and body["arrival_mode"] == "ambulance"
    assert (body["estimated_gender"], body["estimated_age"]) == ("male", 30)
    assert body["initial_vitals"] == {"bp_sys": 90, "bp_dia": 60, "pulse": 120}


def test_emergency_named_patient_needs_a_phone_and_never_black(db, ve):  # noqa: F811
    assert _ask("new casualty patient Ravi Kumar with chest pain, triage yellow")["state"] != "NEEDS_CONFIRMATION"
    assert _ask("emergency: unknown male brought dead, triage black")["state"] != "NEEDS_CONFIRMATION"
    out = _ask("new casualty patient Ravi Kumar 98765 43210 with chest pain, triage yellow")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert S["writes"] == []


def test_emergency_asks_triage_when_not_said(db, ve):  # noqa: F811
    out = _ask("new casualty patient Ravi Kumar 98765 43210 with chest pain")
    assert out["state"] == "NEEDS_CLARIFICATION" and "triage" in out["cards"][0]["missing"], out


def test_book_a_surgery(db, ve):  # noqa: F811
    out = _ask("book knee replacement for bed 12 tomorrow 10 to 12 in OT 1 with Dr Anitha for osteoarthritis knee")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert (body["admission_id"], body["theatre_id"], body["primary_surgeon_id"]) == (A2, "t1", D1)
    assert (body["procedure_name"], body["pre_op_diagnosis"]) == ("knee replacement", "osteoarthritis knee")
    assert body["scheduled_start"].startswith("2026-10-31T10:00") and body["scheduled_end"].startswith("2026-10-31T12:00")


def test_book_a_surgery_names_the_theatre_and_the_reason(db, ve):  # noqa: F811
    assert _ask("book knee replacement for bed 12 tomorrow 10 to 12 with Dr Anitha for osteoarthritis knee")["state"] \
        != "NEEDS_CONFIRMATION"  # two theatres: which one
    out = _ask("book knee replacement for bed 12 tomorrow 10 to 12 in OT 2 with Dr Anitha")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["indication"], out


# ── batch 4: stay details, discharge completion, OT status, staff logins, branches ──

@fake.patch("/admin/ipd/admissions/{adm}")
async def _patch_admission(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("patch", adm, body))
    S["admissions"][adm].update(body)
    return S["admissions"][adm]


@fake.get("/admin/ipd/admissions/{adm}/discharge/checklist")
async def _checklist(adm: str):
    items = S.get("checklist", [{"key": "discharge_initiated", "done": True}, {"key": "bill_settled", "done": True}])
    return {"items": items, "ready": all(i["done"] for i in items)}


@fake.post("/admin/ipd/admissions/{adm}/discharge/complete")
async def _complete(adm: str, request: Request):
    S["writes"].append(("complete", adm, await request.json()))
    S["admissions"][adm]["status"] = "discharged"
    return S["admissions"][adm]


@fake.post("/admin/ot/surgeries/{sid}/status")
async def _ot_status(sid: str, request: Request):
    body = await request.json()
    S["writes"].append(("ot_status", sid, body))
    row = next(s for s in S["ot"] if s["id"] == sid)
    row["status"] = {"start": "in_theatre", "recovery": "recovery", "cancel": "cancelled", "postpone": "postponed"}[
        body["action"]]
    return row


@fake.get("/admin/staff")
async def _staff_list():
    return {"staff": S.setdefault("staff", [
        {"id": "st1", "username": "ramesh", "full_name": "Ramesh Babu", "staff_role": "RECEPTIONIST", "is_active": True},
        {"id": "st2", "username": "priya", "full_name": "Priya Sharma", "staff_role": "FRONT_DESK", "is_active": True}])}


@fake.put("/admin/staff/{sid}/toggle")
async def _toggle(sid: str):
    S["writes"].append(("toggle", sid))
    row = next(r for r in S["staff"] if r["id"] == sid)
    row["is_active"] = not row["is_active"]
    return {"success": True, "is_active": row["is_active"]}


@fake.put("/admin/staff/{sid}")
async def _staff_put(sid: str, request: Request):
    body = await request.json()
    S["writes"].append(("staff", sid, body))
    next(r for r in S["staff"] if r["id"] == sid).update(body)
    return {"success": True}


@fake.get("/admin/branches")
async def _branches():
    return {"branches": S.setdefault("branches", [{"id": "br1", "name": "Aura - Gajuwaka", "short_name": "Gajuwaka",
                                                   "is_active": True}])}


@fake.post("/admin/branches")
async def _branch_add(request: Request):
    body = await request.json()
    S["writes"].append(("branch", body))
    S["branches"].append({"id": "br2", "name": f"Aura - {body['short_name']}", "is_active": True, **body})
    return S["branches"][-1]


@fake.put("/admin/branches/{bid}")
async def _branch_put(bid: str, request: Request):
    body = await request.json()
    S["writes"].append(("branch_put", bid, body))
    next(b for b in S["branches"] if b["id"] == bid).update(body)
    return {"success": True}


@pytest.mark.parametrize("text, patch", [
    ("bed 12 insurance Star Health policy SH12345",
     {"payer_type": "insurance", "payer_name": "Star Health", "policy_number": "SH12345"}),
    ("bed 12 attendant Ramu, son, 98765 43210",
     {"attendant_name": "Ramu", "attendant_relation": "son", "attendant_phone": "9876543210"}),
    ("bed 12 expected discharge Monday", {"expected_discharge_on": "2026-11-02"}),
    ("bed 12 is self pay", {"payer_type": "self"}),
])
def test_resolve_admission_patch(text, patch):
    assert resolve.resolve_admission_patch(text, now=NOW) == patch


@pytest.mark.parametrize("text, reason", [
    ("void the invoice for Ravi Kumar", None),          # "for" names the patient, never the reason
    ("void Ravi's bill because it was made twice", "it was made twice"),
    ("cancel surgery for Sita, reason: patient has fever", "patient has fever"),
])
def test_a_reason_is_only_what_is_said_as_one(text, reason):
    assert resolve.resolve_why(text) == reason


@pytest.mark.parametrize("text, action", [
    ("bed 12 insurance Star Health policy SH12345", "ipd.admission_update"),
    ("complete discharge for bed 12", "ipd.discharge_complete"), ("discharge summary for bed 12", "ipd.discharge"),
    ("start surgery for Sita", "ot.status"), ("postpone bed 12 surgery because blood is not arranged", "ot.status"),
    ("deactivate Ramesh's login", "staff.active"), ("make Priya Sharma a cashier", "staff.role"),
    ("reset Ramesh's password", "staff.manage"), ("add a new branch at Madhurawada", "branch.create"),
    ("rename Gajuwaka branch to Gajuwaka Main", "branch.rename"), ("delete Gajuwaka branch", "branch.manage"),
])
def test_routes_batch4(text, action):
    r = rules.route(text, now=NOW)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


def test_admission_details_only_what_changed(db, ve):  # noqa: F811
    S["admissions"][A2]["payer_type"] = "self"
    out = _ask("bed 12 insurance Star Health policy SH12345")
    assert out["state"] == "NEEDS_CONFIRMATION" and "self → insurance" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("patch", A2, {"payer_type": "insurance", "payer_name": "Star Health",
                                          "policy_number": "SH12345"})]
    assert _ask("bed 12 insurance Star Health policy SH12345")["error_code"] == "nothing_to_do"


def test_discharge_completes_only_when_the_checklist_is_done(db, ve):  # noqa: F811
    S["checklist"] = [{"key": "summary_signed", "done": False}, {"key": "bill_settled", "done": False}]
    out = _ask("complete discharge for bed 12")
    assert out["state"] != "NEEDS_CONFIRMATION" and "discharge summary signed" in str(out), out
    S["checklist"] = [{"key": "summary_signed", "done": True}, {"key": "bill_settled", "done": True}]
    out = _ask("complete discharge for bed 12")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("complete", A2, {})]


def _surgery(status="scheduled"):
    S["ot"] = [{"id": "s9", "admission_id": A2, "patient_name": "Sita Devi", "procedure_name": "Appendectomy",
                "status": status, "scheduled_start": "2026-10-30T14:00:00+05:30", "theatre_name": "OT 1"}]


def test_ot_start_and_cancel_needs_a_reason(db, ve):  # noqa: F811
    _surgery()
    assert _ask("cancel surgery for Sita")["state"] != "NEEDS_CONFIRMATION"   # no reason said: asked, not guessed
    out = _ask("start surgery for Sita")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("ot_status", "s9", {"action": "start"})]
    assert _ask("postpone Sita's surgery because blood is not arranged")["state"] != "NEEDS_CONFIRMATION"  # in theatre


def test_ot_postpone_by_bed_with_reason(db, ve):  # noqa: F811
    _surgery()
    out = _ask("postpone bed 12 surgery because blood is not arranged")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("ot_status", "s9", {"action": "postpone", "reason": "blood is not arranged"})]


def test_staff_login_off_and_role(db, ve):  # noqa: F811
    out = _ask("deactivate Ramesh's login")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("toggle", "st1")]
    assert _ask("deactivate Ramesh's login")["error_code"] == "nothing_to_do"   # never toggled back by a repeat
    out = _ask("make Priya Sharma a cashier")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1] == ("staff", "st2", {"staff_role": "CASHIER"})


def test_staff_needs_the_permission(db, ve):  # noqa: F811
    ve["user"] = AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="FRONT_DESK", permissions=["OPD_FRONT_DESK"])
    assert _ask("deactivate Ramesh's login")["state"] == "REJECTED"


def test_branches(db, ve):  # noqa: F811
    assert _ask("add a new branch at Gajuwaka")["state"] != "NEEDS_CONFIRMATION"   # already a branch
    out = _ask("add a new branch at Madhurawada")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("branch", {"short_name": "Madhurawada"})]
    out = _ask("rename Gajuwaka branch to Gajuwaka Main")
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1] == ("branch_put", "br1", {"short_name": "Gajuwaka Main"})


# ── batch 5: spoken numbers, doctor days / slot length, new services, credit notes, companies ──

@pytest.mark.parametrize("text, expected", [
    ("Change Doctor Meena's consultation fee to six hundred.", "Change Doctor Meena's consultation fee to 600."),
    ("token five BP one twenty over eighty pulse eighty eight temp ninety eight point six",
     "token 5 BP 120 over 80 pulse 88 temp 98.6"),
    ("Ravi paid fifteen hundred cash", "Ravi paid 1500 cash"),
    ("deposit two lakh cash for bed twelve", "deposit 200000 cash for bed 12"),
    ("refund two thousand five hundred to Ravi", "refund 2500 to Ravi"),
    ("one hundred and fifty rupees", "150 rupees"), ("a thousand rupees", "1000 rupees"),
    ("5 thousand cash", "5000 cash"), ("2.5 lakh deposit", "250000 deposit"),
    ("phone nine eight seven six five four three two one zero", "phone 9876543210"),
    ("mornings ten to one and evenings five to eight", "mornings 10 to 1 and evenings 5 to 8"),
    ("bp one ten over seventy", "bp 110 over 70"),
    ("add one doctor", "add one doctor"), ("someone called", "someone called"),     # words stay words
    ("no one should be booked", "no one should be booked"), ("token 5 bp 120/80", "token 5 bp 120/80"),
])
def test_spoken_numbers(text, expected):
    assert resolve.spoken_numbers(text) == expected
    assert resolve.spoken_numbers(expected) == expected  # idempotent


def test_a_spoken_fee_reaches_the_action():
    r = rules.route("Change Doctor Anitha's consultation fee to six hundred.", doctors=[{"id": "d1", "name": "Dr. Anitha"}])
    assert (r.kind, r.action_id, r.params["fee"]) == ("action", "doctor.fee", ["amount", "600"])


@fake.post("/admin/masters/services", status_code=201)
async def _service_add(request: Request):
    body = await request.json()
    S["writes"].append(("service_add", body))
    S["services"].append({"id": "s9", "code": body["service_code"], "name": body["name"],
                          "price_paise": body["standard_price_paise"], "source": "master", "active": True})
    return S["services"][-1]


@fake.get("/admin/opd/invoices/{iid}/credit-notes")
async def _credit_notes(iid: str):
    return {"items": S.setdefault("credit_notes", [])}


@fake.post("/admin/opd/invoices/{iid}/credit-notes", status_code=201)
async def _credit_note(iid: str, request: Request):
    body = await request.json()
    S["writes"].append(("credit_note", iid, body))
    S.setdefault("credit_notes", []).append({"id": "cn1", "credit_note_number": "CRN-1", **body})
    return S["credit_notes"][-1]


@fake.get("/admin/corporate-health/companies")
async def _companies():
    return {"companies": S.setdefault("companies", [{"id": "c1", "name": "Infosys"}]), "can_manage": True}


@fake.post("/admin/corporate-health/companies")
async def _company_add(request: Request):
    body = await request.json()
    S["writes"].append(("company", body))
    S["companies"].append({"id": "c2", **body})
    return {"success": True, "company": S["companies"][-1]}


def test_doctor_days_and_slot_length(db, ve):  # noqa: F811
    S["doctors"][0].update(available_days="Mon,Tue,Wed,Thu,Fri", morning_start="09:00:00", morning_end="13:00:00",
                           evening_start=None, evening_end=None, slot_duration_minutes=30)
    out = _ask("Dr Anitha works Monday to Saturday")
    assert out["state"] == "NEEDS_CONFIRMATION" and "Mon,Tue,Wed,Thu,Fri → Mon,Tue,Wed,Thu,Fri,Sat" in str(out), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    out = _ask("Dr Anitha slots fifteen minutes each")
    assert out["state"] == "NEEDS_CONFIRMATION" and "16 slots" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1] == ("doctor", D1, {"morning_start": "09:00", "morning_end": "13:00",
                                              "slot_duration_minutes": 15})  # the shifts go with it (D-34)
    assert _ask("Dr Anitha slots 7 minutes")["state"] != "NEEDS_CONFIRMATION"  # not a supported length


def test_add_a_service_by_voice(db, ve):  # noqa: F811
    out = _ask("add a new service Treadmill Test at three hundred rupees", page="tariffs")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (_, body), = S["writes"]
    assert body == {"service_code": "TREADMILL-TEST", "name": "Treadmill Test", "category": "diagnostic",
                    "standard_price_paise": 30000}
    assert _ask("add a new service ECG at 400 rupees", page="tariffs")["state"] != "NEEDS_CONFIRMATION"  # exists


def test_credit_note_typed_with_a_gst_reason(db, ve):  # noqa: F811
    S["invoices"][0].update(subtotal_paise=80000, discount_paise=0, total_paise=80000, paid_paise=0)
    out = _ask("credit note of two hundred on Ravi's bill because he was charged twice")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 200, out
    assert _confirm(out, typed_count=200).json()["state"] == "COMPLETED_VERIFIED"
    (_, iid, body), = S["writes"]
    assert (iid, body["reason"], body["amount_paise"], body["reason_notes"]) == \
        ("inv1", "billing_error", 20000, "he was charged twice")
    assert "refund_mode" not in body


def test_credit_note_on_a_paid_bill_needs_the_refund_mode(db, ve):  # noqa: F811
    S["invoices"][0].update(subtotal_paise=80000, discount_paise=0, total_paise=80000, paid_paise=80000, status="paid")
    assert _ask("credit note of 200 on Ravi's bill because he was charged twice")["state"] != "NEEDS_CONFIRMATION"
    out = _ask("credit note of 200 on Ravi's bill because he was charged twice, refund in cash")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out, typed_count=200).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1][2]["refund_mode"] == "cash"


def test_add_a_corporate_client(db, ve):  # noqa: F811
    corp = {**CLINIC, "plan": "diagbooking", "features": {**CLINIC["features"], "corporate_health": True}}
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=corp)):
        assert _ask("add corporate company Infosys")["state"] != "NEEDS_CONFIRMATION"  # already a client
        out = _ask("add Tata Steel as a corporate client")
        assert out["state"] == "NEEDS_CONFIRMATION", out
        assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("company", {"name": "Tata Steel"})]


# ── batch 6: Hindi / Telugu numbers, clock words, beds in two wards, IPD setup ──

@pytest.mark.parametrize("text, expected", [
    ("पाँच सौ रुपये", "500 रुपये"), ("डेढ़ हज़ार cash", "1500 cash"), ("ढाई सौ", "250"), ("साढ़े तीन सौ", "350"),
    ("दो हज़ार पाँच सौ", "2500"), ("ఐదు వందలు", "500"), ("రెండు వేలు", "2000"), ("వెయ్యి రూపాయలు", "1000 రూపాయలు"),
    ("పదిహేను వందలు", "1500"), ("ఒక లక్ష", "100000"), ("do hazaar cash", "2000 cash"), ("rendu velu", "2000"),
    ("do it now", "do it now"), ("teen days", "teen days"), ("पाँच दिन", "पाँच दिन"),       # words stay words
    ("token पाँच", "token 5"),
    ("mornings from ten thirty to one", "mornings from 10:30 to 1"), ("at ten thirty", "at 10:30"),
    ("nine forty five am", "9:45 am"), ("from nine to twelve thirty", "from 9 to 12:30"),
    ("half past five", "5:30"), ("quarter past ten", "10:15"), ("quarter to six", "5:45"),
    ("BP one twenty over eighty", "BP 120 over 80"), ("fee to one twenty", "fee to 120"),  # numbers, not times
])
def test_spoken_numbers_indic_and_clock(text, expected):
    assert resolve.spoken_numbers(text) == expected
    assert resolve.spoken_numbers(expected) == expected


def test_hindi_payment_and_spoken_hours_route():
    r = rules.route("Ravi ne पाँच सौ रुपये cash diye")
    assert (r.action_id, r.params) == ("opd.record_payment", {"who": "ravi", "amount": "500", "mode": "cash"})
    r = rules.route("Dr Anitha morning from ten thirty to one", doctors=[{"id": "d1", "name": "Dr. Anitha"}])
    assert (r.action_id, r.params["window"]) == ("doctor.schedule", "10:30-13:00")


@pytest.mark.parametrize("text, bed, to_bed", [
    ("shift bed 5 in ICU to bed 3 in the private ward", "5@icu", "3@private"),
    ("shift bed 12 patient to bed 15", "12", "15"), ("bed 5 in ICU BP 110/70", "5@icu", None),
])
def test_a_bed_can_carry_its_ward(text, bed, to_bed):
    assert (resolve.resolve_bed(text), resolve.resolve_to_bed(text)) == (bed, to_bed)


def test_the_same_bed_code_in_two_wards_is_asked_never_guessed(db, ve):  # noqa: F811
    S["more_wards"] = [{"id": "w2", "name": "ICU", "code": "ICU", "beds": [
        {"id": "b15i", "code": "15", "status": "available", "is_active": True}]}]
    out = _ask("move Sita to bed 15")
    assert out["state"] != "NEEDS_CONFIRMATION" and "General" in str(out) and "ICU" in str(out), out
    out = _ask("move Sita to bed 15 in ICU")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1][2]["to_bed_id"] == "b15i"


@fake.get("/admin/ipd/setup")
async def _ipd_setup():
    return {"settings": {"bed_tariffs": S.setdefault("tariffs", {"general": 150000, "private": 300000}),
                         **S.setdefault("ipd_settings", {"discharge_cutoff_hour": 12, "reservation_minutes": 120})},
            "wards": S.setdefault("ward_rows", [{"id": "w1", "code": "GEN", "name": "General", "ward_type": "general",
                                                 "is_active": True}])}


@fake.post("/admin/ipd/wards", status_code=201)
async def _ward_add(request: Request):
    body = await request.json()
    S["writes"].append(("ward", body))
    S["ward_rows"].append({"id": "w9", "is_active": True, **body})
    return S["ward_rows"][-1]


@fake.post("/admin/ipd/wards/{wid}/beds", status_code=201)
async def _beds_add(wid: str, request: Request):
    body = await request.json()
    S["writes"].append(("beds", wid, body))
    S["beds"].extend({"id": f"n{b['code']}", "code": b["code"], "status": "available", "is_active": True}
                     for b in body["beds"])
    return {"beds": body["beds"]}


@fake.put("/admin/ipd/settings")
async def _ipd_settings(request: Request):
    body = await request.json()
    S["writes"].append(("settings", body))
    if "bed_tariffs" in body:
        S["tariffs"] = body["bed_tariffs"]
    S.setdefault("ipd_settings", {}).update({k: v for k, v in body.items() if k != "bed_tariffs"})
    return {"settings": {"bed_tariffs": S.get("tariffs"), **S["ipd_settings"]}}


def test_add_a_ward(db, ve):  # noqa: F811
    out = _ask("add a new ward Maternity B")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("ward", {"code": "MATERNITY-B", "name": "Maternity B", "ward_type": "maternity"})]
    assert _ask("add a new ward General")["state"] != "NEEDS_CONFIRMATION"   # already a ward


def test_add_beds_typed_count_and_next_numbers(db, ve):  # noqa: F811
    assert _ask("add beds 12 to 14 to the general ward")["state"] != "NEEDS_CONFIRMATION"   # bed 12 exists
    out = _ask("add 3 beds to the general ward")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 3, out
    assert "17 to 19" in str(out["cards"]) and "₹1,500" in str(out["cards"])  # after 12, 15, 16; the ward's tariff
    assert _confirm(out, typed_count=3).json()["state"] == "COMPLETED_VERIFIED"
    (_, wid, body), = S["writes"]
    assert wid == "w1" and [b["code"] for b in body["beds"]] == ["17", "18", "19"]
    assert {b["bed_category"] for b in body["beds"]} == {"general"}


def test_bed_tariff_keeps_the_others(db, ve):  # noqa: F811
    out = _ask("set ICU bed tariff to five thousand per day")
    assert out["state"] == "NEEDS_CONFIRMATION" and "not set → ₹5,000" in str(out["cards"]), out
    assert "already charged keep" in str(out["cards"])
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("settings", {"bed_tariffs": {"general": 150000, "private": 300000, "icu": 500000}})]
    assert _ask("general bed charges 1500 per day")["error_code"] == "nothing_to_do"


def test_setup_is_for_ipd_admins(db, ve):  # noqa: F811
    ve["user"] = AdminUser(username="nurse1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="NURSE", permissions=["IPD_NURSING"])
    assert _ask("add a new ward Maternity B")["state"] == "REJECTED"
    assert _ask("set ICU bed tariff to 5000 per day")["state"] == "REJECTED"


# ── batch 7: OPD / IPD settings, insurance payers and claims ──────────────────

@fake.get("/admin/opd/setup")
async def _opd_setup():
    return {"state": "READY", "checklist": [], "settings": S.setdefault("opd_settings", {
        "vitals_required": False, "billing_after_consult": True, "auto_open_shift": True,
        "after_checkin_stage": "vitals_pending", "payment_modes": ["cash", "upi", "card"], "rooms": {"other": "Room 1"}})}


@fake.put("/admin/opd/setup/settings")
async def _opd_settings(request: Request):
    body = await request.json()
    S["writes"].append(("opd_settings", body))
    S["opd_settings"].update(body)
    return {"state": "READY", "checklist": [], "settings": S["opd_settings"]}


def _claims():
    return S.setdefault("claims", [{
        "id": "c1", "claim_number": "CLM-0012", "admission_id": A2, "patient_name": "Sita Devi", "payer_name": "Star Health",
        "status": "filed", "settled_amount_paise": 0, "tds_deducted_paise": 0,
        "split": {"payer_payable_paise": 5_000_000, "payer_received_paise": 0, "tds_paise": 0, "disallowed_paise": 0}}])


@fake.get("/admin/insurance/payers")
async def _payers():
    return {"payers": S.setdefault("payers", [{"id": "p1", "name": "Star Health", "payer_type": "private_insurance"}])}


@fake.post("/admin/insurance/payers", status_code=201)
async def _payer_add(request: Request):
    body = await request.json()
    S["writes"].append(("payer", body))
    S["payers"].append({"id": "p2", **body})
    return S["payers"][-1]


@fake.get("/admin/insurance/claims")
async def _claims_list():
    return {"items": _claims(), "total": len(_claims())}


@fake.get("/admin/insurance/claims/{cid}")
async def _claim_get(cid: str):
    return next(c for c in _claims() if c["id"] == cid)


@fake.post("/admin/insurance/claims/{cid}/settlement")
async def _settle(cid: str, request: Request):
    body = await request.json()
    S["writes"].append(("settlement", cid, body))
    c = next(c for c in _claims() if c["id"] == cid)
    c["settled_amount_paise"] += body["amount_received_paise"]
    c["tds_deducted_paise"] += body["tds_paise"]
    c["split"]["disallowed_paise"] += sum(d["amount_paise"] for d in body.get("disallowances", []))
    c["status"] = "partially_settled"
    return c


@fake.post("/admin/insurance/claims/{cid}/action")
async def _claim_act(cid: str, request: Request):
    body = await request.json()
    S["writes"].append(("claim_action", cid, body))
    c = next(c for c in _claims() if c["id"] == cid)
    c["status"] = {"query": "query_raised", "in_process": "in_process", "withdraw": "withdrawn"}[body["action"]]
    return c


@pytest.mark.parametrize("text, change", [
    ("make vitals mandatory", {"vitals_required": True}), ("skip vitals in OPD", {"vitals_required": False}),
    ("patients pay before consultation", {"billing_after_consult": False}),
    ("turn off auto open shift", {"auto_open_shift": False}),
    ("after check-in send patients to waiting", {"after_checkin_stage": "waiting"}),
    ("accept cash and UPI only", {"payment_modes": ["cash", "upi"]}),
    ("set the UPI id to clinic@okaxis", None),       # the payee stays on the page
])
def test_resolve_opd_change(text, change):
    assert resolve.resolve_opd_change(text) == change


def test_opd_setting_sends_only_what_changed(db, ve):  # noqa: F811
    out = _ask("make vitals mandatory")
    assert out["state"] == "NEEDS_CONFIRMATION" and "optional → required" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("opd_settings", {"vitals_required": True})]
    assert _ask("make vitals mandatory")["error_code"] == "nothing_to_do"


def test_a_doctors_room_keeps_the_others(db, ve):  # noqa: F811
    out = _ask("Dr Anitha sits in room 3")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("opd_settings", {"rooms": {"other": "Room 1", D1: "Room 3"}})]


def test_opd_settings_are_for_opd_admins(db, ve):  # noqa: F811
    ve["user"] = AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="FRONT_DESK", permissions=["OPD_FRONT_DESK"])
    assert _ask("make vitals mandatory")["state"] == "REJECTED"


def test_ipd_settings(db, ve):  # noqa: F811
    out = _ask("discharge cut-off 11 am and hold beds for 2 hours")
    assert out["state"] == "NEEDS_CONFIRMATION" and "12:00 → 11:00" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("settings", {"discharge_cutoff_hour": 11})]   # 120 minutes was already set


def test_add_a_tpa(db, ve):  # noqa: F811
    assert _ask("add insurer Star Health")["state"] != "NEEDS_CONFIRMATION"   # already a payer
    out = _ask("add TPA Medi Assist")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("payer", {"name": "Medi Assist", "payer_type": "tpa"})]


def test_claim_settlement_typed_with_utr_and_tds(db, ve):  # noqa: F811
    assert _ask("Star Health paid 45,000 on claim CLM-0012")["state"] != "NEEDS_CONFIRMATION"   # no UTR
    out = _ask("Star Health paid 45,000 on claim CLM-0012, UTR AXIS123456, TDS 4,500")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 45000, out
    assert "₹500 still due" in str(out["cards"])
    assert _confirm(out, typed_count=45000).json()["state"] == "COMPLETED_VERIFIED"
    (_, cid, body), = S["writes"]
    assert (cid, body["amount_received_paise"], body["tds_paise"], body["utr_number"]) == \
        ("c1", 4_500_000, 450_000, "AXIS123456")


def test_claim_query_and_withdraw(db, ve):  # noqa: F811
    out = _ask("claim CLM-0012 query: discharge summary missing")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("claim_action", "c1", {"action": "query", "note": "discharge summary missing"})]
    assert _ask("withdraw Sita's claim")["state"] != "NEEDS_CONFIRMATION"   # the reason is asked, not guessed
    out = _ask("withdraw Sita's claim because the patient will pay cash")
    assert out["state"] == "NEEDS_CONFIRMATION", out


def test_insurance_needs_the_permission(db, ve):  # noqa: F811
    ve["user"] = AdminUser(username="nurse1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                           staff_role="NURSE", permissions=["IPD_NURSING"])
    assert _ask("add TPA Medi Assist")["state"] == "REJECTED"


# ── batch 8: discounts, packages, pre-authorisations ──────────────────────────

@fake.get("/admin/ipd/admissions/{adm}/running-bill")
async def _running_bill(adm: str):
    return {"payable_paise": 5_000_000, **S.setdefault("bill", {})}


@fake.post("/admin/ipd/admissions/{adm}/discounts", status_code=201)
async def _discount(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("discount", adm, body))
    S.setdefault("bill", {})["pending_discount"] = {"amount_paise": body["amount_paise"]}
    return {"id": "dc1", "status": "requested", **body}


@fake.get("/admin/ipd/discounts/pending")
async def _pending():
    return {"discounts": S.setdefault("pending", [{"id": "dc9", "admission_id": A2, "patient_name": "Sita Devi",
                                                   "amount_paise": 200000, "reason": "family hardship",
                                                   "requested_by_name": "cash1"}])}


@fake.post("/admin/ipd/discounts/{did}/decision")
async def _decide(did: str, request: Request):
    body = await request.json()
    S["writes"].append(("decision", did, body))
    S["pending"] = [d for d in S["pending"] if d["id"] != did]
    return {"id": did, "status": body["decision"]}


@fake.get("/admin/ipd/packages")
async def _packages():
    return {"packages": S.setdefault("packages", [{"id": "pk1", "code": "NDEL", "name": "Normal Delivery",
                                                   "price_paise": 2_500_000, "included_categories": ["bed", "nursing"],
                                                   "max_days": 3}])}


@fake.post("/admin/ipd/admissions/{adm}/package")
async def _apply(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("package", adm, body))
    S.setdefault("bill", {})["package"] = {"package_id": body["package_id"], "name": "Normal Delivery"}
    return {"ok": True}


def _preauths():
    return S.setdefault("preauths", [{"id": "pa1", "pre_auth_number": "PRE-0007", "admission_id": A2,
                                      "patient_name": "Sita Devi", "status": "submitted",
                                      "initial_requested_paise": 6_000_000, "enhanced_requested_paise": 0,
                                      "total_approved_paise": 0}])


@fake.get("/admin/insurance/pre-auths")
async def _preauth_list():
    return {"items": _preauths()}


@fake.patch("/admin/ipd/pre-auth/{pid}/decision")
async def _preauth_decide(pid: str, request: Request):
    body = await request.json()
    S["writes"].append(("preauth", pid, body))
    p = next(p for p in _preauths() if p["id"] == pid)
    p["status"] = body["decision"]
    if body["decision"] == "approved":
        p["total_approved_paise"] = body["approved_amount_paise"]
    return p


@fake.post("/admin/ipd/pre-auth/{pid}/action")
async def _preauth_act(pid: str, request: Request):
    body = await request.json()
    S["writes"].append(("preauth_action", pid, body))
    p = next(p for p in _preauths() if p["id"] == pid)
    p["status"] = {"submit": "submitted", "request_enhancement": "enhancement_requested"}[body["action"]]
    return p


def test_discount_request_typed_with_a_reason(db, ve):  # noqa: F811
    assert _ask("give 2000 discount to bed 12")["state"] != "NEEDS_CONFIRMATION"        # the reason is asked
    out = _ask("give 2000 discount to bed 12 because of family hardship")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 2000, out
    assert _confirm(out, typed_count=2000).json()["state"] == "COMPLETED_VERIFIED"
    (_, adm, body), = S["writes"]
    assert (adm, body["amount_paise"], body["reason"]) == (A2, 200000, "family hardship")


def test_discount_decision(db, ve):  # noqa: F811
    out = _ask("reject Sita's discount because it is too high")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 2000, out
    assert _confirm(out, typed_count=2000).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("decision", "dc9", {"decision": "rejected", "note": "it is too high"})]


def test_package_on_a_stay(db, ve):  # noqa: F811
    out = _ask("put bed 12 on the normal delivery package")
    assert out["state"] == "NEEDS_CONFIRMATION" and "₹25,000" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("package", A2, {"package_id": "pk1"})]
    assert _ask("put bed 12 on the normal delivery package")["state"] != "NEEDS_CONFIRMATION"  # already has one


def test_preauth_approval_is_the_total_with_its_reference(db, ve):  # noqa: F811
    asked = _ask("Star Health approved 50,000 for bed 12")   # the reference is asked, never guessed
    assert asked["state"] == "NEEDS_CLARIFICATION" and asked["cards"][0]["missing"] == ["ref"], asked
    out = _ask("Star Health approved 50,000 for bed 12, approval ref AP123")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 50000, out
    assert "TOTAL" in str(out["cards"])
    assert _confirm(out, typed_count=50000).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("preauth", "pa1", {"decision": "approved", "approved_amount_paise": 5_000_000,
                                               "approval_ref": "AP123"})]


def test_preauth_query_and_enhancement(db, ve):  # noqa: F811
    out = _ask("insurer queried bed 12 pre-auth: need ultrasound report")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("preauth", "pa1", {"decision": "query_raised", "note": "need ultrasound report"})]
    _preauths()[0]["status"] = "approved"
    out = _ask("request enhancement of 20,000 for bed 12")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"][-1] == ("preauth_action", "pa1", {"action": "request_enhancement", "amount_paise": 2_000_000})


# ── batch 9: package create, credit, new pre-auth, disallowances ───────────────

@fake.post("/admin/ipd/packages", status_code=201)
async def _package_add(request: Request):
    body = await request.json()
    S["writes"].append(("package_add", body))
    S["packages"].append({"id": "pk2", **body})
    return S["packages"][-1]


@fake.post("/admin/ipd/admissions/{adm}/credit", status_code=201)
async def _credit(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("credit", adm, body))
    S.setdefault("bill", {})["credit"] = {"amount_paise": body["amount_paise"]}
    return {"id": "cr1", **body}


@fake.get("/admin/insurance/policies/{pid}")
async def _policies(pid: str):
    return {"policies": S.setdefault("policies", [
        {"id": "po1", "patient_id": pid, "family_member_id": None, "policy_number": "SH-998", "is_valid_today": True,
         "payer": {"name": "Star Health"}}])}


@fake.post("/admin/ipd/admissions/{adm}/pre-auth", status_code=201)
async def _preauth_create(adm: str, request: Request):
    body = await request.json()
    S["writes"].append(("preauth_create", adm, body))
    _preauths().append({"id": "pa2", "pre_auth_number": "PRE-0008", "admission_id": adm, "patient_name": "Sita Devi",
                        "status": "submitted", "initial_requested_paise": body["requested_paise"],
                        "enhanced_requested_paise": 0, "total_approved_paise": 0})
    return _preauths()[-1]


def test_create_a_package(db, ve):  # noqa: F811
    out = _ask("create a package Cataract Surgery at 18,000 covering procedure, consumables and pharmacy for 2 days")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("package_add", {"code": "CATARACT-SURGERY", "name": "Cataract Surgery", "price_paise": 1_800_000,
                                            "included_categories": ["procedure", "pharmacy", "consumable"], "max_days": 2})]


def test_credit_authorisation_typed(db, ve):  # noqa: F811
    assert _ask("authorise credit of 50,000 for bed 12 from Infosys")["state"] != "NEEDS_CONFIRMATION"  # reference asked
    out = _ask("authorise credit of 50,000 for bed 12 from Infosys, reference PO-778")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 50000, out
    assert _confirm(out, typed_count=50000).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("credit", A2, {"payer_name": "Infosys", "reference": "PO-778", "amount_paise": 5_000_000})]


def test_new_preauth_uses_the_patients_valid_policy(db, ve):  # noqa: F811
    S["admissions"][A2]["patient_id"] = "pat9"
    out = _ask("raise pre-auth of 60,000 for bed 12 for dengue, 3 days")
    assert out["state"] == "NEEDS_CONFIRMATION" and "SH-998" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("preauth_create", A2, {"policy_id": "po1", "requested_paise": 6_000_000,
                                                   "provisional_diagnosis": "dengue", "expected_stay_days": 3})]


def test_new_preauth_with_two_policies_asks_which(db, ve):  # noqa: F811
    S["admissions"][A2]["patient_id"] = "pat9"
    S["policies"] = [{"id": "po1", "family_member_id": None, "policy_number": "SH-998", "is_valid_today": True,
                      "payer": {"name": "Star Health"}},
                     {"id": "po2", "family_member_id": None, "policy_number": "MA-12", "is_valid_today": True,
                      "payer": {"name": "Medi Assist"}}]
    assert _ask("raise pre-auth of 60,000 for bed 12 for dengue")["state"] != "NEEDS_CONFIRMATION"
    out = _ask("raise pre-auth of 60,000 for bed 12 under Medi Assist for dengue")
    assert out["state"] == "NEEDS_CONFIRMATION" and "MA-12" in str(out["cards"]), out


def test_disallowance_reason_coded_and_typed(db, ve):  # noqa: F811
    out = _ask("Star Health disallowed 2,000 on claim CLM-0012 for non-payable items")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["cards"][0]["typed_count"] == 2000, out
    assert _confirm(out, typed_count=2000).json()["state"] == "COMPLETED_VERIFIED"
    (_, cid, body), = S["writes"]
    assert body["disallowances"] == [{"amount_paise": 200000, "reason_code": "non_payable_item"}]
    assert body["amount_received_paise"] == 0
