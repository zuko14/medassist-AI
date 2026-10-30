"""G4 GST billing end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

GSTIN + taxed catalog item -> tax invoice -> paid -> credit note refunds the excess
into the cashier's shift (idempotent) -> GSTR-1 month summary; an exempt bill stays
a bill of supply; permissions; inpatient room rent above Rs 5,000 billed at 5%.
"""

import uuid
from datetime import date

import pytest

from tests.test_ipd_api_e2e import _admit, _clinic, _patient, _q, _setup_hospital, api  # noqa: F401  (fixture)
from tests.test_ipd_finance_api_e2e import _final_bill, _post_all, _to_discharge

GSTIN = "27AAPFU0939F1ZV"
CATALOG = [{"code": "HAIR", "name": "Hair transplant", "item_type": "procedure", "price_paise": 100000,
            "hsn_sac": "999722", "tax_rate_bps": 1800},
           {"code": "DRESS", "name": "Dressing", "item_type": "nursing", "price_paise": 20000}]


def _key():
    return uuid.uuid4().hex


@pytest.fixture
def hosp(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    return {"clinic": clinic, "doctor": doctor, "u": u, "beds": beds}


def _settings(api, h, **patch):  # noqa: F811
    api.as_(h["u"]["admin"])
    return api.call("PUT", "/admin/opd/setup/settings", h["clinic"], patch)


def _bill(api, h, code, price, issue=True):  # noqa: F811
    api.as_(h["u"]["billing"])
    inv = api.call("POST", "/admin/opd/invoices", h["clinic"], {
        "patient_id": _patient(api.conn, h["clinic"]),
        "items": [{"item_type": "procedure", "catalog_code": code, "description": code, "unit_price_paise": price}]})
    assert inv.status_code == 200, inv.text
    if not issue:
        return inv.json()
    issued = api.call("POST", f"/admin/opd/invoices/{inv.json()['id']}/issue", h["clinic"])
    assert issued.status_code == 200, issued.text
    return issued.json()


def test_tax_invoice_credit_note_refund_and_gstr1(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    assert _settings(api, h, gstin="27AAPFU0939F1ZW").status_code == 422          # check digit wrong
    assert _settings(api, h, service_catalog=[{**CATALOG[0], "tax_rate_bps": 18}]).status_code == 422
    r = _settings(api, h, gstin=GSTIN.lower(), service_catalog=CATALOG)
    assert r.status_code == 200 and r.json()["settings"]["gstin"] == GSTIN

    inv = _bill(api, h, "HAIR", 100000)
    assert inv["is_tax_invoice"] is True and inv["clinic_gstin"] == GSTIN
    assert inv["total_paise"] == 118000 and inv["total_tax_paise"] == 18000
    line = inv["items"][0]
    assert (line["hsn_sac"], line["cgst_paise"], line["sgst_paise"]) == ("999722", 9000, 9000)
    assert api.call("GET", f"/admin/opd/invoices/{inv['id']}/pdf", c).status_code == 200

    exempt = _bill(api, h, "DRESS", 20000)
    assert exempt["is_tax_invoice"] is False and exempt["total_tax_paise"] == 0 and exempt["total_paise"] == 20000

    paid = api.call("POST", f"/admin/opd/invoices/{inv['id']}/receipts", c,
                    {"mode": "cash", "amount_paise": 118000, "idempotency_key": _key()})
    assert paid.status_code == 201, paid.text

    # a desk login cannot credit; more than the bill is refused; a refund must be chosen
    api.as_(h["u"]["desk"])
    body = {"amount_paise": 59000, "reason": "service_deficiency", "idempotency_key": _key()}
    assert api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c, body).status_code == 403
    api.as_(h["u"]["billing"])
    assert api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c,
                    {**body, "amount_paise": 118001}).status_code == 422
    need = api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c, body)
    assert need.status_code == 422 and "refund" in need.json()["detail"]
    assert api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c,
                    {**body, "reason": "other"}).status_code == 422                # 'other' needs notes

    body["refund_mode"] = "cash"
    cn = api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c, body)
    assert cn.status_code == 201, cn.text
    cn = cn.json()
    assert cn["credit_note_number"].startswith(f"CRN-{date.today().year}-")
    assert (cn["amount_paise"], cn["tax_paise"], cn["refund_paise"]) == (50000, 9000, 59000)
    again = api.call("POST", f"/admin/opd/invoices/{inv['id']}/credit-notes", c, body).json()
    assert again["id"] == cn["id"] and again["replayed"] is True                 # retry: one note, one refund

    after = api.call("GET", f"/admin/opd/invoices/{inv['id']}", c).json()
    assert (after["status"], after["paid_paise"], after["credited_paise"], after["balance_due_paise"]) == \
        ("paid", 59000, 59000, 0)
    assert [x["credit_note_number"] for x in after["credit_notes"]] == [cn["credit_note_number"]]
    assert [r["kind"] for r in after["receipts"]] == ["payment", "refund"]
    listed = api.call("GET", f"/admin/opd/invoices/{inv['id']}/credit-notes", c).json()["items"]
    assert len(listed) == 1
    assert api.call("POST", f"/admin/opd/invoices/{inv['id']}/void", c,
                    {"reason": "Wrong patient billed"}).status_code >= 400
    shift = api.call("GET", "/admin/opd/shifts/current", c).json()
    closed = api.call("POST", f"/admin/opd/shifts/{shift['id']}/close", c, {"declared_cash_paise": 59000})
    assert closed.status_code == 200 and closed.json()["variance_paise"] == 0

    # GSTR-1: admin only
    month = {"month": date.today().month, "year": date.today().year}
    assert api.call("GET", "/admin/billing/gstr1", c, **month).status_code == 403
    api.as_(h["u"]["admin"])
    g = api.call("GET", "/admin/billing/gstr1", c, **month)
    assert g.status_code == 200, g.text
    g = g.json()
    assert g["clinic_gstin"] == GSTIN and g["b2b"] == []
    assert g["b2cs"] == [{"rate_bps": 1800, "taxable_paise": 100000, "cgst_paise": 9000, "sgst_paise": 9000}]
    assert g["nil_exempt_paise"] == 20000
    assert [x["credit_note_number"] for x in g["credit_notes"]] == [cn["credit_note_number"]]
    assert g["credit_notes"][0]["invoice_number"] == inv["invoice_number"]
    assert g["doc_summary"]["invoices"]["total"] == 2 and g["doc_summary"]["credit_notes"]["total"] == 1


def test_buyer_gstin_puts_the_bill_in_b2b(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    assert _settings(api, h, gstin=GSTIN, service_catalog=CATALOG).status_code == 200
    draft = _bill(api, h, "HAIR", 100000, issue=False)
    api.as_(h["u"]["billing"])
    saved = api.call("PUT", f"/admin/opd/invoices/{draft['id']}", c, {
        "expected_updated_at": draft["updated_at"], "patient_gstin": "29AAGCB7383J1Z4",
        "items": [{"item_type": "procedure", "catalog_code": "HAIR", "description": "HAIR", "unit_price_paise": 100000}]})
    assert saved.status_code == 200 and saved.json()["patient_gstin"] == "29AAGCB7383J1Z4"
    assert api.call("POST", f"/admin/opd/invoices/{draft['id']}/issue", c).status_code == 200
    api.as_(h["u"]["admin"])
    g = api.call("GET", "/admin/billing/gstr1", c, month=date.today().month, year=date.today().year).json()
    assert [b["buyer_gstin"] for b in g["b2b"]] == ["29AAGCB7383J1Z4"] and g["b2cs"] == []


def test_clinic_without_gstin_charges_no_tax_even_on_taxed_items(api, hosp):  # noqa: F811
    h = hosp
    assert _settings(api, h, service_catalog=CATALOG).status_code == 200
    inv = _bill(api, h, "HAIR", 100000)
    assert inv["total_paise"] == 100000 and inv["total_tax_paise"] == 0 and inv["clinic_gstin"] is None


def test_room_rent_above_5000_is_billed_at_5_percent(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    assert _settings(api, h, gstin=GSTIN).status_code == 200
    r = api.call("PUT", "/admin/ipd/settings", c, {"bed_tariffs": {"general": 600000, "icu": 900000}})
    assert r.status_code == 200, r.text
    r = _admit(api, c, h["u"], h["doctor"], h["beds"]["101"])
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    api.as_(h["u"]["billing"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges/accrue-bed", c).json()["added"] >= 1
    bed = _q(api.conn, "SELECT tax_rate_bps FROM ipd_charges WHERE admission_id = %s AND source = 'bed_day' LIMIT 1", aid)
    assert bed == 500
    _post_all(api, h, aid)
    run = api.call("GET", f"/admin/ipd/admissions/{aid}/running-bill", c).json()
    days = _q(api.conn, "SELECT count(*) FROM ipd_charges WHERE admission_id = %s AND source = 'bed_day'", aid)
    assert run["tax_paise"] == 30000 * days and run["payable_paise"] == 630000 * days
    _to_discharge(api, h, aid)
    inv = _final_bill(api, h, aid)
    issued = api.call("POST", f"/admin/opd/invoices/{inv}/issue", c).json()
    assert issued["is_tax_invoice"] is True and issued["total_tax_paise"] == 30000 * days
    assert issued["items"][0]["hsn_sac"] == "999311"

    # an ICU bed is exempt at any rent
    r = _admit(api, c, h["u"], h["doctor"], h["beds"]["ICU1"])
    api.as_(h["u"]["billing"])
    icu = r.json()["id"]
    api.call("POST", f"/admin/ipd/admissions/{icu}/charges/accrue-bed", c)
    assert _q(api.conn, "SELECT max(tax_rate_bps) FROM ipd_charges WHERE admission_id = %s", icu) == 0
