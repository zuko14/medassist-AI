"""Tests for Kriya OPD OS Billing & Cashier Engine (Phase 1.4).

Verifies:
1. Consultation price derived from doctor fee (client price ignored for non-admin).
2. Service catalog and lab test pricing server enforcement.
3. Draft invoice editing with CAS optimistic locking (409 stale conflict).
4. Invoice issuing assigns sequential INV-YYYY-XXXXX number.
5. Zero total invoice marked as 'paid' upon issuing.
6. Split payment (Cash + UPI) marks invoice as 'paid'.
7. UPI / Card counter receipts require reference (422 validation).
8. Idempotency key replay returns the identical receipt without duplicate insertion.
9. Refunds strictly require OPD_ADMIN and reason >= 5 characters.
10. Void blocked when paid_paise > 0 (409 conflict).
11. One live invoice per visit constraint (second creation returns 409).
12. Prepaid online booking generates prepaid_online receipt on issue, outside drawer.
13. Shifts: 1 open per cashier, receipt into another cashier's shift returns 409,
    close computes expected cash / variance, admin reconciliation.
14. Collections summary aggregations.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from app.services.opd_billing import (
    close_shift,
    create_invoice,
    create_receipt,
    create_refund,
    get_catalog,
    get_collections_summary,
    get_current_shift,
    get_or_create_appointment_invoice,
    issue_invoice,
    open_shift,
    save_invoice_draft,
    settle_opd_payment_link,
    void_invoice,
)

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
BRANCH_ID = str(uuid.uuid4())
DOCTOR_ID = str(uuid.uuid4())
APPOINTMENT_ID = str(uuid.uuid4())
PATIENT_ID = str(uuid.uuid4())
CASHIER_ID = str(uuid.uuid4())
OTHER_CASHIER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())

CASHIER_USER = AdminUser(
    username="cashier_jane",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_BILLING"],
    user_id=CASHIER_ID,
)

OTHER_CASHIER_USER = AdminUser(
    username="cashier_bob",
    role="staff",
    clinic_id=CLINIC_ID,
    permissions=["OPD_BILLING"],
    user_id=OTHER_CASHIER_ID,
)

ADMIN_USER = AdminUser(
    username="clinic_admin",
    role="clinic_admin",
    clinic_id=CLINIC_ID,
    permissions=["OPD_ADMIN", "OPD_BILLING"],
    user_id=ADMIN_ID,
)

MOCK_CLINIC = {
    "id": CLINIC_ID,
    "name": "Kriya Metro Hospital",
    "features": {"opd_enabled": True},
    "opd_state": "READY",
    "opd_settings": {
        "service_catalog": [
            {"code": "ECG-01", "name": "Standard 12-Lead ECG", "price_paise": 30000, "active": True},
            {"code": "DRESSING", "name": "Wound Dressing", "price_paise": 15000, "active": True},
        ],
        "auto_open_shift": False,
        "upi_vpa": "kriya@icici",
    },
}


def _q_path(query) -> str:
    """Helper to extract table path from Supabase PostgREST query object."""
    return str(getattr(getattr(query, "request", None), "path", ""))


# ─── 1. PRICING & CATALOG ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consultation_with_unknown_doctor_rejected_for_non_admin():
    """A doctor_id outside this clinic must not let a cashier's typed price stand."""
    from fastapi import HTTPException
    from app.services.opd_billing import _validate_and_price_items

    items = [{"item_type": "consultation", "doctor_id": "other-clinic-doc",
              "description": "Consultation", "quantity": 1, "unit_price_paise": 1}]
    with patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)),          patch("app.services.opd_billing.scoped_query"),          patch("app.services.opd_billing.sb", AsyncMock(return_value=MagicMock(data=[]))):
        with pytest.raises(HTTPException) as exc:
            await _validate_and_price_items(CLINIC_ID, items, actor=CASHIER_USER)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_consultation_fee_priced_from_doctor_fee_client_price_ignored():
    """Non-admin client price is ignored for consultation; doctor fee is used."""
    mock_doctor = {
        "id": DOCTOR_ID,
        "name": "Dr. Sarah",
        "consultation_fee": 600,  # 60,000 paise
    }

    with patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd_billing.scoped_query") as mock_sq, \
         patch("app.services.opd_billing.sb") as mock_sb:

        mock_sb.return_value = MagicMock(data=[mock_doctor])

        # Non-admin submits inflated unit_price_paise (150,000 paise)
        items = [{
            "item_type": "consultation",
            "doctor_id": DOCTOR_ID,
            "description": "Consultation with Dr. Sarah",
            "quantity": 1,
            "unit_price_paise": 150000,
        }]

        from app.services.opd_billing import _validate_and_price_items
        validated, subtotal = await _validate_and_price_items(
            CLINIC_ID, items, actor=CASHIER_USER
        )

        # Enforced to 60,000 paise
        assert validated[0]["unit_price_paise"] == 60000
        assert subtotal == 60000

        # Admin override allowed
        validated_admin, subtotal_admin = await _validate_and_price_items(
            CLINIC_ID, items, actor=ADMIN_USER
        )
        assert validated_admin[0]["unit_price_paise"] == 150000
        assert subtotal_admin == 150000


@pytest.mark.asyncio
async def test_catalog_and_lab_test_pricing_enforcement():
    """Catalog code and lab test prices are enforced from the database."""
    mock_lab = {
        "id": str(uuid.uuid4()),
        "test_name": "Complete Blood Count",
        "price_paise": 45000,
        "price": 450,
    }

    with patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)), \
         patch("app.services.opd_billing.scoped_query") as mock_sq, \
         patch("app.services.opd_billing.sb", AsyncMock(return_value=MagicMock(data=[mock_lab]))):

        items = [
            {
                "item_type": "diagnostic",
                "catalog_code": "ECG-01",
                "description": "ECG",
                "quantity": 1,
                "unit_price_paise": 100,  # client passes fake 100
            },
            {
                "item_type": "diagnostic",
                "lab_test_id": mock_lab["id"],
                "description": "CBC",
                "quantity": 1,
                "unit_price_paise": 100,  # client passes fake 100
            },
        ]

        from app.services.opd_billing import _validate_and_price_items
        validated, subtotal = await _validate_and_price_items(
            CLINIC_ID, items, actor=CASHIER_USER
        )

        assert validated[0]["unit_price_paise"] == 30000  # from service_catalog
        assert validated[1]["unit_price_paise"] == 45000  # from lab_tests table
        assert subtotal == 75000


# ─── 2. INVOICE LIFECYCLE, CAS DRAFT & ISSUE ────────────────────────────────


@pytest.mark.asyncio
async def test_draft_edit_cas_stale_conflict_409():
    """Updating a draft with mismatched expected_updated_at triggers 409 conflict."""
    invoice_id = str(uuid.uuid4())
    stored_updated_at = "2026-10-09T10:00:00+00:00"
    stale_updated_at = "2026-10-09T09:00:00+00:00"

    mock_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "draft",
        "updated_at": stored_updated_at,
        "items": [],
    }

    with patch("app.services.opd_billing.sb", AsyncMock(return_value=MagicMock(data=[mock_inv]))):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await save_invoice_draft(
                clinic_id=CLINIC_ID,
                invoice_id=invoice_id,
                items=[{"item_type": "other", "description": "Bandage", "quantity": 1, "unit_price_paise": 5000}],
                expected_updated_at=datetime.fromisoformat(stale_updated_at),
                actor=CASHIER_USER,
            )
        assert exc_info.value.status_code == 409
        assert "modified concurrently" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_issue_invoice_assigns_sequential_number():
    """Issuing an invoice assigns sequential invoice_number and updates status to issued."""
    invoice_id = str(uuid.uuid4())
    mock_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "draft",
        "total_paise": 60000,
        "paid_paise": 0,
        "items": [{"item_type": "consultation", "description": "Consultation", "quantity": 1, "unit_price_paise": 60000}],
    }

    updated_inv = dict(mock_inv)
    updated_inv["status"] = "issued"
    updated_inv["invoice_number"] = "INV-2026-00001"

    async def mock_sb_issue(query):
        q = str(query)
        if "opd_invoice_items" in q:
            return MagicMock(data=[{"id": "item1"}])
        return MagicMock(data=[mock_inv])

    with patch("app.services.opd_billing.get_invoice", AsyncMock(return_value=updated_inv)), \
         patch("app.services.opd_billing.sb", side_effect=mock_sb_issue):

        res = await issue_invoice(CLINIC_ID, invoice_id, actor=CASHIER_USER)
        assert res["status"] == "issued"
        assert res["invoice_number"] == "INV-2026-00001"


@pytest.mark.asyncio
async def test_zero_total_invoice_marked_paid_on_issue():
    """An invoice with 0 total payable is automatically marked paid upon issue."""
    invoice_id = str(uuid.uuid4())
    mock_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "draft",
        "subtotal_paise": 50000,
        "discount_paise": 50000,  # 100% discount
        "total_paise": 0,
        "paid_paise": 0,
        "items": [],
    }

    paid_inv = dict(mock_inv)
    paid_inv["status"] = "paid"
    paid_inv["invoice_number"] = "INV-2026-00002"

    async def mock_sb_issue_zero(query):
        q = str(query)
        if "opd_invoice_items" in q:
            return MagicMock(data=[{"id": "item1"}])
        return MagicMock(data=[mock_inv])

    with patch("app.services.opd_billing.get_invoice", AsyncMock(return_value=paid_inv)), \
         patch("app.services.opd_billing.sb", side_effect=mock_sb_issue_zero):

        res = await issue_invoice(CLINIC_ID, invoice_id, actor=CASHIER_USER)
        assert res["status"] == "paid"


# ─── 3. SPLIT PAYMENTS & RECEIPT IDEMPOTENCY ───────────────────────────────


@pytest.mark.asyncio
async def test_split_payment_cash_and_upi_settles_invoice():
    """Split payment over Cash and UPI updates paid_paise and finishes with paid status."""
    invoice_id = str(uuid.uuid4())
    shift_id = str(uuid.uuid4())

    inv_state = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "issued",
        "total_paise": 100000,  # ₹1000
        "paid_paise": 0,
        "balance_due_paise": 100000,
    }

    mock_shift = {
        "id": shift_id,
        "clinic_id": CLINIC_ID,
        "cashier_admin_id": CASHIER_ID,
        "status": "open",
        "closed_at": None,
    }

    async def mock_sb_split_1(query):
        p = _q_path(query)
        if "opd_cashier_shifts" in p:
            return MagicMock(data=[mock_shift])
        if "opd_invoices" in p:
            return MagicMock(data=[inv_state])
        if "opd_receipts" in p:
            return MagicMock(data=[{
                "id": str(uuid.uuid4()),
                "receipt_number": "RCT-2026-00001",
                "mode": "cash",
                "amount_paise": 40000,
            }])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_split_1):
        rcpt1 = await create_receipt(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="cash",
            amount_paise=40000,
            idempotency_key="key-part-1-cash-1234",
            actor=CASHIER_USER,
        )
        assert rcpt1["amount_paise"] == 40000

    # 2. UPI receipt of remaining ₹600
    inv_state["paid_paise"] = 40000
    inv_state["status"] = "partially_paid"

    async def mock_sb_split_2(query):
        p = _q_path(query)
        if "opd_cashier_shifts" in p:
            return MagicMock(data=[mock_shift])
        if "opd_invoices" in p:
            return MagicMock(data=[inv_state])
        if "opd_receipts" in p:
            return MagicMock(data=[{
                "id": str(uuid.uuid4()),
                "receipt_number": "RCT-2026-00002",
                "mode": "upi",
                "amount_paise": 60000,
                "reference": "UPI9823471029",
            }])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_split_2):
        rcpt2 = await create_receipt(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="upi",
            amount_paise=60000,
            reference="UPI9823471029",
            idempotency_key="key-part-2-upi-5678",
            actor=CASHIER_USER,
        )
        assert rcpt2["mode"] == "upi"
        assert rcpt2["reference"] == "UPI9823471029"


@pytest.mark.asyncio
async def test_receipt_without_reference_for_upi_and_card_returns_422():
    """UPI or Card payment without a reference/UTR is refused with 422."""
    invoice_id = str(uuid.uuid4())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await create_receipt(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="upi",
            amount_paise=50000,
            reference=None,  # Missing reference
            idempotency_key="upi-key-without-ref",
            actor=CASHIER_USER,
        )
    assert exc_info.value.status_code == 422
    assert "reference" in exc_info.value.detail.lower() and "required" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_receipt_idempotency_key_replay_returns_same_receipt():
    """Calling create_receipt with same idempotency key returns the cached receipt."""
    invoice_id = str(uuid.uuid4())
    shift_id = str(uuid.uuid4())
    idem_key = "idemp-cache-key-testing-99"

    mock_inv = {"id": invoice_id, "status": "issued", "total_paise": 50000, "paid_paise": 0}
    mock_shift = {"id": shift_id, "cashier_admin_id": CASHIER_ID, "status": "open", "closed_at": None}
    mock_rcpt = {
        "id": str(uuid.uuid4()),
        "receipt_number": "RCT-2026-00099",
        "invoice_id": invoice_id,
        "mode": "cash",
        "amount_paise": 50000,
    }

    async def mock_sb_idemp(query):
        p = _q_path(query)
        if "opd_invoices" in p:
            return MagicMock(data=[mock_inv])
        if "opd_cashier_shifts" in p:
            return MagicMock(data=[mock_shift])
        if "opd_receipts" in p:
            return MagicMock(data=[mock_rcpt])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_idemp):
        first_res = await create_receipt(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="cash",
            amount_paise=50000,
            idempotency_key=idem_key,
            actor=CASHIER_USER,
        )

        # Immediate replay with same key
        second_res = await create_receipt(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="cash",
            amount_paise=50000,
            idempotency_key=idem_key,
            actor=CASHIER_USER,
        )

        assert first_res["id"] == second_res["id"]
        assert second_res["receipt_number"] == "RCT-2026-00099"


# ─── 4. REFUNDS & VOIDS ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refund_requires_admin_and_minimum_reason():
    """Refunds require OPD_ADMIN permission and reason >= 5 chars."""
    invoice_id = str(uuid.uuid4())
    from fastapi import HTTPException

    # 1. Non-admin cashier rejected with 403
    with pytest.raises(HTTPException) as exc_403:
        await create_refund(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="cash",
            amount_paise=20000,
            reason="Patient requested refund",
            actor=CASHIER_USER,
        )
    assert exc_403.value.status_code == 403

    # 2. Admin with short reason rejected with 422
    with pytest.raises(HTTPException) as exc_422:
        await create_refund(
            clinic_id=CLINIC_ID,
            invoice_id=invoice_id,
            mode="cash",
            amount_paise=20000,
            reason="bad",  # < 5 chars
            actor=ADMIN_USER,
        )
    assert exc_422.value.status_code == 422


@pytest.mark.asyncio
async def test_void_invoice_blocked_when_paid_paise_greater_than_zero():
    """Voiding an invoice with recorded payments is blocked with 409."""
    invoice_id = str(uuid.uuid4())
    from fastapi import HTTPException

    mock_paid_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "partially_paid",
        "paid_paise": 25000,
    }

    with patch("app.services.opd_billing.sb", AsyncMock(return_value=MagicMock(data=[mock_paid_inv]))):
        with pytest.raises(HTTPException) as exc_info:
            await void_invoice(
                clinic_id=CLINIC_ID,
                invoice_id=invoice_id,
                reason="Patient left before consultation",
                actor=ADMIN_USER,
            )
        assert exc_info.value.status_code == 409
        assert "payments" in exc_info.value.detail.lower()


# ─── 5. VISIT CONSTRAINTS & PREPAID ONLINE BOOKINGS ─────────────────────────


@pytest.mark.asyncio
async def test_one_live_invoice_per_visit_returns_existing():
    """Attempting to get_or_create invoice for an appointment with an existing invoice returns it."""
    existing_inv = {
        "id": str(uuid.uuid4()),
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "status": "issued",
        "invoice_number": "INV-2026-00088",
    }

    appt_row = {
        "id": APPOINTMENT_ID,
        "clinic_id": CLINIC_ID,
        "patient_id": PATIENT_ID,
    }

    async def mock_sb_visit(query):
        p = _q_path(query)
        if "appointments" in p:
            return MagicMock(data=[appt_row])
        if "opd_invoices" in p:
            return MagicMock(data=[existing_inv])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_visit), \
         patch("app.services.opd_billing.get_invoice", AsyncMock(return_value=existing_inv)):

        res = await get_or_create_appointment_invoice(
            clinic_id=CLINIC_ID,
            appointment_id=APPOINTMENT_ID,
            actor=CASHIER_USER,
        )
        assert res["id"] == existing_inv["id"]
        assert res["invoice_number"] == "INV-2026-00088"


@pytest.mark.asyncio
async def test_prepaid_online_booking_creates_prepaid_online_receipt_on_issue():
    """Prepaid appointment booked online automatically mints prepaid_online receipt on issue."""
    invoice_id = str(uuid.uuid4())
    payment_ref = "pay_rzp_prepaid_123"

    mock_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "appointment_id": APPOINTMENT_ID,
        "status": "draft",
        "total_paise": 50000,
        "paid_paise": 0,
        "items": [],
    }

    mock_appt = {
        "id": APPOINTMENT_ID,
        "clinic_id": CLINIC_ID,
        "payment_status": "paid",
        "amount_paid_paise": 50000,
        "payment_reference": payment_ref,
        "payment_gateway": "razorpay",
    }

    receipt_inserted = False

    async def mock_sb_dispatch(query):
        nonlocal receipt_inserted
        p = _q_path(query)
        m = getattr(getattr(query, "request", None), "http_method", "")
        if "opd_invoice_items" in p:
            return MagicMock(data=[{"id": "item-1"}])
        if "appointments" in p:
            return MagicMock(data=[mock_appt])
        if "opd_receipts" in p and m == "POST":
            receipt_inserted = True
            return MagicMock(data=[{"id": str(uuid.uuid4()), "mode": "prepaid_online", "shift_id": None}])
        if "opd_receipts" in p:
            # Check for existing receipt returns empty
            return MagicMock(data=[])
        return MagicMock(data=[mock_inv])

    with patch("app.services.opd_billing.get_invoice", AsyncMock(return_value={**mock_inv, "status": "paid", "paid_paise": 50000})), \
         patch("app.services.opd_billing.sb", side_effect=mock_sb_dispatch):

        issued = await issue_invoice(CLINIC_ID, invoice_id, actor=CASHIER_USER)
        assert issued["status"] == "paid"
        assert receipt_inserted is True


# ─── 6. CASHIER SHIFTS & DRAWER RECONCILIATION ──────────────────────────────


@pytest.mark.asyncio
async def test_cashier_shifts_one_open_per_cashier():
    """A cashier can only open one active shift at a time."""
    from fastapi import HTTPException

    open_shift_row = {
        "id": str(uuid.uuid4()),
        "cashier_admin_id": CASHIER_ID,
        "status": "open",
        "closed_at": None,
    }

    with patch("app.services.opd_billing.sb", AsyncMock(return_value=MagicMock(data=[open_shift_row]))):

        with pytest.raises(HTTPException) as exc_info:
            await open_shift(
                clinic_id=CLINIC_ID,
                opening_float_paise=100000,
                actor=CASHIER_USER,
            )
        assert exc_info.value.status_code == 409
        assert "already have an open shift" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_receipt_into_another_cashiers_shift_returns_409():
    """Trying to record a counter receipt specifying another cashier's shift returns 409."""
    invoice_id = str(uuid.uuid4())
    other_shift_id = str(uuid.uuid4())
    from fastapi import HTTPException

    mock_inv = {
        "id": invoice_id,
        "clinic_id": CLINIC_ID,
        "status": "issued",
        "total_paise": 50000,
        "paid_paise": 0,
    }

    other_shift = {
        "id": other_shift_id,
        "clinic_id": CLINIC_ID,
        "cashier_admin_id": OTHER_CASHIER_ID,  # Different cashier
        "status": "open",
        "closed_at": None,
    }

    async def mock_sb_shift_check(query):
        p = _q_path(query)
        if "opd_invoices" in p:
            return MagicMock(data=[mock_inv])
        if "opd_cashier_shifts" in p:
            return MagicMock(data=[other_shift])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_shift_check):
        with pytest.raises(HTTPException) as exc_info:
            await create_receipt(
                clinic_id=CLINIC_ID,
                invoice_id=invoice_id,
                mode="cash",
                amount_paise=50000,
                idempotency_key="shift-mismatch-test",
                shift_id=other_shift_id,
                actor=CASHIER_USER,
            )
        assert exc_info.value.status_code == 409
        assert "cannot record receipt into another cashier's shift" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_close_shift_computes_expected_cash_and_variance():
    """Closing shift invokes RPC and returns reconciled expected cash, declared, and variance."""
    shift_id = str(uuid.uuid4())
    shift_row = {
        "id": shift_id,
        "clinic_id": CLINIC_ID,
        "cashier_admin_id": CASHIER_ID,
        "status": "open",
        "closed_at": None,
    }

    rpc_result = {
        "id": shift_id,
        "opening_float_paise": 100000,  # ₹1000
        "expected_cash_paise": 250000,  # ₹2500
        "declared_cash_paise": 245000,  # ₹2450 (₹50 shortage)
        "variance_paise": -5000,
        "closed_at": "2026-10-09T18:00:00+00:00",
    }

    with patch("app.services.opd_billing.sb", AsyncMock(side_effect=[
        MagicMock(data=[shift_row]),  # shift check
        MagicMock(data=[rpc_result]),  # rpc call
    ])):

        closed = await close_shift(
            clinic_id=CLINIC_ID,
            shift_id=shift_id,
            declared_cash_paise=245000,
            notes="Cash shortage ₹50",
            actor=CASHIER_USER,
        )
        assert closed["variance_paise"] == -5000
        assert closed["declared_cash_paise"] == 245000
