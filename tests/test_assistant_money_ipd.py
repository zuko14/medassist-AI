"""Session 55: money and inpatient tasks by voice, end to end through /admin/assistant (a fake hospital behind it).

Every money step is R3 (the rupees are typed to confirm) with a key derived from what was read, so a repeated
confirm never writes twice; every preview refuses what the server would refuse, before anything is sent.
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

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)
TODAY = "2026-11-02"
CLINIC = {"id": CID, "plan": "diagbooking", "account_type": "tenant", "opd_state": "READY",
          "features": {"kriya_ketu": True, "opd_enabled": True, "booking": True, "ipd_enabled": True}}

fake = FastAPI()
S: dict = {}


def _reset(**kw):
    S.clear()
    S.update(writes=[], me={"branch_id": None, "role": "clinic_admin", "permissions": []},
             beds=[{"id": "bed5", "code": "5", "status": "occupied", "is_active": True,
                    "occupant": {"admission_id": "A5", "patient_name": "Ravi Kumar"}},
                   {"id": "bed7", "code": "7", "status": "occupied", "is_active": True,
                    "occupant": {"admission_id": "A7", "patient_name": "Lakshmi Devi"}},
                   {"id": "bed12", "code": "12", "status": "available", "is_active": True, "bed_category": "general"},
                   {"id": "bed14", "code": "14", "status": "cleaning", "is_active": True}],
             adm={"A5": {"id": "A5", "status": "admitted"}, "A7": {"id": "A7", "status": "discharge_pending"},
                  "P1": {"id": "P1", "status": "pending"}},
             pending=[{"id": "P1", "patient_name": "Sita Devi", "status": "pending", "admission_number": "IP-9"}],
             charges={"A5": [{"id": "c1", "description": "Dressing", "quantity": 1, "unit_price_paise": 50000,
                              "status": "proposed", "service_date": TODAY, "invoice_id": None, "source_ref": "x"}],
                      "A7": []},
             bills={"A5": [], "A7": []}, package={"A5": {"name": "Normal Delivery"}, "A7": None},
             deposits={"A5": [{"id": "d1", "kind": "deposit", "mode": "cash", "amount_paise": 1000000,
                               "idempotency_key": "k0"}], "A7": []},
             shift=None)
    S.update(kw)


def _balance(a):
    return sum((1 if d["kind"] == "deposit" else -1) * d["amount_paise"] for d in S["deposits"][a])


@fake.get("/admin/me")
async def _me():
    return S["me"]


@fake.get("/admin/ipd/board")
async def _board():
    return {"wards": [{"name": "General", "beds": S["beds"]}]}


@fake.get("/admin/ipd/admissions")
async def _admissions(status: str = ""):
    return {"admissions": [a for a in S["pending"] if not status or a["status"] in status.split(",")]}


@fake.get("/admin/ipd/admissions/{a}")
async def _adm(a: str):
    return {"admission": S["adm"][a], "charges": S["charges"].get(a, []), "bills": S["bills"].get(a, [])}


@fake.post("/admin/ipd/admissions/{a}/charges")
async def _charge(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("charge", a, body))
    S["charges"][a].append({"id": f"c{len(S['charges'][a]) + 9}", "status": "proposed", "invoice_id": None, **body})
    return S["charges"][a][-1]


@fake.post("/admin/ipd/charges/{cid}/void")
async def _void(cid: str, request: Request):
    body = await request.json()
    S["writes"].append(("void", cid, body))
    for rows in S["charges"].values():
        for c in rows:
            if c["id"] == cid:
                c["status"] = "void"
    return {"ok": True}


@fake.post("/admin/ipd/admissions/{a}/charges/post")
async def _post(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("post", a, body))
    for c in S["charges"][a]:
        if c["id"] in body["charge_ids"]:
            c["status"] = "posted"
    return {"posted": len(body["charge_ids"])}


@fake.post("/admin/ipd/admissions/{a}/final-bill")
async def _final(a: str):
    S["writes"].append(("final", a))
    S["bills"][a].append({"id": "inv1", "invoice_number": "INV-1", "status": "issued", "total_paise": 300000})
    return {"invoice_id": "inv1"}


@fake.get("/admin/ipd/admissions/{a}/running-bill")
async def _running(a: str):
    prop = [c for c in S["charges"][a] if c["status"] == "proposed"]
    return {"proposed": {"count": len(prop), "total_paise": sum(c["unit_price_paise"] * c["quantity"] for c in prop)},
            "package": S["package"].get(a), "deposits": {"balance_paise": _balance(a)}, "total_paise": 300000,
            "payable_paise": 300000, "refundable_paise": max(_balance(a) - 300000, 0)}


@fake.get("/admin/ipd/admissions/{a}/deposits")
async def _deposits(a: str):
    return {"deposits": S["deposits"][a]}


@fake.post("/admin/ipd/admissions/{a}/deposits/refund")
async def _refund(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("refund", a, body))
    S["deposits"][a].append({"id": "r1", "kind": "refund", **body})
    return S["deposits"][a][-1]


@fake.post("/admin/ipd/admissions/{a}/assign-bed")
async def _assign(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("assign", a, body))
    S["adm"][a]["status"] = "admitted"
    return S["adm"][a]


@fake.post("/admin/ipd/admissions/{a}/cancel")
async def _cancel(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("cancel", a, body))
    S["adm"][a]["status"] = "cancelled"
    return S["adm"][a]


@fake.post("/admin/ipd/admissions/{a}/package/remove")
async def _unpackage(a: str, request: Request):
    body = await request.json()
    S["writes"].append(("unpackage", a, body))
    S["package"][a] = None
    return {"ok": True}


@fake.get("/admin/opd/shifts/current")
async def _current():
    return S["shift"]


@fake.post("/admin/opd/shifts/open")
async def _open(request: Request):
    body = await request.json()
    S["writes"].append(("open", body))
    S["shift"] = {"id": "sh1", "status": "open", "expected_cash_paise": body["opening_float_paise"]}
    return S["shift"]


@fake.post("/admin/opd/shifts/{sid}/close")
async def _close(sid: str, request: Request):
    body = await request.json()
    S["writes"].append(("close", sid, body))
    S["shift"] = None
    return {"id": sid, "status": "closed"}


client = TestClient(app)


def _staff(*perms):
    return AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="FRONT_DESK",
                     permissions=list(perms))


@pytest.fixture
def mi(as_user):  # noqa: F811
    as_user["user"] = AdminUser(username="owner", role="clinic_admin", clinic_id=CID, user_id=str(uuid.uuid4()),
                                staff_role=None, permissions=[])
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch.object(playbooks, "_now", lambda: NOW), patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="dashboard"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "voice",
        "ui_context": {"page": page}}).json()


def _confirm(out, typed=None):
    body = {"plan_hash": out["plan_hash"]}
    if typed is not None:
        body["typed_count"] = typed
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json=body)


def _done(text, typed=None):
    out = _ask(text)
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert S["writes"] == []
    if typed is not None:  # R3: the wrong number is refused, nothing is sent
        bad = _confirm(out, typed + 1)
        assert bad.status_code == 422 and S["writes"] == []
    done = _confirm(out, typed).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    return out


# ── charges ──────────────────────────────────────────────────────────────────

def test_add_a_charge(db, mi):  # noqa: F811
    out = _done("add 2 injections at 150 each for bed 5", typed=300)
    assert "2 × ₹300" not in str(out["cards"]) and "2 × ₹150 = ₹300" in str(out["cards"])
    (_, adm, body), = S["writes"]
    assert adm == "A5" and {k: body[k] for k in ("category", "description", "quantity", "unit_price_paise",
                                                 "service_date")} == \
        {"category": "pharmacy", "description": "Injection", "quantity": 2, "unit_price_paise": 15000,
         "service_date": TODAY}
    assert len(body["source_ref"]) >= 8


def test_the_same_charge_twice_gets_two_keys_never_a_silent_merge(db, mi):  # noqa: F811
    _done("add a dressing charge of 500 to bed 5", typed=500)
    first = S["writes"][:]
    S["writes"].clear()
    _done("add a dressing charge of 500 to bed 5", typed=500)
    refs = [w[2]["source_ref"] for w in first + S["writes"]]
    assert len(set(refs)) == 2  # asked twice, confirmed twice: two charges, each idempotent on its own


def test_void_a_charge(db, mi):  # noqa: F811
    _done("remove the dressing charge from bed 5 because it was entered twice", typed=500)
    assert S["writes"] == [("void", "c1", {"reason": "it was entered twice"})]


def test_a_billed_charge_is_never_voided(db, mi):  # noqa: F811
    S["charges"]["A5"][0]["invoice_id"] = "inv0"
    out = _ask("remove the dressing charge from bed 5 because it was entered twice")
    assert out["error_code"] == "nothing_to_do" and "credit note" in out["message"] and S["writes"] == []


def test_post_charges(db, mi):  # noqa: F811
    _done("post the charges for bed 5", typed=500)
    assert S["writes"] == [("post", "A5", {"charge_ids": ["c1"]})]


def test_nothing_to_post(db, mi):  # noqa: F811
    S["charges"]["A5"] = []
    assert _ask("post the charges for bed 5")["error_code"] == "nothing_to_do"


# ── final bill ───────────────────────────────────────────────────────────────

def test_final_bill_after_discharge_starts(db, mi):  # noqa: F811
    _done("make the final bill for bed 7")
    assert S["writes"] == [("final", "A7")]


def test_final_bill_needs_the_discharge_started(db, mi):  # noqa: F811
    S["charges"]["A5"] = []
    out = _ask("make the final bill for bed 5")
    assert out["error_code"] == "nothing_to_do" and "Start the discharge" in out["message"]


def test_final_bill_waits_for_proposed_charges(db, mi):  # noqa: F811
    S["charges"]["A7"] = [{"id": "c7", "description": "X-ray", "quantity": 1, "unit_price_paise": 80000,
                           "status": "proposed", "invoice_id": None}]
    out = _ask("make the final bill for bed 7")
    assert out["error_code"] == "nothing_to_do" and "post the charges" in out["message"]


def test_a_second_final_bill_is_refused(db, mi):  # noqa: F811
    S["bills"]["A7"] = [{"id": "inv1", "invoice_number": "INV-1", "status": "issued", "total_paise": 300000}]
    assert "already has bill INV-1" in _ask("make the final bill for bed 7")["message"]


# ── deposits ─────────────────────────────────────────────────────────────────

def test_refund_a_deposit(db, mi):  # noqa: F811
    _done("refund 2000 deposit to bed 5 in cash because the patient is shifting hospitals", typed=2000)
    (_, adm, body), = S["writes"]
    assert adm == "A5" and {k: body[k] for k in ("mode", "amount_paise", "reason")} == \
        {"mode": "cash", "amount_paise": 200000, "reason": "the patient is shifting hospitals"}


def test_refund_more_than_held_is_refused(db, mi):  # noqa: F811
    out = _ask("refund 20000 deposit to bed 5 in cash because the patient is shifting hospitals")
    assert out["error_code"] == "nothing_to_do" and "unused" in out["message"] and S["writes"] == []


def test_upi_refund_needs_its_reference(db, mi):  # noqa: F811
    out = _ask("refund 2000 deposit to bed 5 by upi because the patient is shifting hospitals")
    assert out["error_code"] == "nothing_to_do" and "UTR" in out["message"]


def test_refund_needs_ipd_admin(db, mi):  # noqa: F811
    mi["user"] = _staff("IPD_BILLING")
    out = _ask("refund 2000 deposit to bed 5 in cash because the patient is shifting hospitals")
    assert out["state"] == "REJECTED" and S["writes"] == []


# ── waiting list, package ────────────────────────────────────────────────────

def test_give_a_waiting_patient_a_bed(db, mi):  # noqa: F811
    _done("give bed 12 to Sita")
    assert S["writes"] == [("assign", "P1", {"bed_id": "bed12"})]


def test_a_bed_that_is_not_free_is_refused(db, mi):  # noqa: F811
    out = _ask("give bed 14 to Sita")
    assert out["error_code"] == "nothing_to_do" and "cleaning" in out["message"]


def test_cancel_a_waiting_admission(db, mi):  # noqa: F811
    _done("cancel Sita's admission because she went home")
    assert S["writes"] == [("cancel", "P1", {"reason": "she went home"})]


def test_only_a_waiting_patient_can_be_cancelled(db, mi):  # noqa: F811
    out = _ask("cancel Ravi's admission because he went home")
    assert out["error_code"] == "nothing_to_do" and "waiting for a bed" in out["message"]


def test_take_a_stay_off_its_package(db, mi):  # noqa: F811
    _done("take bed 5 off the package because the surgery was not done")
    assert S["writes"] == [("unpackage", "A5", {"reason": "the surgery was not done"})]


def test_no_package_is_nothing_to_do(db, mi):  # noqa: F811
    assert _ask("take bed 7 off the package because the surgery was not done")["error_code"] == "nothing_to_do"


# ── cash drawer ──────────────────────────────────────────────────────────────

def test_open_and_close_the_cash_drawer(db, mi):  # noqa: F811
    _done("open my cash counter with 2000 float")
    assert S["writes"] == [("open", {"opening_float_paise": 200000})]
    S["writes"].clear()
    S["shift"]["expected_cash_paise"] = 1550000
    out = _done("close my shift, cash counted 15400", typed=15400)
    assert "₹100 short" in str(out["cards"]) and "Expected in the drawer" in str(out["cards"])
    assert S["writes"] == [("close", "sh1", {"declared_cash_paise": 1540000})]


def test_a_second_open_is_refused(db, mi):  # noqa: F811
    S["shift"] = {"id": "sh1", "status": "open", "expected_cash_paise": 0}
    assert _ask("open my shift")["error_code"] == "nothing_to_do"


def test_closing_without_a_shift_is_refused(db, mi):  # noqa: F811
    assert _ask("close my shift, cash counted 15400")["error_code"] == "nothing_to_do"


# ── routing guards ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("make the bill for Ravi", "opd.invoice_create"), ("take a deposit of 10000 from bed 12 by cash", "ipd_finance.deposit"),
    ("refund 300 to Ravi in cash because the test was not done", "opd.refund"),
    ("put bed 12 on the normal delivery package", "ipd.package_apply"), ("admit Ravi under Dr Anitha for fever", "ipd.admit"),
    ("move bed 12 to bed 15", "ipd.transfer"), ("mark bed 12 cleaned", "ipd.bed_status"),
    ("change the ward charges to 2000", "services.price"), ("cancel Ravi's appointment", "appointment.cancel"),
    ("cancel the surgery for bed 5", "ot.status"), ("add 5 beds to the general ward", "ipd.beds_create"),
    ("Ravi paid 500 cash", "opd.record_payment"), ("end of day summary", "briefing.eod"),
    ("turn off auto open shift", "opd.setting"), ("automatically open the shift after check in", "opd.setting"),
])
def test_neighbours_keep_their_sentences(text, action):
    assert rules.route(text, doctors=[{"id": "d", "name": "Dr. Anitha Rao"}], now=NOW).action_id == action


@pytest.mark.parametrize("text", ["return the advance of 5000 to bed 7 by upi UTR 123456789012 as the surgery was cancelled",
                                  "take bed 5 off the package because the surgery was not done"])
def test_a_reason_never_names_the_task(text):
    assert rules.route(text, now=NOW).kind == "action"  # the reason's "surgery" never ties with OT actions


def test_charge_reader():
    want = {"description": "X-ray", "category": "investigation", "quantity": 1, "unit_rupees": 800}
    assert resolve.resolve_ipd_charge("bed 5 x-ray charge 800") == want
    assert resolve.resolve_ipd_charge("add a charge to bed 5") is None  # nothing said: the form asks


def test_every_money_move_is_typed_to_confirm():
    for aid in ("ipd.charge_add", "ipd.charge_void", "ipd.charges_post", "ipd_finance.deposit_refund", "opd.shift_close"):
        assert REGISTRY[aid].risk == "R3", aid
    for aid in ("ipd.final_bill", "ipd.assign_bed", "ipd.admission_cancel", "ipd.package_remove", "opd.shift_open"):
        assert REGISTRY[aid].risk == "R2", aid


def test_no_top_level_name_is_defined_twice():
    """A second top-level definition silently replaces the first (session 55 caught `_SHIFTS`: the cash drawer's path
    replaced the doctor's shift fields and broke "change Dr …'s hours")."""
    import ast
    from pathlib import Path
    for rel in ("app/assistant/playbooks.py", "app/assistant/registry.py", "app/assistant/resolve.py",
                "app/assistant/rules.py"):
        tree = ast.parse((Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8"))
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names += [t.id for t in node.targets if isinstance(t, ast.Name) and t.id.isupper() or
                          isinstance(t, ast.Name) and t.id.startswith("_") and t.id[1:].isupper()]
        dupes = {n for n in names if names.count(n) > 1} - {"REGISTRY"}  # REGISTRY is rebuilt once, on purpose
        assert not dupes, (rel, dupes)
