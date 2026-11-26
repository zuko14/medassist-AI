"""S3 packages + consumption ledger + instalments (SP-S3-01) and S5 estimates (SP-S5-01) on real Postgres.

A package sale is an ORDINARY OPD invoice: one catalogue line priced server-side, taxed by the catalogue entry (18 %
for a cosmetic course with a GSTIN) — no zero-value lines ever (VERIFICATION D15); a session is consumed only on an
issued bill, numbered 1…N, never beyond N, a reversed number is reused, a completed package re-opens on reversal.
Instalments must add up to the invoice total; paid / due / overdue is derived from the invoice's paid amount.
Estimates: two options as WhatsApp buttons; the patient's tap records the choice and the message id once (a replayed
webhook or someone else's phone changes nothing); "Not now" declines; nothing is charged; accepted → package sale.
"""

import asyncio
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import sp_hooks
from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

GSTIN = "27AAPFU0939F1ZV"
CATALOG = [{"code": "LHR6", "name": "Laser hair reduction course", "item_type": "procedure", "price_paise": 1200000,
            "hsn_sac": "999722", "tax_rate_bps": 1800},
           {"code": "DRESS", "name": "Dressing", "item_type": "nursing", "price_paise": 20000}]


@pytest.fixture
def shop(api):  # noqa: F811
    w = sp_world(api, plan="derma", packs=("derma",))
    api.as_(w.u.admin)
    r = api.call("PUT", "/admin/opd/setup/settings", w.clinic, {"gstin": GSTIN, "service_catalog": CATALOG})
    assert r.status_code == 200, r.text
    pkg = api.call("POST", "/admin/sp/packages", w.clinic, {"code": "LHR-6", "name": "Laser ×6", "specialty": "derma",
                                                            "service_code": "LHR6", "sessions_total": 3, "validity_days": 180})
    assert pkg.status_code == 201, pkg.text
    return SimpleNamespace(w=w, pkg=pkg.json())


def _issue(api, w, inv):
    api.as_(w.u.admin)
    r = api.call("POST", f"/admin/opd/invoices/{inv}/issue", w.clinic)
    assert r.status_code == 200, r.text
    return r.json()


def test_catalogue_needs_an_opd_service(api, shop):  # noqa: F811
    w = shop.w
    assert shop.pkg["price_paise"] == 1200000 and shop.pkg["tax_rate_bps"] == 1800
    bad = api.call("POST", "/admin/sp/packages", w.clinic, {"code": "X1", "name": "Ghost", "specialty": "derma",
                                                            "service_code": "NOPE", "sessions_total": 2})
    assert bad.status_code == 422
    assert api.call("POST", "/admin/sp/packages", w.clinic, {"code": "LHR-6", "name": "Again", "specialty": "derma",
                                                             "service_code": "LHR6", "sessions_total": 2}).status_code == 409
    api.as_(w.u.desk)
    assert api.call("POST", "/admin/sp/packages", w.clinic, {"code": "Y1", "name": "Desk", "specialty": "derma",
                                                             "service_code": "LHR6", "sessions_total": 2}).status_code == 403
    assert len(api.call("GET", "/admin/sp/packages", w.clinic).json()["packages"]) == 1


def test_sale_is_an_ordinary_taxed_invoice_and_sessions_are_a_ledger(api, shop):  # noqa: F811
    w = shop.w
    api.as_(w.u.desk)
    sale = api.call("POST", "/admin/sp/package-sales", w.clinic, {"package_id": shop.pkg["id"], "patient_id": w.patient})
    assert sale.status_code == 201, sale.text
    sale = sale.json()
    lines = _q(w.conn, "SELECT json_agg(json_build_array(catalog_code, quantity, unit_price_paise)) FROM opd_invoice_items "
                       "WHERE invoice_id = %s", sale["invoice_id"])
    assert lines == [["LHR6", 1, 1200000]]                                         # one ordinary line, no zero lines
    sid = sale["id"]
    r = api.call("POST", f"/admin/sp/package-sales/{sid}/consume", w.clinic, {})
    assert r.status_code == 409 and "Issue" in r.text                                # a draft bill is not a sale yet
    inv = _issue(api, w, sale["invoice_id"])
    assert inv["is_tax_invoice"] is True and inv["total_tax_paise"] == 216000         # 18 % from the catalogue entry
    api.as_(w.u.nurse)
    got = [api.call("POST", f"/admin/sp/package-sales/{sid}/consume", w.clinic, {}).json() for _ in range(3)]
    assert [g["session_no"] for g in got] == [1, 2, 3] and got[-1]["sessions_left"] == 0
    assert _q(w.conn, "SELECT status FROM sp_package_sales WHERE id = %s", sid) == "completed"
    assert api.call("POST", f"/admin/sp/package-sales/{sid}/consume", w.clinic, {}).status_code == 409
    assert _q(w.conn, "SELECT count(*) FROM opd_invoice_items WHERE invoice_id = %s", sale["invoice_id"]) == 1
    api.as_(w.u.admin)
    assert api.call("POST", f"/admin/sp/package-consumptions/{got[1]['id']}/reverse", w.clinic,
                    {"reason": "charted twice"}).status_code == 200
    again = api.call("POST", f"/admin/sp/package-sales/{sid}/consume", w.clinic, {}).json()
    assert again["session_no"] == 2                                                   # the reversed number is reused
    st = api.call("GET", f"/admin/sp/patients/{w.patient}/package-sales", w.clinic).json()["sales"][0]
    assert (st["sessions_used"], st["sessions_left"], st["invoice"]["status"]) == (3, 0, "issued")


def test_expired_package_refuses_a_session(api, shop):  # noqa: F811
    w = shop.w
    sale = api.call("POST", "/admin/sp/package-sales", w.clinic, {"package_id": shop.pkg["id"], "patient_id": w.patient}).json()
    _issue(api, w, sale["invoice_id"])
    _q(w.conn, "UPDATE sp_package_sales SET expires_on = CURRENT_DATE - 1 WHERE id = %s", sale["id"])
    r = api.call("POST", f"/admin/sp/package-sales/{sale['id']}/consume", w.clinic, {})
    assert r.status_code == 409 and "expired" in r.text


def test_instalments_add_up_and_follow_receipts(api, shop):  # noqa: F811
    w = shop.w
    sale = api.call("POST", "/admin/sp/package-sales", w.clinic, {"package_id": shop.pkg["id"], "patient_id": w.patient}).json()
    inv = _issue(api, w, sale["invoice_id"])
    total, d0 = inv["total_paise"], date.today()
    path = f"/admin/sp/invoices/{inv['id']}/instalments"
    bad = {"instalments": [{"due_on": str(d0), "amount_paise": total - 1}]}
    assert api.call("PUT", path, w.clinic, bad).status_code == 422
    plan = {"instalments": [{"due_on": str(d0 - timedelta(days=40)), "amount_paise": 400000},
                            {"due_on": str(d0 + timedelta(days=3)), "amount_paise": 400000},
                            {"due_on": str(d0 + timedelta(days=60)), "amount_paise": total - 800000}]}
    assert api.call("PUT", path, w.clinic, plan).status_code == 200
    states = [i["status"] for i in api.call("GET", path, w.clinic).json()["instalments"]]
    assert states == ["overdue", "due", "due"]
    paid = api.call("POST", f"/admin/opd/invoices/{inv['id']}/receipts", w.clinic,
                    {"mode": "cash", "amount_paise": 500000, "idempotency_key": uuid.uuid4().hex})
    assert paid.status_code == 201, paid.text
    rows = api.call("GET", path, w.clinic).json()["instalments"]
    assert [r["status"] for r in rows][:1] == ["paid"] and rows[1]["outstanding_paise"] == 300000
    due = api.call("GET", "/admin/sp/instalments/due", w.clinic, within_days=7).json()["instalments"]
    assert [d["seq"] for d in due] == [2]


def _manager():
    return SimpleNamespace(whatsapp=SimpleNamespace(send_text=AsyncMock(return_value=True)))


def test_estimate_accept_on_whatsapp_moves_no_money(api, shop, monkeypatch):  # noqa: F811
    from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
    from app.services.whatsapp import whatsapp_service
    w = shop.w
    _q(w.conn, "UPDATE patients SET opted_in = true WHERE id = %s", w.patient)
    phone = _q(w.conn, "SELECT phone FROM patients WHERE id = %s", w.patient)
    api.as_(w.u.desk)
    est = api.call("POST", "/admin/sp/estimates", w.clinic, {
        "patient_id": w.patient, "specialty": "derma", "title": "Laser hair reduction", "valid_until": str(date.today() + timedelta(days=30)),
        "options": [{"code": "std", "label": "Standard", "lines": [{"label": "6 sessions", "amount_paise": 1200000}]},
                    {"code": "pro", "label": "Premium", "lines": [{"label": "6 sessions", "amount_paise": 1500000},
                                                                  {"label": "Aftercare kit", "amount_paise": 150000}]}]})
    assert est.status_code == 201, est.text
    est = est.json()
    assert [o["total_paise"] for o in est["options"]] == [1200000, 1650000]
    buttons = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp_service, "send_interactive_buttons", buttons)
    assert api.call("POST", f"/admin/sp/estimates/{est['id']}/send", w.clinic).status_code == 200
    sent = buttons.await_args
    ids = [b["id"] for b in sent.args[3]]
    assert ids == [f"spest:{est['id']}:std", f"spest:{est['id']}:pro", f"spest:{est['id']}:no"]
    assert all(len(b["title"]) <= 20 for b in sent.args[3]) and "nothing is charged" in sent.args[2]
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(w.clinic))
    receipts_before = _q(w.conn, "SELECT count(*) FROM opd_receipts WHERE clinic_id = %s", w.clinic)
    m = _manager()
    assert asyncio.run(sp_hooks.handle_tap(m, clinic, "+919999999999", ids[1], "wamid.X0")) is True   # a stranger
    assert _q(w.conn, "SELECT status FROM sp_estimates WHERE id = %s", est["id"]) == "sent"
    assert asyncio.run(sp_hooks.handle_tap(m, clinic, phone, ids[1], "wamid.X1")) is True
    assert asyncio.run(sp_hooks.handle_tap(m, clinic, phone, ids[2], "wamid.X2")) is True               # replay / late "no"
    row = _q(w.conn, "SELECT row_to_json(e) FROM sp_estimates e WHERE id = %s", est["id"])
    assert (row["status"], row["accepted_option"], row["response_message_id"]) == ("accepted", "pro", "wamid.X1")
    assert "Nothing has been charged" in m.whatsapp.send_text.await_args_list[1].args[2]
    assert _q(w.conn, "SELECT count(*) FROM opd_receipts WHERE clinic_id = %s", w.clinic) == receipts_before
    assert asyncio.run(sp_hooks.handle_tap(m, clinic, phone, "spest:not-a-uuid:std", "w")) is True     # absorbed
    sale = api.call("POST", f"/admin/sp/estimates/{est['id']}/convert", w.clinic, {"package_id": shop.pkg["id"]})
    assert sale.status_code == 201, sale.text
    assert _q(w.conn, "SELECT status FROM sp_estimates WHERE id = %s", est["id"]) == "converted"
    three = api.call("POST", "/admin/sp/estimates", w.clinic, {
        "patient_id": w.patient, "specialty": "derma", "title": "Three ways", "valid_until": str(date.today() + timedelta(days=5)),
        "options": [{"code": c, "label": c, "lines": [{"label": "x", "amount_paise": 1}]} for c in ("a", "b", "c")]}).json()
    assert api.call("POST", f"/admin/sp/estimates/{three['id']}/send", w.clinic).status_code == 422
    assert api.call("POST", f"/admin/sp/estimates/{three['id']}/respond", w.clinic, {"option": None}).json()["status"] == "declined"


def test_tap_reaches_the_hook_only_for_a_specialty_clinic():
    from unittest.mock import patch

    from app.services.conversation import ConversationManager
    session = {"state": "main_menu", "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    for feats, claimed in (({"opd_enabled": True, "specialty_os": True}, True), ({"opd_enabled": True}, False)):
        clinic = {"id": "c1", "name": "H", "config": {}, "plan": "derma", "account_type": "tenant", "features": feats}
        m = ConversationManager()
        m.whatsapp = AsyncMock()
        tap = AsyncMock(return_value=True)
        with patch.object(sp_hooks, "handle_tap", tap), \
                patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)), \
                patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value={"id": "p", "language": "en"})), \
                patch("app.services.conversation.update_conversation", new=AsyncMock()), \
                patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")):
            asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000001", message="Accept", message_type="interactive",
                                                 message_id="wamid.T", interactive_data={"id": f"spest:{uuid.uuid4()}:std"}))
        assert tap.await_count == (1 if claimed else 0)
