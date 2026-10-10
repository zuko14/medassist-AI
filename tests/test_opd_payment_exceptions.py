"""OPD online-payment exceptions (migration 104).

An online payment larger than the invoice balance (or for a paid / void invoice)
must never leave money unrecorded: the balance becomes a receipt, the rest an
open exception that an admin refunds through the gateway or marks settled.
"""

import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

CLINIC = str(uuid.uuid4())
INVOICE = str(uuid.uuid4())
ADMIN = SimpleNamespace(role="clinic_admin", user_id=str(uuid.uuid4()), username="admin", permissions=[])
CASHIER = SimpleNamespace(role="staff", user_id=str(uuid.uuid4()), username="cash", permissions=["OPD_BILLING"])


def _ob():
    # Resolved at call time: the suite reloads app modules between files.
    return importlib.import_module("app.services.opd_billing")


class FakeDB:
    """Just enough PostgREST for the settlement path, keyed on table + method."""

    def __init__(self, invoice, receipts=None, exceptions=None):
        self.invoice = invoice
        self.receipts = list(receipts or [])
        self.exceptions = list(exceptions or [])
        self.receipt_error: Exception | None = None      # raise on the next receipt insert
        self.exception_error: Exception | None = None    # raise on the next exception insert
        self.after_receipt_error: dict | None = None  # invoice state once the insert was refused
        self.patches = []

    async def __call__(self, query):
        req = query.request
        path, method, body = str(req.path), req.http_method, req.json
        if "opd_invoices" in path:
            return MagicMock(data=[self.invoice])
        if "opd_receipts" in path:
            if method == "POST":
                if self.receipt_error:
                    err, self.receipt_error = self.receipt_error, None
                    if self.after_receipt_error:
                        self.invoice = self.after_receipt_error
                    raise err
                self.receipts.append(body)
                return MagicMock(data=[body])
            return MagicMock(data=list(self.receipts))
        if "opd_payment_exceptions" in path:
            if method == "POST":
                if self.exception_error:
                    err, self.exception_error = self.exception_error, None
                    raise err
                self.exceptions.append(body)
                return MagicMock(data=[body])
            if method == "PATCH":
                self.patches.append(body)
                open_rows = [e for e in self.exceptions if e.get("status", "open") == "open"]
                if not open_rows:
                    return MagicMock(data=[])
                open_rows[0].update(body)
                return MagicMock(data=[open_rows[0]])
            return MagicMock(data=list(self.exceptions))
        return MagicMock(data=[])


def _inv(status="issued", total=60000, paid=0):
    return {"id": INVOICE, "clinic_id": CLINIC, "status": status, "total_paise": total,
            "paid_paise": paid, "invoice_number": "INV-2026-00001"}


async def _settle(db, amount=60000, payment_id="pay_x1", gateway="razorpay", link="plink_1"):
    with patch("app.services.opd_billing.sb", side_effect=db.__call__), \
         patch("connectors.runner.send_admin_alert", new=AsyncMock()) as alert:
        res = await _ob().settle_opd_payment_link(
            clinic_id=CLINIC, gateway=gateway, payment_id=payment_id,
            amount_paid=amount, payment_link_id=link, opd_invoice_id=INVOICE,
        )
    return res, alert


@pytest.mark.asyncio
async def test_exact_payment_settles_without_exception():
    db = FakeDB(_inv())
    res, alert = await _settle(db)
    assert res.get("opd_settled") is True
    assert [r["amount_paise"] for r in db.receipts] == [60000]
    assert db.exceptions == []
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_partially_paid_invoice_applies_balance_and_parks_rest():
    db = FakeDB(_inv(status="partially_paid", paid=30000))
    res, alert = await _settle(db, amount=60000)
    assert res["reason"] == "overpaid" and res["applied_paise"] == 30000
    assert [r["amount_paise"] for r in db.receipts] == [30000]
    exc = db.exceptions[0]
    assert (exc["paid_paise"], exc["applied_paise"], exc["reason"]) == (60000, 30000, "overpaid")
    assert exc["gateway_order_id"] == "plink_1"
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_paid_invoice_parks_whole_payment():
    """Patient paid at the counter, then also paid the link."""
    db = FakeDB(_inv(status="paid", paid=60000))
    res, _ = await _settle(db)
    assert res["reason"] == "invoice_settled" and res["applied_paise"] == 0
    assert db.receipts == []
    assert db.exceptions[0]["applied_paise"] == 0


@pytest.mark.asyncio
async def test_replay_after_exception_is_a_no_op():
    db = FakeDB(_inv(status="paid", paid=60000), exceptions=[{"gateway_payment_id": "pay_x1", "status": "open"}])
    res, alert = await _settle(db)
    assert res["reason"] == "already_settled"
    assert len(db.exceptions) == 1 and db.receipts == []
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_crash_between_receipt_and_exception_is_repaired_on_replay():
    db = FakeDB(_inv(status="paid", paid=60000),
                receipts=[{"gateway_payment_id": "pay_x1", "amount_paise": 30000}])
    res, _ = await _settle(db, amount=60000)
    assert res["reason"] == "overpaid"
    assert len(db.receipts) == 1  # no second receipt
    assert db.exceptions[0]["applied_paise"] == 30000


@pytest.mark.asyncio
async def test_counter_receipt_race_parks_payment_instead_of_losing_it():
    """DB refuses the receipt (a counter payment settled it first); re-read shows paid."""
    db = FakeDB(_inv())
    db.receipt_error = Exception('new row violates check constraint "opd_invoices_paid_consistent"')
    db.after_receipt_error = _inv(status="paid", paid=60000)
    res, _ = await _settle(db)
    assert res["reason"] == "invoice_settled"
    assert db.receipts == []
    assert db.exceptions[0]["applied_paise"] == 0


@pytest.mark.asyncio
async def test_exception_insert_failure_propagates_so_gateway_retries():
    db = FakeDB(_inv(status="paid", paid=60000))
    db.exception_error = Exception("connection reset")
    with pytest.raises(Exception, match="connection reset"):
        await _settle(db)


@pytest.mark.asyncio
async def test_concurrent_duplicate_exception_insert_is_swallowed():
    db = FakeDB(_inv(status="paid", paid=60000))
    db.exception_error = Exception('duplicate key value violates unique constraint (23505)')
    res, alert = await _settle(db)
    assert res["reason"] == "invoice_settled"
    alert.assert_not_awaited()


# ── Resolution ─────────────────────────────────────────────────────────────


def _open_exc(gateway="razorpay"):
    return {"id": "exc-1", "clinic_id": CLINIC, "invoice_id": INVOICE, "gateway": gateway,
            "gateway_payment_id": "pay_x1", "gateway_order_id": "OPDINV-abc-1", "reason": "overpaid",
            "paid_paise": 60000, "applied_paise": 30000, "excess_paise": 30000, "status": "open"}


@pytest.mark.asyncio
async def test_resolve_requires_opd_admin():
    with pytest.raises(HTTPException) as e:
        await _ob().resolve_payment_exception(CLINIC, "exc-1", "settled_offline", note="Cash back", actor=CASHIER)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_settled_offline_needs_a_note_and_writes_once():
    db = FakeDB(_inv(), exceptions=[_open_exc()])
    with patch("app.services.opd_billing.sb", side_effect=db.__call__):
        with pytest.raises(HTTPException) as e:
            await _ob().resolve_payment_exception(CLINIC, "exc-1", "settled_offline", note="ok", actor=ADMIN)
        assert e.value.status_code == 422
        row = await _ob().resolve_payment_exception(
            CLINIC, "exc-1", "settled_offline", note="Refunded in cash at counter", actor=ADMIN)
        assert row["status"] == "settled_offline"
        assert db.patches[0]["resolved_by_name"] == "admin"
        # Already resolved: a second click is refused, nothing rewritten.
        with pytest.raises(HTTPException) as e2:
            await _ob().resolve_payment_exception(
                CLINIC, "exc-1", "settled_offline", note="Refunded in cash at counter", actor=ADMIN)
        assert e2.value.status_code == 409
    assert len(db.patches) == 1


@pytest.mark.asyncio
async def test_razorpay_refund_refunds_only_the_excess_idempotently():
    db = FakeDB(_inv(), exceptions=[_open_exc()])
    pay = importlib.import_module("app.services.payment")
    refund = AsyncMock(return_value={"id": "rfnd_123"})
    with patch("app.services.opd_billing.sb", side_effect=db.__call__), \
         patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value={"id": CLINIC})), \
         patch.object(pay, "get_razorpay_creds", return_value=("rzp_key", "rzp_secret", "")), \
         patch.object(pay.payment_service, "_create_razorpay_refund", refund):
        row = await _ob().resolve_payment_exception(CLINIC, "exc-1", "refund_gateway", actor=ADMIN)
    args = refund.call_args.args
    assert args[0] == "pay_x1" and args[1] == 30000 and args[3] == "opdx-exc-1"
    assert row["status"] == "refunded" and row["resolution_reference"] == "rfnd_123"


@pytest.mark.asyncio
async def test_phonepe_refund_uses_merchant_order_id():
    db = FakeDB(_inv(), exceptions=[_open_exc("phonepe")])
    pay = importlib.import_module("app.services.payment")
    refund = AsyncMock(return_value={"refundId": "PP-R-1"})
    with patch("app.services.opd_billing.sb", side_effect=db.__call__), \
         patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value={"id": CLINIC})), \
         patch.object(pay.payment_service, "_create_phonepe_refund", refund):
        row = await _ob().resolve_payment_exception(CLINIC, "exc-1", "refund_gateway", actor=ADMIN)
    assert refund.call_args.kwargs["merchant_order_id"] == "OPDINV-abc-1"
    assert refund.call_args.kwargs["amount_paise"] == 30000
    assert row["resolution_reference"] == "PP-R-1"


@pytest.mark.asyncio
async def test_gateway_failure_changes_nothing():
    db = FakeDB(_inv(), exceptions=[_open_exc()])
    pay = importlib.import_module("app.services.payment")
    with patch("app.services.opd_billing.sb", side_effect=db.__call__), \
         patch("app.services.opd_billing.get_clinic_by_id", AsyncMock(return_value={"id": CLINIC})), \
         patch.object(pay, "get_razorpay_creds", return_value=("k", "s", "")), \
         patch.object(pay.payment_service, "_create_razorpay_refund", AsyncMock(side_effect=RuntimeError("400"))):
        with pytest.raises(HTTPException) as e:
            await _ob().resolve_payment_exception(CLINIC, "exc-1", "refund_gateway", actor=ADMIN)
    assert e.value.status_code == 502
    assert db.patches == [] and db.exceptions[0]["status"] == "open"
