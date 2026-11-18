"""Statutory registers, batch recall trace and narcotic empties through the real /admin/rx routes on real Postgres
(W9, A6, A11; RX-49, RX-50, RX-74, RX-79).

A Schedule H1 counter sale and a narcotic IPD dispense land in their registers on their own (the ledger trigger);
the pharmacist lists them, exports the H1 / narcotic formats as CSV and PDF (audited, never a phone number) and
sees the chain status turn red at the tampered seq. Managers trace a batch to every patient and ward; the narcotic
custodian receives empties only from someone else.
"""

import csv
import io
import uuid
from datetime import date

import pytest

from tests.test_ipd_api_e2e import _code, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m5 import complete, draft, line
from tests.test_rx_ipd import _dispense, _raise, ward  # noqa: F401  (fixture)

PHONE = "+919845012345"
EXT = {"external": True, "name": "Dr. Outside", "registration_number": "KMC/99/1", "council": "KMC",
       "rx_date": str(date.today()), "patient_name": "Lakshmi Devi"}
H1_HEADERS = ["Seq", "Date", "Bill / Doc No.", "Entry", "Patient Name & Address", "Prescriber Name & Reg. No.",
              "Drug Name", "Batch", "Qty", "Balance", "Supplier", "Pharmacist"]


def _staff(conn, clinic, role):
    """A real login row (returned_by / received_by are clinic_admins ids)."""
    return str(_q(conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active) "
                        "VALUES (%s, %s, 'x', 'staff', %s, true) RETURNING id", clinic, role.lower() + uuid.uuid4().hex[:6], role))


@pytest.fixture
def reg(api, ward, monkeypatch):  # noqa: F811
    w, c, conn = ward, ward["clinic"], api.conn
    audits = []

    async def _log(**kw):
        audits.append(kw)
    monkeypatch.setattr("app.routers.rx.log_admin_action", _log)
    # Schedule H1: an outside prescription sold at the counter to a registered patient (with a phone + address)
    calpol = str(_q(conn, "SELECT id FROM rx_items WHERE clinic_id = %s AND item_code = 'CALPOL650'", c))
    _q(conn, "UPDATE rx_items SET schedule = 'H1' WHERE id = %s", calpol)
    batch = _batch(conn, c, calpol, "CH1", mrp=12000)                     # pack of 15 → ₹8 a tablet
    _move(conn, c, w["store"], batch, calpol, 30, "opening")
    pat = str(_q(conn, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on, address_line, mrn) "
                       "VALUES (%s, 'Lakshmi Devi', %s, 'female', 54, CURRENT_DATE, '12 MG Road, Hyderabad', 'MRN-77') "
                       "RETURNING id", c, PHONE))
    wd = {"clinic": c, "store": w["store"], "item": calpol, "batch": batch}
    sale = draft(conn, wd, "otc", patient=pat, snapshot=EXT)
    line(conn, wd, sale, 3)
    complete(conn, wd, sale, [{"mode": "upi", "amount_paise": 2400}])
    mgr_id = _staff(conn, c, "PHARMACY_MANAGER")
    users = {"pharmacist": w["pharmacist"],
             "manager": api.U("mgr", "staff", c, mgr_id, [], staff_role="PHARMACY_MANAGER"),
             "keeper": api.U("keeper", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER")}
    return {**w, "calpol": calpol, "batch": batch, "patient": pat, "audits": audits, "users": users, "mgr_id": mgr_id}


def _narcotic_dispense(api, r):  # noqa: F811
    """DOLO650 becomes a narcotic; the ward gets 3 tablets (FEFO from DNEAR)."""
    _q(api.conn, "UPDATE rx_items SET schedule = 'narcotic' WHERE id = %s", r["item"])
    indent = _raise(api, r).json()
    assert _dispense(api, r, indent).status_code == 200
    return str(_q(api.conn, "SELECT id FROM rx_dispense_lines WHERE clinic_id = %s", r["clinic"]))


def _csv(res):
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(res.content.decode("utf-8").lstrip("﻿"))))


def test_h1_register_list_and_exports_audited_without_phone(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    api.as_(r["users"]["pharmacist"])
    got = api.call("GET", "/admin/rx/registers/H1", c)
    assert got.status_code == 200, got.text
    opening, sale = got.json()["entries"]
    assert (opening["direction"], opening["receipt"], sale["direction"], sale["receipt"]) == ("in", True, "out", False)
    assert sale["item"]["brand_name"] == "Calpol 650" and sale["batch"]["batch_no"] == "CH1"
    assert sale["patient_snapshot"]["name"] == "Lakshmi Devi" and "row_hash" not in sale
    assert api.call("GET", "/admin/rx/registers/H1", c, limit=1).json()["has_more"] is True

    rows = _csv(api.call("GET", "/admin/rx/registers/H1/export.csv", c, **{"from": str(date.today()), "to": str(date.today())}))
    assert rows[0] == H1_HEADERS and len(rows) == 3
    sold = dict(zip(H1_HEADERS, rows[2]))
    assert sold["Entry"] == "Issue" and sold["Qty"] == "3" and sold["Balance"] == "27"
    assert sold["Patient Name & Address"] == "Lakshmi Devi, 54/F, MRN MRN-77, 12 MG Road, Hyderabad"
    assert sold["Prescriber Name & Reg. No."] == "Dr. Outside — Reg. KMC/99/1 (KMC)"
    assert sold["Bill / Doc No."].startswith("PHS-") and sold["Drug Name"] == "Calpol 650"
    pdf = api.call("GET", "/admin/rx/registers/H1/export.pdf", c)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    for blob in (pdf.content, api.call("GET", "/admin/rx/registers/H1/export.csv", c).content,
                 api.call("GET", "/admin/rx/registers/H1", c).content):
        assert b"45012345" not in blob and b"XXXXXX" not in blob                 # no phone, not even masked
    exports = [a for a in r["audits"] if a["action"] == "rx_register_export"]
    assert [(a["resource_id"], a["details"]["format"], a["details"]["rows"]) for a in exports] == \
        [("H1", "csv", 2), ("H1", "pdf", 2), ("H1", "csv", 2)]
    assert exports[0]["details"]["from"] == str(date.today())
    assert api.call("GET", "/admin/rx/registers/H1", c, **{"from": "2026-02-01", "to": "2026-01-01"}).status_code == 422


def test_narcotic_running_balance_from_an_ipd_dispense(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    _narcotic_dispense(api, r)
    api.as_(r["users"]["pharmacist"])
    [e] = api.call("GET", "/admin/rx/registers/narcotic", c).json()["entries"]
    assert (e["direction"], e["qty_units"], e["balance_after_units"]) == ("out", 3, 137)
    assert e["prescriber_snapshot"]["name"] and e["patient_snapshot"]["admission"].startswith("IPD-")
    rows = _csv(api.call("GET", "/admin/rx/registers/narcotic/export.csv", c, store_id=r["store"]))
    row = dict(zip(rows[0], rows[1]))
    assert (row["Opening Balance"], row["Quantity Received"], row["Quantity Issued"], row["Balance"]) == ("140", "", "3", "137")
    assert row["Doc No."].startswith("IND-") and row["Signature"] == "pharma"
    assert api.call("GET", "/admin/rx/registers/narcotic/export.pdf", c).content[:4] == b"%PDF"


def test_verify_turns_red_at_the_tampered_seq(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    api.as_(r["users"]["pharmacist"])
    ok = api.call("GET", "/admin/rx/registers/H1/verify", c).json()
    assert (ok["ok"], ok["rows"], ok["first_bad_seq"]) == (True, 2, None)
    _q(api.conn, "SET session_replication_role = replica; UPDATE rx_register_entries SET qty_units = 1 "
                 "WHERE clinic_id = %s AND register = 'H1' AND seq = 2; SET session_replication_role = origin", c)
    bad = api.call("GET", "/admin/rx/registers/H1/verify", c).json()
    assert (bad["ok"], bad["rows"], bad["first_bad_seq"]) == (False, 1, 2)
    assert api.call("GET", "/admin/rx/registers/H1/export.pdf", c).status_code == 200    # still exportable, flagged


def test_register_permissions(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    api.as_(r["users"]["keeper"])                                    # RX_READ only: registers hold patient PHI
    assert api.call("GET", "/admin/rx/registers/H1", c).status_code == 403
    assert api.call("GET", "/admin/rx/registers/H1/export.csv", c).status_code == 403
    api.as_(r["u"]["nurse"])
    assert api.call("GET", "/admin/rx/registers/H1/verify", c).status_code == 403
    api.as_(r["users"]["pharmacist"])
    assert api.call("GET", "/admin/rx/registers/Z", c).status_code == 422
    assert api.call("GET", f"/admin/rx/batches/{r['batch']}/trace", c).status_code == 403    # managers only
    assert api.call("GET", "/admin/rx/registers/H1", c, store_id=str(uuid.uuid4())).status_code == 404


def test_batch_recall_trace(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    api.as_(r["users"]["manager"])
    t = api.call("GET", f"/admin/rx/batches/{r['batch']}/trace", c)
    assert t.status_code == 200, t.text
    t = t.json()
    [sale] = t["sales"]
    assert (sale["patient_name"], sale["phone_masked"], sale["qty_units"]) == ("Lakshmi Devi", "XXXXXX2345", 3)
    assert PHONE[3:] not in str(t)
    assert t["on_hand"][0]["qty_units"] == 27 and t["summary"] == {"patients": 1, "sold_units": 3, "ipd_units": 0,
                                                                   "on_hand_units": 27}
    dnear = str(_q(api.conn, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'DNEAR'", c))
    _narcotic_dispense(api, r)
    api.as_(r["users"]["manager"])
    [ipd] = api.call("GET", f"/admin/rx/batches/{dnear}/trace", c).json()["ipd"]
    assert (ipd["bed"], ipd["qty_units"], ipd["mar_doses"]) == ("101", 3, 0) and ipd["ward"]
    assert [a["action"] for a in r["audits"]].count("rx_batch_trace") == 2
    assert api.call("GET", f"/admin/rx/batches/{uuid.uuid4()}/trace", c).status_code == 404


def test_narcotic_empties_two_person(api, reg):  # noqa: F811
    r, c = reg, reg["clinic"]
    dl = _narcotic_dispense(api, r)
    nurse_id = _staff(api.conn, c, "NURSE")
    path = f"/admin/rx/dispenses/{dl}/empties"
    api.as_(r["users"]["pharmacist"])                                # no RX_CONTROLLED
    assert api.call("POST", path, c, {"units": 1, "returned_by": nurse_id}).status_code == 403
    api.as_(r["users"]["manager"])
    same = api.call("POST", path, c, {"units": 1, "returned_by": r["mgr_id"]})
    assert same.status_code == 409 and _code(same) == "rx_empties_two_person"
    got = api.call("POST", path, c, {"units": 2, "returned_by": nurse_id})
    assert got.status_code == 201, got.text
    assert (got.json()["empties_returned_units"], got.json()["outstanding_units"]) == (2, 1)
    assert _code(api.call("POST", path, c, {"units": 2, "returned_by": nurse_id})) == "rx_empties_exceed_dispensed"
    assert api.call("POST", path, c, {"units": 1, "returned_by": str(uuid.uuid4())}).status_code == 404
    assert api.call("POST", f"/admin/rx/dispenses/{uuid.uuid4()}/empties", c,
                    {"units": 1, "returned_by": nurse_id}).status_code == 404
    assert [a["details"]["units"] for a in r["audits"] if a["action"] == "rx_empties_received"] == [2]
