"""D-38: OPD invoice create / issue / payment link (the routes Kriya Ketu KK-P4-15 uses).

Each writes one audit row on success, and a branch-pinned staff member cannot act on another branch's
appointment or invoice (403 before anything is written) — the same check GET /invoices/{id} already makes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from tests.test_opd_queue import CLINIC_ID, MOCK_CLINIC

client = TestClient(app)
BILLING = AdminUser("cashier", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_BILLING"])
PINNED_B2 = AdminUser("cashier_b2", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_BILLING"], branch_id="b2")
INV = {"id": "inv1", "branch_id": "b1", "status": "issued", "total_paise": 50000, "paid_paise": 0,
       "patient_id": "p1", "invoice_number": "INV-1"}


def _post(path, user, *, appt_branch="b1"):
    app.dependency_overrides[admin.verify_credentials] = lambda: user
    audit, create, issue = AsyncMock(), AsyncMock(return_value={**INV, "status": "draft"}), AsyncMock(return_value=INV)
    link = AsyncMock(return_value={"url": "https://pay.example/x", "id": "plink_1"})
    try:
        with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
             patch("app.routers.opd.sb", AsyncMock(return_value=MagicMock(data=[{"id": "a1", "branch_id": appt_branch,
                                                                                  "phone": None}]))), \
             patch("app.routers.opd.get_or_create_appointment_invoice", create), \
             patch("app.routers.opd.issue_invoice", issue), \
             patch("app.routers.opd.get_invoice", AsyncMock(return_value=INV)), \
             patch("app.routers.opd.payment_service.create_opd_payment_link", link), \
             patch("app.routers.opd.log_admin_action", audit):
            r = client.post(f"{path}?clinic_id={CLINIC_ID}")
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)
    return r, audit, {"create": create, "issue": issue, "link": link}


CASES = [("/admin/opd/appointments/a1/invoice", "create", "OPD_INVOICE_CREATE"),
         ("/admin/opd/invoices/inv1/issue", "issue", "OPD_INVOICE_ISSUE"),
         ("/admin/opd/invoices/inv1/payment-link", "link", "OPD_PAYMENT_LINK")]


@pytest.mark.parametrize("path, write, action", CASES)
def test_each_billing_write_is_audited(path, write, action):
    r, audit, writes = _post(path, BILLING)
    assert r.status_code == 200, r.text
    assert writes[write].await_count == 1
    assert audit.await_count == 1 and audit.await_args.kwargs["action"] == action


@pytest.mark.parametrize("path, write, action", CASES)
def test_a_pinned_cashier_cannot_bill_another_branch(path, write, action):
    r, audit, writes = _post(path, PINNED_B2)
    assert r.status_code == 403
    writes[write].assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("path, write, action", CASES)
def test_a_pinned_cashier_bills_their_own_branch(path, write, action):
    user = AdminUser("cashier_b1", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_BILLING"], branch_id="b1")
    r, _, writes = _post(path, user)
    assert r.status_code == 200 and writes[write].await_count == 1
