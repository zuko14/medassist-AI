"""DX-19: existing paths never act on Diagnostic OS rows (AUDIT A4, A10, A20, A22, ADR-DX-21).

On a clinic running OPD and DX together: the G1 worklist shows no DX orders,
G1 collect / complete refuse them (409 dx_use_dx_route), the OPD draft-bill save
cannot rewrite a DX bill, the generic lab-report resend / resolve refuse DX
reports, and a test ordered in DX cannot be deleted (single or bulk) — while
everything OPD keeps working.
"""

import asyncio
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.services import opd_billing
from tests.test_dx_visits_e2e import dxw, visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


@pytest.fixture
def both(dxw):  # noqa: F811
    """The DX lab also runs OPD (polyclinic): G1 and LIS routes are open to it."""
    _q(dxw["c"], "UPDATE clinics SET plan = 'polyclinic', opd_state = 'READY', "
                 "features = features || '{\"opd_enabled\": true}'::jsonb WHERE id = %s", dxw["clinic"])
    v = visit(dxw, ["cbc", "lipid"]).json()
    return {**dxw, "v": v}


def test_g1_worklist_hides_dx_orders_and_refuses_to_work_them(both):
    api, c = both["api"], both["clinic"]
    collector = api.U("col", "staff", c, str(uuid.uuid4()), ["ORDERS_READ", "ORDERS_COLLECT", "ORDERS_WRITE"],
                      staff_role="LAB_OPERATOR")
    api.as_(collector)
    wl = api.call("GET", "/admin/diagnostic/worklist", c)
    assert wl.status_code == 200
    dx_ids = {o["id"] for o in both["v"]["orders"]}
    # the visit's two DX orders are open ('ordered'); the G1 desk lists neither
    assert wl.json()["total"] == 0 and not dx_ids & {r["id"] for r in wl.json()["orders"]}
    order = both["v"]["orders"][0]["id"]
    r = api.call("POST", f"/admin/diagnostic/orders/{order}/collect", c, {"specimen_barcode": "ABC-123"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_use_dx_route"
    r = api.call("POST", f"/admin/diagnostic/orders/{order}/complete", c, {"result_summary": "normal"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_use_dx_route"
    assert _q(both["c"], "SELECT status FROM opd_orders WHERE id = %s", order) == "ordered"


def test_opd_draft_save_cannot_rewrite_a_dx_bill(both):
    api, c = both["api"].as_(both["u"]["desk"]), both["clinic"]
    bill = api.call("POST", f"/admin/dx/visits/{both['v']['visit']['id']}/bill", c, {"issue": False}).json()
    inv = _q(both["c"], "SELECT to_jsonb(i) FROM opd_invoices i WHERE id = %s", bill["invoice_id"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(opd_billing.save_invoice_draft(
            c, bill["invoice_id"], datetime.fromisoformat(inv["updated_at"]),
            [{"item_type": "other", "description": "Repriced", "quantity": 1, "unit_price_paise": 100}]))
    assert e.value.status_code == 409 and "Diagnostic OS" in str(e.value.detail)
    assert _q(both["c"], "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", bill["invoice_id"]) == 90000


def test_generic_report_resend_and_resolve_refuse_dx_reports(both):
    api, c = both["api"].as_(both["u"]["admin"]), both["clinic"]
    rid = str(_q(both["c"], "INSERT INTO lab_reports (clinic_id, patient_phone, report_name, source, status) VALUES "
                            "(%s, '+919800000009', 'DX visit report', 'dx', 'needs_review') RETURNING id", c))
    r = api.call("POST", f"/admin/lab-reports/{rid}/resend", c)
    assert r.status_code == 409 and "Reports & Dispatch" in r.text
    r = api.call("POST", f"/admin/reports/{rid}/resolve-match", c, {"patient_phone": "+919812345678",
                                                                   "send_now": True})
    assert r.status_code == 409 and "Reports & Dispatch" in r.text
    assert _q(both["c"], "SELECT patient_phone FROM lab_reports WHERE id = %s", rid) == "+919800000009"


def test_a_test_ordered_in_dx_cannot_be_deleted(both):
    api, c = both["api"].as_(both["u"]["admin"]), both["clinic"]
    r = api.call("DELETE", f"/admin/lab-tests/{both['T']['cbc']}", c)
    assert r.status_code == 409 and "deactivate" in r.text
    r = api.call("POST", "/admin/lab-tests/bulk-delete", c, {"ids": [both["T"]["lipid"]]})
    assert r.status_code == 409 and "deactivate" in r.text
    unused = str(_q(both["c"], "INSERT INTO lab_tests (clinic_id, name, price_paise) VALUES (%s, 'Unused', 100) "
                               "RETURNING id", c))
    assert api.call("DELETE", f"/admin/lab-tests/{unused}", c).status_code == 200
    assert _q(both["c"], "SELECT count(*) FROM lab_tests WHERE id IN (%s, %s)", both["T"]["cbc"], both["T"]["lipid"]) == 2
