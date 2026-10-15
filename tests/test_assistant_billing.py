"""KK-P4-15: OPD billing (R2), online-booking refund (R3, admin, typed amount), home collection assignment (R2).

PAY-01 invoice create (today's visit from the live queue) and issue (a named draft: items and total previewed).
PAY-02 payment link for an issued invoice with a balance (not idempotent: never resent, read from the response).
PAY-03 refund: admin only, single booking, the typed number is the amount in rupees; read back on the
       Appointments list (refund_state); a failed refund is never shown as done.
PAY-04 booking confirm / reject: built in KK-P4-05 (tests/test_assistant_desk.py).
HC-01 auto-assign: the service skips phlebotomists on leave; the response's counts are the result.
HC-02 assign one visit: a phlebotomist on leave that day is refused before any write; read back on the visits list.
Design deviation: invoice create and issue are two actions (the issue path needs the new invoice's id, which a
plan fixed at preview time cannot hold); cash, OPD refunds and voids stay hand-offs (KK-P4-12).
"""

import json
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
from app.voice.dates import IST
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
TODAY = "2026-10-15"
CLINIC = {"id": CID, "plan": "diagbooking", "account_type": "tenant", "opd_state": "READY",
          "features": {"kriya_ketu": True, "opd_enabled": True, "booking": True}}


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("make the bill for Ravi Kumar", "opd.invoice_create"),
    ("issue Ravi's invoice", "opd.invoice_issue"),
    ("send payment link to Ravi", "opd.payment_link"),
    ("refund Ravi's booking", "booking.refund"),
    ("assign Lakshmi's home visit to Kiran", "homecollect.assign"),
    ("auto assign home visits", "homecollect.auto_assign"),
    ("refund the invoice for Ravi", "opd.refund"),                     # OPD refunds stay a hand-off
])
def test_routes(text, action):
    assert rules.route(text, now=NOW).action_id == action


@pytest.mark.parametrize("text, who", [
    ("make the bill for Ravi Kumar", "ravi kumar"), ("issue Ravi's invoice", "ravi"),
    ("send payment link to Ravi", "ravi"), ("refund Ravi's booking", "ravi"),
])
def test_billing_words_are_not_part_of_the_name(text, who):
    assert resolve.resolve_booking(text) == who


def test_visit_and_phlebotomist_come_from_the_text():
    assert rules.route("assign Lakshmi's home visit to Kiran", now=NOW).params == {"who": "lakshmi", "phleb": "kiran"}


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(**kw):
    S.clear()
    S.update(queue=[], invoices=[], appts=[], visits=[], phlebs=[], writes=[], refund_ok=True, link_ok=True,
             auto=None)
    S.update({k: [dict(x) for x in v] if isinstance(v, (list, tuple)) else v for k, v in kw.items()})


def _w(*x):
    S["writes"].append(x)


@fake.get("/admin/opd/queue")
async def _queue():
    return {"doctors": [{"doctor_name": "Dr. Rao", "rows": S["queue"]}]}


@fake.post("/admin/opd/appointments/{aid}/invoice")
async def _create(aid: str):
    _w("create", aid)
    row = next((r for r in S["queue"] if r["appointment_id"] == aid), {})
    inv = {"id": f"inv{len(S['invoices']) + 1}", "appointment_id": aid, "status": "draft", "invoice_number": None,
           "patient_snapshot": {"name": row.get("patient_name")}, "total_paise": 50000, "paid_paise": 0,
           "items": [{"description": "Consultation", "quantity": 1, "line_total_paise": 50000}]}
    S["invoices"].append(inv)
    return inv


@fake.get("/admin/opd/invoices")
async def _invoices(request: Request):
    q = dict(request.query_params)
    rows = [i for i in S["invoices"] if (not q.get("status") or i["status"] == q["status"])
            and (not q.get("q") or q["q"].lower() in (i["patient_snapshot"].get("name") or "").lower())]
    return rows


@fake.get("/admin/opd/invoices/{iid}")
async def _invoice(iid: str):
    inv = next((i for i in S["invoices"] if i["id"] == iid), None)
    if not inv:
        raise HTTPException(status_code=404)
    return inv


@fake.post("/admin/opd/invoices/{iid}/issue")
async def _issue(iid: str):
    _w("issue", iid)
    inv = next(i for i in S["invoices"] if i["id"] == iid)
    inv.update(status="issued", invoice_number="INV-2026-0007")
    return inv


@fake.post("/admin/opd/invoices/{iid}/payment-link")
async def _link(iid: str):
    _w("link", iid)
    if not S["link_ok"]:
        raise HTTPException(status_code=502)
    return {"url": "https://pay.example/x", "id": "plink_1"}


@fake.get("/admin/appointments")
async def _appts(request: Request):
    q = dict(request.query_params)
    rows = [a for a in S["appts"] if (not q.get("status") or a["status"] == q["status"])
            and (not q.get("q") or q["q"].lower() in (a["patient_name"].lower() + " " + a["booking_ref"].lower()))]
    return {"appointments": rows, "total": len(rows)}


@fake.post("/admin/bookings/{bid}/refund")
async def _refund(bid: str, request: Request):
    _w("refund", bid, await request.json())
    a = next(a for a in S["appts"] if a["id"] == bid)
    if not S["refund_ok"]:
        a["refund_state"] = "failed"
        raise HTTPException(status_code=400, detail="The payment gateway did not accept the refund.")
    a.update(status="refunded", refund_state="refunded")
    return {"success": True, "refund_id": "rfnd_1"}


@fake.get("/admin/home-collection/visits")
async def _visits(date: str = TODAY):
    return {"date": date, "visits": [v for v in S["visits"] if v["appointment_date"] == date]}


@fake.get("/admin/home-collection/phlebotomists")
async def _phlebs():
    return {"phlebotomists": S["phlebs"]}


@fake.post("/admin/home-collection/visits/{vid}/assign")
async def _assign(vid: str, request: Request):
    body = await request.json()
    _w("assign", vid, body)
    next(v for v in S["visits"] if v["id"] == vid)["phlebotomist_id"] = body["phlebotomist_id"]
    return {"success": True}


@fake.post("/admin/home-collection/auto-assign")
async def _auto():
    _w("auto")
    return S["auto"] or {"success": True, "assigned": 0, "remaining": 0}


client = TestClient(app)


@pytest.fixture
def bl(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=CLINIC)):
        yield as_user


def _ask(text, page="opdbilling"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm",
                       json={"plan_hash": out["plan_hash"], **extra}).json()


def _card(out):
    return out["cards"][0]


Q = {"appointment_id": "a1", "patient_name": "Ravi Kumar", "token_number": 4, "stage": "billing"}


def _inv(iid="inv1", status="draft", name="Ravi Kumar", paid=0):
    return {"id": iid, "appointment_id": "a1", "status": status, "invoice_number": None if status == "draft" else "INV-1",
            "patient_snapshot": {"name": name}, "total_paise": 50000, "paid_paise": paid,
            "items": [{"description": "Consultation", "quantity": 1, "line_total_paise": 50000}]}


# PAY-01

def test_invoice_create_for_todays_visit(db, bl):  # noqa: F811
    _reset(queue=[Q])
    out = _ask("make the bill for Ravi Kumar")
    assert out["state"] == "NEEDS_CONFIRMATION" and "token 4" in json.dumps(_card(out)).lower(), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("create", "a1")]


def test_invoice_create_refuses_a_visit_that_already_has_one(db, bl):  # noqa: F811
    _reset(queue=[Q], invoices=[_inv()])
    out = _ask("make the bill for Ravi Kumar")
    assert out["error_code"] == "nothing_to_do" and "already" in out["message"] and S["writes"] == []


def test_invoice_issue_previews_items_and_total(db, bl):  # noqa: F811
    _reset(invoices=[_inv()])
    out = _ask("issue Ravi's invoice")
    text = json.dumps(_card(out), ensure_ascii=False)
    assert out["state"] == "NEEDS_CONFIRMATION" and "Consultation" in text and "500" in text, out
    done = _confirm(out)
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("issue", "inv1")], done
    assert "INV-2026-0007" in json.dumps(done["cards"])


def test_two_drafts_for_the_name_are_listed_not_guessed(db, bl):  # noqa: F811
    _reset(invoices=[_inv("inv1"), _inv("inv2", name="Ravi Teja")])
    out = _ask("issue Ravi's invoice")
    assert out["error_code"] == "nothing_to_do" and "Ravi Teja" in out["message"] and S["writes"] == []


# PAY-02

def test_payment_link_for_an_issued_invoice_with_a_balance(db, bl):  # noqa: F811
    _reset(invoices=[_inv(status="issued")])
    out = _ask("send payment link to Ravi")
    assert out["state"] == "NEEDS_CONFIRMATION" and "WhatsApp" in json.dumps(_card(out)), out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("link", "inv1")]


def test_no_link_for_a_paid_or_draft_invoice(db, bl):  # noqa: F811
    _reset(invoices=[_inv(status="paid", paid=50000), _inv("inv2", status="draft")])
    out = _ask("send payment link to Ravi")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_a_link_that_may_have_been_made_is_never_resent(db, bl):  # noqa: F811
    _reset(invoices=[_inv(status="issued")], link_ok=False)
    done = _confirm(_ask("send payment link to Ravi"))
    assert done["state"] in ("UNKNOWN_OUTCOME", "FAILED") and S["writes"] == [("link", "inv1")]


# PAY-03

def _booking(**kw):
    return {"id": "b1", "booking_ref": "MC-2026-ABCD2345", "patient_name": "Ravi Kumar", "status": "confirmed",
            "appointment_date": "2026-10-20", "appointment_time": "10:00", "doctor_name": "Dr. Rao",
            "payment_id": "pay_1", "amount_paise": 50000, "refund_state": "none", **kw}


def test_refund_needs_the_typed_amount_then_reads_back(db, bl):  # noqa: F811
    _reset(appts=[_booking()])
    out = _ask("refund Ravi's booking", page="payments")
    card = _card(out)
    assert (out["state"], card["type"], card["typed_count"]) == ("NEEDS_CONFIRMATION", "preview-typed", 500), out
    wrong = client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"],
                                                                               "typed_count": 1})
    assert wrong.status_code == 422 and S["writes"] == []
    done = _confirm(out, typed_count=500)
    assert done["state"] == "COMPLETED_VERIFIED" and [w[0] for w in S["writes"]] == ["refund"], done


def test_a_refused_refund_is_never_shown_as_done(db, bl):  # noqa: F811
    _reset(appts=[_booking()], refund_ok=False)
    done = _confirm(_ask("refund Ravi's booking", page="payments"), typed_count=500)
    assert done["state"] == "FAILED" and len(S["writes"]) == 1


def test_an_unpaid_booking_has_nothing_to_refund(db, bl):  # noqa: F811
    _reset(appts=[_booking(payment_id=None)])
    out = _ask("refund Ravi's booking", page="payments")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_staff_cannot_refund(db, bl):  # noqa: F811
    bl["user"] = _user()
    out = _ask("refund Ravi's booking", page="payments")
    assert out["state"] == "REJECTED" and "admin" in out["message"].lower() and S["writes"] == []


# HC-02 / HC-01

PH = [{"id": "p1", "name": "Kiran", "off_on_date": False, "is_active": True},
      {"id": "p2", "name": "Suma", "off_on_date": True, "is_active": True}]


def _visit(vid="v1", name="Lakshmi", day=TODAY, phleb=None):
    return {"id": vid, "patient_name": name, "appointment_date": day, "status": "confirmed",
            "collection_slot": "07:00-08:00", "phlebotomist_id": phleb, "lab_test_name": "HbA1c"}


def test_assign_a_visit_and_read_it_back(db, bl):  # noqa: F811
    _reset(visits=[_visit()], phlebs=PH)
    out = _ask("assign Lakshmi's home visit to Kiran", page="homecollect")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("assign", "v1", {"phlebotomist_id": "p1"})]


def test_a_phlebotomist_on_leave_is_refused(db, bl):  # noqa: F811
    _reset(visits=[_visit()], phlebs=PH)
    out = _ask("assign Lakshmi's home visit to Suma", page="homecollect")
    assert out["error_code"] == "nothing_to_do" and "leave" in out["message"] and S["writes"] == []


def test_auto_assign_previews_the_unassigned_visits_and_reports_the_result(db, bl):  # noqa: F811
    _reset(visits=[_visit(), _visit("v2", "Ravi", "2026-10-16"), _visit("v3", "Sita", phleb="p1")], phlebs=PH,
           auto={"success": True, "assigned": 1, "remaining": 1})
    out = _ask("auto assign home visits", page="homecollect")
    text = json.dumps(_card(out))
    assert out["state"] == "NEEDS_CONFIRMATION" and "2 " in text and "later" in text.lower(), out
    done = _confirm(out)
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [("auto",)], done
    assert "1 assigned" in json.dumps(done["cards"])


def test_auto_assign_with_nothing_unassigned_is_refused(db, bl):  # noqa: F811
    _reset(visits=[_visit(phleb="p1")], phlebs=PH)
    assert _ask("auto assign home visits", page="homecollect")["error_code"] == "nothing_to_do" and S["writes"] == []


# ── registry + corpus ────────────────────────────────────────────────────────

ACTIONS = {"opd.invoice_create": ("R2", "OPD_BILLING", "handler"), "opd.invoice_issue": ("R2", "OPD_BILLING", "handler"),
           "opd.payment_link": ("R2", "OPD_BILLING", "handler"), "booking.refund": ("R3", None, "role"),
           "homecollect.assign": ("R2", "HOME_COLLECTION_MANAGE", "handler"),
           "homecollect.auto_assign": ("R2", "HOME_COLLECTION_MANAGE", "handler")}


@pytest.mark.parametrize("aid", sorted(ACTIONS))
def test_registry_shape(aid):
    spec = REGISTRY[aid]
    assert (spec.risk, spec.permission, spec.perm_source) == ACTIONS[aid]


@pytest.mark.parametrize("aid", sorted(ACTIONS))
def test_corpus_covers_each_action(aid):
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    # te/hi word order ("Lakshmi visit Kiran ki assign cheyyi") asks for the names on the form: tagged clarify.
    pos = [e for e in corpus if e["expect"] == f"action:{aid}" or e.get("action") == aid]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}, aid
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2, aid
