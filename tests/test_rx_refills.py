"""Chronic refills end to end on real Postgres (W10; RX-57, RX-58, RX-59, RX-68 part 2).

Plans only from the closed frequency map, only for chronic non-H1/X/narcotic/psychotropic items, only with the
pharmacist's tick; the 09:30 job sends the clinic's own template to ACTIVE clinics' patients who never sent STOP and
expires a plan after three failed sends; the two button payloads are the only inbound text claimed (plan must match
the clinic AND the sender's phone, X17); verify → pack (draft sale, FEFO) → complete fulfils, reject cancels; DPDP
erasure removes plans and requests but never a register row.
"""

import asyncio
import inspect
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services import rx_refills
from tests.test_ipd_api_e2e import _clinic, _code, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m5 import draft, line, signed_rx

TODAY = date.today()


@pytest.mark.parametrize("dosage,freq,want", [
    ("1", "OD", 1), ("2", "TDS", 6), ("0.5", "BD", 1), ("1.5", "QID", 6), ("1", "hs", 1),
    ("1", "SOS", None), ("1/2", "BD", None), ("1 tab", "BD", None), ("1", "TID", None), ("0", "OD", None),
    ("1.25", "OD", None), (None, "OD", None), ("1", None, None)])
def test_units_per_day_closed_map_only(dosage, freq, want):
    got = rx_refills.units_per_day(dosage, freq)
    assert got == (None if want is None else Decimal(want))


@pytest.fixture
def ref(api, monkeypatch):  # noqa: F811
    """A pharmacy clinic with refills on and its own WhatsApp number; a signed Rx (Dolo 1 BD × 180 days) for a
    patient (6 months, so refills stay inside it); Dolo is chronic, stocked at 200 paise a tablet."""
    c, conn = _clinic(api.conn), api.conn
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    _q(conn, "UPDATE clinics SET rx_settings = rx_settings || '{\"refill_enabled\": true, \"refill_lead_days\": 5}', "
             "config = COALESCE(config, '{}'::jsonb) || '{\"meta_access_token\": \"t\", \"meta_phone_number_id\": \"p\"}' "
             "WHERE id = %s", c)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    store = str(_q(conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    g = _generic(conn, c, "Paracetamol", "650 mg")
    dolo = _item(conn, c, g, "DOLO650", "Dolo 650", pack_size=15, is_chronic=True)
    batch = _batch(conn, c, dolo, "D1", mrp=3000)
    _move(conn, c, store, batch, dolo, 500, "opening")
    rx = signed_rx(conn, c, lines=(("Dolo 650", "tablet", "1", "BD", 180), ("Dolo 650", "tablet", "1", "SOS", 5)))
    mgr = api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER")
    sent: list = []

    async def _tpl(clinic, phone, name, components=None, _source=None, _capture=None, **kw):
        sent.append({"clinic": clinic["id"], "phone": phone, "name": name, "components": components})
        if _capture is not None:
            _capture["meta_message_id"] = "wamid.TEST"
        return ref_state["ok"]
    ref_state = {"ok": True}
    from app.services.whatsapp import whatsapp_service
    monkeypatch.setattr(whatsapp_service, "send_template", _tpl)
    monkeypatch.setattr(whatsapp_service, "send_text", AsyncMock(return_value=True))
    return {"clinic": c, "store": store, "item": dolo, "batch": batch, "rx": rx, "mgr": mgr, "sent": sent,
            "state": ref_state, "generic": g}


def _sale(api, r, rx_line=0, qty=60, item=None):  # noqa: F811
    w = {"clinic": r["clinic"], "store": r["store"], "item": item or r["item"], "batch": r["batch"]}
    if item:
        w["batch"] = _batch(api.conn, r["clinic"], item, "B" + uuid.uuid4().hex[:5], mrp=3000)
        _move(api.conn, r["clinic"], r["store"], w["batch"], item, 100, "opening")
    s = draft(api.conn, w, "opd_rx", r["rx"]["rx"], r["rx"]["patient"],
              snapshot={"name": "Dr. Meena", "registration_number": "TSMC/2012/777"})
    line(api.conn, w, s, qty, rx_item=r["rx"]["rx_items"][rx_line])
    return s


def _complete(api, r, sale, opt_in=True, amount=12000):  # noqa: F811
    api.as_(r["mgr"])
    return api.call("POST", f"/admin/rx/sales/{sale}/complete", r["clinic"], {
        "payments": [{"mode": "upi", "amount_paise": amount}], "idempotency_key": uuid.uuid4().hex,
        "refill_opt_in": opt_in})


def _plans(api, r, status=None):  # noqa: F811
    return _q(api.conn, "SELECT COALESCE(json_agg(row_to_json(p) ORDER BY created_at), '[]') FROM rx_refill_plans p "
                        "WHERE clinic_id = %s AND (%s::text IS NULL OR status = %s)", r["clinic"], status, status)


def test_plan_from_closed_frequency_only(api, ref):  # noqa: F811
    r = ref
    done = _complete(api, r, _sale(api, r))
    assert done.status_code == 200, done.text
    assert done.json()["refill_plans"] == 1
    [p] = _plans(api, r)
    assert (Decimal(str(p["units_per_day"])), p["covered_until"], p["remind_on"], p["status"]) == (
        Decimal("2"), str(TODAY + timedelta(days=30)), str(TODAY + timedelta(days=25)), "scheduled")
    assert p["opt_in_by"] == r["mgr"].user_id
    # the SOS line never makes a plan; without the pharmacist's tick nothing is planned
    assert _complete(api, r, _sale(api, r, rx_line=1, qty=5), amount=1000).json()["refill_plans"] == 0
    third = _complete(api, r, _sale(api, r, qty=2), opt_in=False, amount=400)
    assert third.status_code == 200, third.text
    assert third.json()["refill_plans"] == 0
    assert len(_plans(api, r)) == 1


def test_no_plan_for_h1_x_or_non_chronic_or_refills_off(api, ref):  # noqa: F811
    r = ref
    for sched, chronic in (("H1", True), ("X", True), ("narcotic", True), ("psychotropic", True), ("none", False)):
        kw = {"allow_loose": True} if sched in ("X", "narcotic") else {}
        item = _item(api.conn, r["clinic"], r["generic"], "I" + uuid.uuid4().hex[:6].upper(), "Item " + sched,
                     pack_size=15, schedule=sched, is_chronic=chronic, **kw)
        done = _complete(api, r, _sale(api, r, item=item, qty=2), amount=400)
        assert done.status_code == 200, (sched, done.text)
        assert done.json()["refill_plans"] == 0, sched
    _q(api.conn, "UPDATE clinics SET rx_settings = rx_settings || '{\"refill_enabled\": false}' WHERE id = %s", r["clinic"])
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    assert _complete(api, r, _sale(api, r)).json()["refill_plans"] == 0
    assert _plans(api, r) == []


def test_newer_sale_supersedes_open_plan(api, ref):  # noqa: F811
    r = ref
    _complete(api, r, _sale(api, r))
    _complete(api, r, _sale(api, r))
    assert [p["status"] for p in _plans(api, r)] == ["cancelled", "scheduled"]


def _due(api, r, phone_opted_in=True):  # noqa: F811
    _complete(api, r, _sale(api, r))
    _q(api.conn, "UPDATE rx_refill_plans SET remind_on = CURRENT_DATE WHERE clinic_id = %s AND status = 'scheduled'",
       r["clinic"])
    _q(api.conn, "UPDATE patients SET opted_in = %s WHERE id = %s", phone_opted_in, r["rx"]["patient"])


def _mine(r):
    return [s for s in r["sent"] if s["clinic"] == r["clinic"]]


@pytest.mark.asyncio
async def test_job_respects_consent_and_optout(api, ref):  # noqa: F811
    r = ref
    _due(api, r, phone_opted_in=False)                              # the patient sent STOP
    await rx_refills.run_reminders()
    assert _mine(r) == [] and _plans(api, r)[0]["status"] == "scheduled"
    _q(api.conn, "UPDATE patients SET opted_in = true WHERE id = %s", r["rx"]["patient"])
    await rx_refills.run_reminders()
    [msg] = _mine(r)
    assert msg["name"] == "rx_refill_due_v1" and msg["phone"] == r["rx"]["phone"]
    plan = _plans(api, r)[0]
    assert (plan["status"], plan["last_message_id"]) == ("sent", "wamid.TEST")
    texts = [p["text"] for c in msg["components"] if c["type"] == "body" for p in c["parameters"]]
    assert "Dolo 650" in texts and not any("BD" in t or "tablet" in t for t in texts)        # no dose text
    payloads = [c["parameters"][0]["payload"] for c in msg["components"] if c["type"] == "button"]
    assert payloads == [f"rx_refill:{plan['id']}", f"rx_refill_off:{plan['id']}"]
    await rx_refills.run_reminders()
    assert len(_mine(r)) == 1                                       # sent plans are not re-sent


@pytest.mark.asyncio
async def test_job_skips_off_clinics_and_clinics_without_their_own_number(api, ref):  # noqa: F811
    r = ref
    _due(api, r)
    _q(api.conn, "UPDATE clinics SET config = config - 'meta_access_token' WHERE id = %s", r["clinic"])
    await rx_refills.run_reminders()
    assert _mine(r) == []                                           # never the platform's number
    _q(api.conn, "UPDATE clinics SET config = config || '{\"meta_access_token\": \"t\"}', rx_state = 'WIND_DOWN', "
                 "features = features || '{\"pharmacy_enabled\": false}' WHERE id = %s", r["clinic"])
    await rx_refills.run_reminders()
    assert _mine(r) == []


@pytest.mark.asyncio
async def test_three_failed_sends_expire_the_plan(api, ref):  # noqa: F811
    r = ref
    _due(api, r)
    r["state"]["ok"] = False
    for n in (1, 2, 3):
        await rx_refills.run_reminders()
        p = _plans(api, r)[0]
        assert p["send_failures"] == n
    assert p["status"] == "expired"


def _manager():
    m = type("M", (), {})()
    m.whatsapp = type("W", (), {})()
    m.whatsapp.send_text = AsyncMock(return_value=True)
    return m


@pytest.mark.asyncio
async def test_refill_reply_creates_request_and_stop_opts_out(api, ref):  # noqa: F811
    r = ref
    _complete(api, r, _sale(api, r))
    plan = _plans(api, r)[0]
    clinic = {"id": r["clinic"], "name": "RX"}
    m = _manager()
    for _ in range(2):                                              # a double tap queues one request
        assert await rx_refills.handle_button(m, clinic, r["rx"]["phone"], f"rx_refill:{plan['id']}") is True
    assert m.whatsapp.send_text.await_args_list[0].args[2] == rx_refills.REPLY_REQUESTED
    assert _q(api.conn, "SELECT count(*) FROM rx_refill_requests WHERE plan_id = %s AND status = 'new'", plan["id"]) == 1
    assert _plans(api, r)[0]["status"] == "requested"
    _complete(api, r, _sale(api, r))                               # a second open plan for the same patient
    other = _plans(api, r, "scheduled")[0]
    assert await rx_refills.handle_button(m, clinic, r["rx"]["phone"], f"rx_refill_off:{other['id']}") is True
    assert m.whatsapp.send_text.await_args.args[2] == rx_refills.REPLY_OFF
    assert _plans(api, r, "opted_out")[0]["id"] == other["id"]


@pytest.mark.asyncio
async def test_mismatched_phone_or_clinic_ignored_and_typed_text_not_claimed(api, ref):  # noqa: F811
    r = ref
    _complete(api, r, _sale(api, r))
    plan = _plans(api, r)[0]
    m = _manager()
    payload = f"rx_refill:{plan['id']}"
    assert await rx_refills.handle_button(m, {"id": r["clinic"]}, "+919999900000", payload) is True     # X17
    assert await rx_refills.handle_button(m, {"id": str(uuid.uuid4())}, r["rx"]["phone"], payload) is True
    assert {c.args[2] for c in m.whatsapp.send_text.await_args_list} == {rx_refills.REPLY_IGNORED}
    assert _plans(api, r)[0]["status"] == "scheduled"
    assert _q(api.conn, "SELECT count(*) FROM rx_refill_requests WHERE plan_id = %s", plan["id"]) == 0
    for typed in ("refill", "STOP", "rx_refill", "rx_refill:", "rx_refill:not-a-uuid", f" {payload}", f"{payload}x",
                  "dentrev:" + plan["id"] + ":3"):
        assert await rx_refills.handle_button(m, {"id": r["clinic"]}, r["rx"]["phone"], typed) is False
    # the hook sits inside the template-button branch only: typed text never reaches it (RG4)
    from app.services import conversation
    src = inspect.getsource(conversation)
    start = src.index('if message_type == "button" and interactive_data:\n            from app.services import dental_plans')
    assert start < src.index("rx_refills.handle_button") < src.index("# A tap on a TEMPLATE quick-reply", start)


def test_verify_pack_complete_and_reject_lifecycle(api, ref):  # noqa: F811
    r = ref
    m = _manager()
    _complete(api, r, _sale(api, r))
    _complete(api, r, _sale(api, r))                               # supersedes; take the open one
    plan = _plans(api, r, "scheduled")[0]
    asyncio.run(
        rx_refills.handle_button(m, {"id": r["clinic"]}, r["rx"]["phone"], f"rx_refill:{plan['id']}"))
    api.as_(r["mgr"])
    q = api.call("GET", "/admin/rx/refills/requests", r["clinic"]).json()["requests"]
    [req] = [x for x in q if x["plan_id"] == plan["id"]]
    assert req["patient"]["phone_masked"].endswith(r["rx"]["phone"][-4:]) and r["rx"]["phone"] not in str(q)
    base = f"/admin/rx/refills/requests/{req['id']}"
    assert _code(api.call("POST", base + "/pack", r["clinic"], {"store_id": r["store"]})) == "rx_refill_request_state"
    v = api.call("POST", base + "/verify", r["clinic"])
    assert v.status_code == 200 and v.json()["status"] == "verified"
    assert _code(api.call("POST", base + "/verify", r["clinic"])) == "rx_refill_request_state"
    p = api.call("POST", base + "/pack", r["clinic"], {"store_id": r["store"]})
    assert p.status_code == 200, p.text
    sale = p.json()["sale_id"]
    assert _q(api.conn, "SELECT status FROM rx_sales WHERE id = %s", sale) == "draft"
    assert _q(api.conn, "SELECT json_agg(json_build_array(item_id, qty_units)) FROM rx_sale_items WHERE sale_id = %s",
              sale) == [[r["item"], 60]]
    assert _q(api.conn, "SELECT count(*) FROM rx_stock_movements WHERE ref_id IN (SELECT id FROM rx_sale_items "
                        "WHERE sale_id = %s)", sale) == 0           # packing never dispenses
    assert _complete(api, r, sale, opt_in=False).status_code == 200
    assert _q(api.conn, "SELECT status FROM rx_refill_requests WHERE id = %s", req["id"]) == "completed"
    assert _q(api.conn, "SELECT status FROM rx_refill_plans WHERE id = %s", plan["id"]) == "fulfilled"
    # reject: reason required, patient told to see the doctor (fixed text), plan cancelled
    _complete(api, r, _sale(api, r))
    plan2 = _plans(api, r, "scheduled")[0]
    asyncio.run(
        rx_refills.handle_button(m, {"id": r["clinic"]}, r["rx"]["phone"], f"rx_refill:{plan2['id']}"))
    req2 = str(_q(api.conn, "SELECT id FROM rx_refill_requests WHERE plan_id = %s", plan2["id"]))
    assert api.call("POST", f"/admin/rx/refills/requests/{req2}/reject", r["clinic"], {"reason": "x"}).status_code == 422
    rj = api.call("POST", f"/admin/rx/refills/requests/{req2}/reject", r["clinic"], {"reason": "Therapy changed"})
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"
    from app.services.whatsapp import whatsapp_service
    assert whatsapp_service.send_text.await_args.args[2] == rx_refills.REPLY_REJECTED
    assert _q(api.conn, "SELECT status FROM rx_refill_plans WHERE id = %s", plan2["id"]) == "cancelled"
    # plans list + cancel
    _complete(api, r, _sale(api, r))
    open_plan = _plans(api, r, "scheduled")[0]
    listed = api.call("GET", "/admin/rx/refills/plans", r["clinic"], patient_id=r["rx"]["patient"]).json()["plans"]
    assert open_plan["id"] in [x["id"] for x in listed] and listed[0]["item"]["brand_name"] == "Dolo 650"
    assert api.call("POST", f"/admin/rx/refills/plans/{open_plan['id']}/cancel", r["clinic"]).json()["status"] == "cancelled"
    assert _code(api.call("POST", f"/admin/rx/refills/plans/{open_plan['id']}/cancel", r["clinic"])) == "rx_refill_plan_closed"


def test_verify_refuses_an_expired_prescription(api, ref):  # noqa: F811
    r = ref
    _complete(api, r, _sale(api, r))
    plan = _plans(api, r)[0]
    asyncio.run(
        rx_refills.handle_button(_manager(), {"id": r["clinic"]}, r["rx"]["phone"], f"rx_refill:{plan['id']}"))
    req = str(_q(api.conn, "SELECT id FROM rx_refill_requests WHERE plan_id = %s", plan["id"]))
    _q(api.conn, "SET session_replication_role = replica; UPDATE opd_prescriptions SET signed_at = now() - interval '60 days' "
                 "WHERE id = %s; SET session_replication_role = origin", r["rx"]["rx"])
    api.as_(r["mgr"])
    assert _code(api.call("POST", f"/admin/rx/refills/requests/{req}/verify", r["clinic"])) == "rx_refill_rx_expired"


@pytest.mark.asyncio
async def test_dpdp_erasure_removes_refill_state_not_registers(api, ref):  # noqa: F811
    r = ref
    _q(api.conn, "UPDATE rx_items SET schedule = 'H' WHERE id = %s", r["item"])     # so the sale is also registered
    _complete(api, r, _sale(api, r))
    plan = _plans(api, r)[0]
    await rx_refills.handle_button(_manager(), {"id": r["clinic"]}, r["rx"]["phone"], f"rx_refill:{plan['id']}")
    registers = _q(api.conn, "SELECT count(*) FROM rx_register_entries WHERE clinic_id = %s", r["clinic"])
    assert registers >= 1                                           # the sale out
    from app.services.data_retention import DataRetentionService
    out = await DataRetentionService().anonymize_clinical_records(r["clinic"], r["rx"]["phone"])
    assert out["errors"] == []
    assert _q(api.conn, "SELECT count(*) FROM rx_refill_plans WHERE clinic_id = %s", r["clinic"]) == 0
    assert _q(api.conn, "SELECT count(*) FROM rx_refill_requests WHERE clinic_id = %s", r["clinic"]) == 0
    assert _q(api.conn, "SELECT count(*) FROM rx_register_entries WHERE clinic_id = %s", r["clinic"]) == registers
