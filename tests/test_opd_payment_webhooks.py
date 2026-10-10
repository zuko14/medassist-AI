"""Tests for OPD Payment Webhooks (Phase 1.4).

Verifies:
1. Razorpay payment_link.paid settles invoice into opd_receipts.
2. Replaying the same webhook 3x is idempotent and only mints 1 receipt.
3. Overpayment returns HTTP 200: the balance becomes a receipt, the excess an open payment exception.
4. Cross-tenant mismatch (Clinic A event references Clinic B invoice) returns 200 and rejects without mutating Clinic B.
5. Void invoice returns 200 with reason='invoice_void': no receipt, the whole payment parked as an exception.
6. PhonePe OPDINV- order webhook settles invoice identically.
7. Existing appointment booking webhook payment path operates without regression.
"""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.opd_billing import settle_opd_payment_link
from app.services.payment import PaymentService, payment_service

client = TestClient(app)

CLINIC_A_ID = str(uuid.uuid4())
CLINIC_B_ID = str(uuid.uuid4())
INVOICE_ID = str(uuid.uuid4())
PAYMENT_LINK_ID = "plink_test_opd_12345"
SECRET_KEY = "test_webhook_secret_key"


def _sign(body_bytes: bytes, secret: str = SECRET_KEY) -> str:
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _q_path(query) -> str:
    return str(getattr(getattr(query, "request", None), "path", ""))


# ─── 1. RAZORPAY PAYMENT_LINK.PAID SETTLEMENT & IDEMPOTENCY ───────────────────


@pytest.mark.asyncio
async def test_razorpay_payment_link_paid_settles_invoice_and_is_idempotent():
    """Razorpay payment_link.paid creates receipt on first call, replays 3x idempotently."""
    mock_inv = {
        "id": INVOICE_ID,
        "clinic_id": CLINIC_A_ID,
        "status": "issued",
        "total_paise": 60000,
        "paid_paise": 0,
        "payment_link_id": PAYMENT_LINK_ID,
    }

    receipts_db = []

    async def mock_sb_handler(query):
        p = _q_path(query)
        m = getattr(getattr(query, "request", None), "http_method", "")
        if "opd_invoices" in p:
            return MagicMock(data=[mock_inv])
        if "opd_receipts" in p:
            if m == "POST":
                # Insert receipt
                payload = getattr(getattr(query, "request", None), "json", {})
                rct = {
                    "id": str(uuid.uuid4()),
                    "clinic_id": CLINIC_A_ID,
                    "invoice_id": INVOICE_ID,
                    "gateway": "razorpay",
                    "gateway_payment_id": "pay_rzp_first_1",
                    "amount_paise": 60000,
                    "mode": "payment_link",
                }
                receipts_db.append(rct)
                return MagicMock(data=[rct])
            # Check for existing
            return MagicMock(data=receipts_db)
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_handler):
        # Call 1: First delivery creates receipt
        res1 = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_first_1",
            amount_paid=60000,
            payment_link_id=PAYMENT_LINK_ID,
            opd_invoice_id=INVOICE_ID,
        )
        assert res1["status"] == "ok"
        assert res1.get("opd_settled") is True
        assert len(receipts_db) == 1

        # Call 2: Replay 1
        res2 = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_first_1",
            amount_paid=60000,
            payment_link_id=PAYMENT_LINK_ID,
            opd_invoice_id=INVOICE_ID,
        )
        assert res2["status"] == "ok"
        assert res2.get("reason") == "already_settled"
        assert len(receipts_db) == 1

        # Call 3: Replay 2
        res3 = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_first_1",
            amount_paid=60000,
            payment_link_id=PAYMENT_LINK_ID,
            opd_invoice_id=INVOICE_ID,
        )
        assert res3["status"] == "ok"
        assert res3.get("reason") == "already_settled"
        assert len(receipts_db) == 1


# ─── 2. AMOUNT MISMATCH ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_razorpay_overpayment_applies_balance_and_parks_excess():
    """Overpayment: receipt for the balance only, excess recorded as an exception, admin alerted."""
    mock_inv = {
        "id": INVOICE_ID,
        "clinic_id": CLINIC_A_ID,
        "status": "issued",
        "total_paise": 50000,
        "paid_paise": 0,
    }

    receipts, exceptions = [], []

    async def mock_sb_mismatch(query):
        p = _q_path(query)
        m = getattr(getattr(query, "request", None), "http_method", "")
        body = getattr(getattr(query, "request", None), "json", None)
        if "opd_invoices" in p:
            return MagicMock(data=[mock_inv])
        if "opd_receipts" in p and m == "POST":
            receipts.append(body)
            return MagicMock(data=[{"id": "rct1"}])
        if "opd_payment_exceptions" in p and m == "POST":
            exceptions.append(body)
            return MagicMock(data=[{"id": "exc1"}])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_mismatch),          patch("connectors.runner.send_admin_alert", new=AsyncMock()) as mock_alert:
        # Patient pays 80,000 paise on a 50,000 balance
        res = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_overpay",
            amount_paid=80000,
            opd_invoice_id=INVOICE_ID,
        )
        assert res["code"] == 200
        assert res["reason"] == "overpaid"
        assert [r["amount_paise"] for r in receipts] == [50000]  # never more than the balance
        assert len(exceptions) == 1
        assert exceptions[0]["paid_paise"] == 80000 and exceptions[0]["applied_paise"] == 50000
        assert exceptions[0]["gateway_payment_id"] == "pay_rzp_overpay"
        mock_alert.assert_awaited_once()  # the excess must reach a human


# ─── 3. CROSS-TENANT MISMATCH ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_event_mismatch_returns_200_and_blocks():
    """Clinic A webhook event targeting Clinic B invoice returns 200 clinic_mismatch."""
    mock_inv_b = {
        "id": INVOICE_ID,
        "clinic_id": CLINIC_B_ID,  # Belongs to Clinic B
        "status": "issued",
        "total_paise": 50000,
        "paid_paise": 0,
    }

    inserted = False

    async def mock_sb_cross(query):
        nonlocal inserted
        p = _q_path(query)
        m = getattr(getattr(query, "request", None), "http_method", "")
        if "opd_invoices" in p:
            return MagicMock(data=[mock_inv_b])
        if "opd_receipts" in p and m == "POST":
            inserted = True
            return MagicMock(data=[{"id": "rct1"}])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_cross):
        # Clinic A webhook delivers this payment
        res = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_cross_tenant",
            amount_paid=50000,
            opd_invoice_id=INVOICE_ID,
        )
        assert res["code"] == 200
        assert res["reason"] == "clinic_mismatch"
        assert inserted is False


# ─── 4. VOID INVOICE REJECTION ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_void_invoice_payment_rejected_gracefully():
    """Payment received for an already-void invoice returns 200 invoice_void."""
    mock_void_inv = {
        "id": INVOICE_ID,
        "clinic_id": CLINIC_A_ID,
        "status": "void",
        "total_paise": 50000,
        "paid_paise": 0,
    }

    inserted = False
    exceptions = []

    async def mock_sb_void(query):
        nonlocal inserted
        p = _q_path(query)
        m = getattr(getattr(query, "request", None), "http_method", "")
        if "opd_invoices" in p:
            return MagicMock(data=[mock_void_inv])
        if "opd_receipts" in p and m == "POST":
            inserted = True
            return MagicMock(data=[{"id": "rct1"}])
        if "opd_payment_exceptions" in p and m == "POST":
            exceptions.append(getattr(getattr(query, "request", None), "json", None))
            return MagicMock(data=[{"id": "exc1"}])
        return MagicMock(data=[])

    with patch("app.services.opd_billing.sb", side_effect=mock_sb_void),          patch("connectors.runner.send_admin_alert", new=AsyncMock()):
        res = await settle_opd_payment_link(
            clinic_id=CLINIC_A_ID,
            gateway="razorpay",
            payment_id="pay_rzp_void_test",
            amount_paid=50000,
            opd_invoice_id=INVOICE_ID,
        )
        assert res["code"] == 200
        assert res["reason"] == "invoice_void"
        assert inserted is False
        assert len(exceptions) == 1 and exceptions[0]["applied_paise"] == 0


# ─── 5. PHONEPE OPDINV- WEBHOOK BRANCH ──────────────────────────────────────


@pytest.mark.asyncio
async def test_phonepe_opd_order_webhook_settles_invoice():
    """PhonePe checkout.order.completed with merchantOrderId 'OPDINV-...' settles invoice."""
    order_id = f"OPDINV-{INVOICE_ID.replace('-', '')[:24]}"
    raw_payload = {
        "event": "checkout.order.completed",
        "payload": {
            "merchantOrderId": order_id,
            "amount": 75000,
        },
    }
    raw_body = json.dumps(raw_payload).encode("utf-8")

    svc = PaymentService()
    clinic = {"id": CLINIC_A_ID, "name": "Test Clinic"}

    mock_check = {
        "status": "paid",
        "transaction_id": "phonepe_tx_9988",
        "amount": 75000,
    }

    with patch.object(svc, "verify_phonepe_authorization", return_value=True), \
         patch.object(svc, "_check_phonepe_payment", AsyncMock(return_value=mock_check)), \
         patch("app.services.payment.is_valid_clinic_scope", return_value=True), \
         patch("app.services.opd_billing.settle_opd_payment_link", AsyncMock(return_value={"status": "ok", "code": 200, "opd_settled": True})) as mock_settle:

        res = await svc.process_phonepe_webhook(
            raw_body=raw_body,
            authorization="Basic dGVzdDp0ZXN0",
            clinic=clinic,
        )
        assert res["status"] == "ok"
        assert res.get("opd_settled") is True
        mock_settle.assert_awaited_once_with(
            clinic_id=CLINIC_A_ID,
            gateway="phonepe",
            payment_id="phonepe_tx_9988",
            amount_paid=75000,
            payment_link_id=order_id,
            opd_invoice_id=None,
        )


# ─── 6. NON-REGRESSION ON BOOKING PAYMENTS ───────────────────────────────────


@pytest.mark.asyncio
async def test_regular_booking_webhook_not_diverted_to_opd():
    """Regular appointment booking without OPD notes routes to standard settlement."""
    booking_id = str(uuid.uuid4())
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_regular_booking",
                    "amount": 50000,
                    "notes": {
                        "booking_id": booking_id,
                        "type": "appointment_booking",
                    },
                }
            }
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = _sign(raw_body, SECRET_KEY)

    svc = PaymentService()

    with patch.object(svc, "_settle_captured_payment", AsyncMock(return_value={"status": "ok", "code": 200})) as mock_booking_settle, \
         patch("app.services.opd_billing.settle_opd_payment_link", AsyncMock()) as mock_opd_settle:

        res = await svc.process_payment_webhook(
            raw_body=raw_body,
            signature=sig,
            webhook_secret=SECRET_KEY,
            clinic_id=CLINIC_A_ID,
        )
        assert res["status"] == "ok"
        mock_booking_settle.assert_awaited_once()
        mock_opd_settle.assert_not_called()


def test_opd_order_id_is_unique_per_link_and_carries_invoice_id():
    """A second link for the same invoice needs a fresh gateway id (Razorpay and
    PhonePe both refuse a reused one), and a payment on an older PhonePe link
    must still find its invoice after a newer link replaced payment_link_id."""
    from app.services.payment import _opd_invoice_id_from_order

    hexid = INVOICE_ID.replace("-", "")
    assert _opd_invoice_id_from_order(f"OPDINV-{hexid}-1760000000") == str(uuid.UUID(INVOICE_ID))
    assert _opd_invoice_id_from_order(f"OPDINV-{hexid[:24]}") is None  # pre-fix links settle by link id
