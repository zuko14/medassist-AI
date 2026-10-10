"""OPD data export (Data & Support -> Export data): datasets, scoping, money."""

import asyncio
import csv
import io
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials
from app.services import client_data as cd
from app.services import opd_export as ox

CLINIC = "11111111-1111-1111-1111-111111111111"
client = TestClient(app)
ADMIN = AdminUser(username="adm", role="clinic_admin", clinic_id=CLINIC, user_id="u1")
STAFF = AdminUser(username="desk", role="staff", clinic_id=CLINIC, user_id="u2")


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


class _Q:
    """Records every builder call; every method returns the builder."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __getattr__(self, name):
        if name == "not_":
            self.calls.append(("not_", ()))
            return self

        def f(*a, **k):
            self.calls.append((name, a))
            return self
        return f


def _fake(tables):
    """tables: name -> rows. Records every query."""
    log = []
    sup = MagicMock()

    def table(name):
        q = _Q(name)
        log.append(q)
        return q
    sup.table.side_effect = table

    async def sb(q):
        return MagicMock(data=tables.get(q.table, []))
    return sup, sb, log


def _run(monkeypatch, dataset, tables, frm=date(2026, 10, 1), to=date(2026, 10, 10)):
    sup, sb, log = _fake(tables)
    monkeypatch.setattr(ox, "supabase", sup)
    monkeypatch.setattr(ox, "sb", sb)
    rows = asyncio.run(ox.fetch_opd_export_rows(CLINIC, dataset, frm, to))
    cols = ox.OPD_EXPORT_DATASETS[dataset][2]
    text = cd.csv_from_columns(cols, rows).lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text))), log


# ─── endpoint ────────────────────────────────────────────────────────────────


def test_opd_dataset_refused_for_non_opd_clinic(as_user):
    as_user(ADMIN)
    with patch("app.routers.admin.get_clinic_by_id", AsyncMock(return_value={"id": CLINIC, "features": {}})), \
         patch.object(ox, "fetch_opd_export_rows", AsyncMock()) as fetch:
        res = client.get("/admin/data/export?dataset=opd_invoices&date_from=2026-10-01&date_to=2026-10-10")
    assert res.status_code == 403
    fetch.assert_not_awaited()


def test_opd_export_range_rules_csv_and_staff_refused(as_user):
    opd_clinic = {"id": CLINIC, "features": {"opd_enabled": True}, "plan": "enterprise", "account_type": "tenant"}
    as_user(ADMIN)
    with patch("app.routers.admin.get_clinic_by_id", AsyncMock(return_value=opd_clinic)), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch.object(ox, "fetch_opd_export_rows", AsyncMock(return_value=[])) as fetch:
        assert client.get("/admin/data/export?dataset=opd_invoices").status_code == 400
        bad = client.get("/admin/data/export?dataset=opd_receipts&date_from=2026-10-10&date_to=2026-10-01")
        assert bad.status_code == 400
        fetch.assert_not_awaited()
        ok = client.get("/admin/data/export?dataset=opd_patients")
        assert ok.status_code == 200
        assert fetch.await_args.args[:2] == (CLINIC, "opd_patients")
        assert ok.content.decode("utf-8-sig").splitlines()[0].startswith("MRN,Name,Record Type")
        assert 'filename="opd_patients.csv"' in ok.headers["content-disposition"]
    as_user(STAFF)
    assert client.get("/admin/data/export?dataset=opd_patients").status_code == 403


# ─── datasets ────────────────────────────────────────────────────────────────


def test_visits_join_mrn_invoice_branch_and_skip_erased(monkeypatch):
    appts = [
        {"id": "a1", "appointment_date": "2026-10-05", "token_number": 3, "patient_id": "p1",
         "patient_name": "Ravi", "patient_phone": "+919", "doctor_name": "Dr X", "branch_id": "b1",
         "visit_type": "follow_up", "is_walk_in": True, "booking_channel": "front_desk",
         "queue_status": "completed", "status": "completed", "checked_in_at": "2026-10-05T04:00:00+00:00"},
        {"id": "a2", "appointment_date": "2026-10-05", "token_number": 4, "patient_id": "p2",
         "family_member_id": "f1", "patient_name": "Kid", "patient_phone": "+918"},
        {"id": "a3", "appointment_date": "2026-10-05", "token_number": 5, "patient_name": "[REDACTED]"},
    ]
    rows, log = _run(monkeypatch, "opd_visits", {
        "appointments": appts,
        "patients": [{"id": "p1", "mrn": "MRN-1"}, {"id": "p2", "mrn": "MRN-2"}],
        "family_members": [{"id": "f1", "mrn": "MRN-3"}],
        "opd_invoices": [
            {"appointment_id": "a1", "invoice_number": "INV-9", "status": "void", "total_paise": 1, "paid_paise": 0},
            {"appointment_id": "a1", "invoice_number": "INV-10", "status": "paid",
             "total_paise": 50000, "paid_paise": 50000},
        ],
        "branches": [{"id": "b1", "name": "Kukatpally"}],
    })
    # family member's own MRN; erased shell dropped
    assert [r["MRN"] for r in rows] == ["MRN-1", "MRN-3"]
    r = rows[0]
    assert (r["Branch"], r["Walk-in"], r["Visit Type"], r["Checked In (IST)"]) == \
        ("Kukatpally", "Yes", "follow up", "2026-10-05 09:30")
    assert (r["Invoice No"], r["Billed (Rs)"], r["Paid (Rs)"]) == ("INV-10", "500.00", "500.00")
    q = log[0]
    assert q.table == "appointments"
    assert ("gte", ("appointment_date", "2026-10-01")) in q.calls
    assert ("lt", ("appointment_date", "2026-10-11")) in q.calls
    assert ("is_", ("token_number", "null")) in q.calls
    assert all(("eq", ("clinic_id", CLINIC)) in x.calls for x in log)  # every query scoped


def test_invoices_money_balance_and_void(monkeypatch):
    invs = [
        {"id": "i1", "invoice_number": "INV-1", "status": "partially_paid", "issued_at": "2026-10-02T06:00:00+00:00",
         "subtotal_paise": 80000, "discount_paise": 5000, "total_paise": 75000, "paid_paise": 25000,
         "patient_snapshot": {"name": "A", "mrn": "M1", "phone": "+91"}, "branch_id": "b1"},
        {"id": "i2", "invoice_number": "INV-2", "status": "void", "issued_at": "2026-10-02T07:00:00+00:00",
         "subtotal_paise": 30000, "discount_paise": 0, "total_paise": 30000, "paid_paise": 0,
         "voided_at": "2026-10-02T08:00:00+00:00", "void_reason": "wrong patient"},
    ]
    rows, log = _run(monkeypatch, "opd_invoices", {"opd_invoices": invs, "branches": [{"id": "b1", "name": "Main"}]})
    assert (rows[0]["Total (Rs)"], rows[0]["Paid (Rs)"], rows[0]["Balance Due (Rs)"]) == ("750.00", "250.00", "500.00")
    assert (rows[0]["MRN"], rows[0]["Branch"]) == ("M1", "Main")
    assert (rows[1]["Balance Due (Rs)"], rows[1]["Void Reason"]) == ("0.00", "wrong patient")
    q = log[0]
    assert ("gte", ("issued_at", "2026-09-30T18:30:00Z")) in q.calls
    assert ("lt", ("issued_at", "2026-10-10T18:30:00Z")) in q.calls


def test_items_ordered_with_doctor_names(monkeypatch):
    invs = [{"id": "i1", "invoice_number": "INV-1", "issued_at": "2026-10-02T06:00:00+00:00", "status": "paid"},
            {"id": "i2", "invoice_number": "INV-2", "issued_at": "2026-10-03T06:00:00+00:00", "status": "issued"}]
    items = [
        {"invoice_id": "i2", "line_no": 1, "item_type": "consultation", "doctor_id": "d1", "description": "Consult",
         "quantity": 1, "unit_price_paise": 50000, "discount_paise": 0, "line_total_paise": 50000},
        {"invoice_id": "i1", "line_no": 2, "item_type": "diagnostic", "description": "CBC",
         "quantity": 2, "unit_price_paise": 20000, "discount_paise": 1000, "line_total_paise": 39000},
        {"invoice_id": "i1", "line_no": 1, "item_type": "consultation", "doctor_id": "d1", "description": "Consult",
         "quantity": 1, "unit_price_paise": 40000, "discount_paise": 0, "line_total_paise": 40000},
    ]
    rows, _ = _run(monkeypatch, "opd_invoice_items", {
        "opd_invoices": invs, "opd_invoice_items": items, "doctors": [{"id": "d1", "name": "Dr X"}]})
    assert [(r["Invoice No"], r["Line"]) for r in rows] == [("INV-1", "1"), ("INV-1", "2"), ("INV-2", "1")]
    assert (rows[0]["Doctor"], rows[1]["Doctor"], rows[1]["Line Total (Rs)"]) == ("Dr X", "", "390.00")


def test_receipts_refunds_negative_and_online(monkeypatch):
    rcts = [
        {"id": "r1", "invoice_id": "i1", "kind": "payment", "mode": "upi", "amount_paise": 50000, "shift_id": "s1",
         "received_by_name": "Cashier A", "created_at": "2026-10-02T06:00:00+00:00", "reference": "UTR1"},
        {"id": "r2", "invoice_id": "i1", "kind": "refund", "mode": "cash", "amount_paise": 10000, "shift_id": "s1",
         "received_by_name": "Cashier A", "reason": "overcharged", "created_at": "2026-10-02T07:00:00+00:00"},
        {"id": "r3", "invoice_id": "i1", "kind": "payment", "mode": "payment_link", "amount_paise": 5000,
         "gateway": "razorpay", "gateway_payment_id": "pay_1", "created_at": "2026-10-02T08:00:00+00:00"},
    ]
    rows, _ = _run(monkeypatch, "opd_receipts", {
        "opd_receipts": rcts,
        "opd_invoices": [{"id": "i1", "invoice_number": "INV-1", "patient_snapshot": {"name": "A"}, "branch_id": None}],
    })
    assert [r["Net Effect (Rs)"] for r in rows] == ["500.00", "-100.00", "50.00"]
    assert [r["Type"] for r in rows] == ["Payment", "Refund", "Payment"]
    assert (rows[2]["Mode"], rows[2]["Received By"], rows[0]["Invoice No"]) == ("Payment link", "Online", "INV-1")


def test_registry_includes_family_members_and_skips_erased(monkeypatch):
    rows, log = _run(monkeypatch, "opd_patients", {
        "patients": [{"id": "p1", "mrn": "MRN-2", "name": "Ravi", "phone": "+91",
                      "allergies": ["Penicillin", "Sulfa"], "allergies_status": "present",
                      "created_at": "2026-10-01T00:00:00+00:00"},
                     {"id": "p2", "mrn": "MRN-9", "name": "[REDACTED]", "phone": "[REDACTED]"}],
        "family_members": [{"id": "f1", "mrn": "MRN-1", "full_name": "Kid", "primary_phone": "+91",
                            "relationship": "son", "allergies": []}],
    }, frm=None, to=None)
    assert [(r["MRN"], r["Record Type"]) for r in rows] == [("MRN-1", "Family member"), ("MRN-2", "Account holder")]
    assert rows[1]["Allergies"] == "Penicillin; Sulfa"
    assert rows[0]["Relationship"] == "son"
    assert all(("is_", ("mrn", "null")) in q.calls for q in log)


def test_shifts_variance_keeps_sign(monkeypatch):
    rows, _ = _run(monkeypatch, "opd_shifts", {"opd_cashier_shifts": [
        {"cashier_name": "A", "status": "closed", "opened_at": "2026-10-02T03:00:00+00:00",
         "opening_float_paise": 100000, "expected_cash_paise": 250000, "declared_cash_paise": 249500,
         "variance_paise": -500, "branch_id": None}]})
    assert (rows[0]["Variance (Rs)"], rows[0]["Expected Cash (Rs)"]) == ("-5.00", "2500.00")


def test_cap_and_scope(monkeypatch):
    monkeypatch.setattr(ox, "EXPORT_PAGE", 2)
    monkeypatch.setattr(ox, "EXPORT_MAX_ROWS", 3)
    sup, sb, _ = _fake({"opd_cashier_shifts": [{"id": 1}, {"id": 2}]})
    monkeypatch.setattr(ox, "supabase", sup)
    monkeypatch.setattr(ox, "sb", sb)
    with pytest.raises(OverflowError):
        asyncio.run(ox.fetch_opd_export_rows(CLINIC, "opd_shifts", date(2026, 1, 1), date(2026, 1, 2)))
    with pytest.raises(ValueError):
        asyncio.run(ox.fetch_opd_export_rows("default", "opd_shifts", date(2026, 1, 1), date(2026, 1, 2)))
