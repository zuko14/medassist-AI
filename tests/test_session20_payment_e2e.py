"""Session 20: payment lifecycle end-to-end through the real HTTP route.

Real Razorpay HMAC signatures, the real FastAPI webhook route, the real
PaymentService, and a stateful in-memory `appointments` table that returns
appointment_time exactly as Postgres does ("HH:MM:SS"). Only the network edges
(Razorpay refund API, WhatsApp) are faked.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

CLINIC_ID = "11111111-1111-1111-1111-111111111111"
SECRET = "whsec_test_e2e"
CLINIC = {
    "id": CLINIC_ID,
    "name": "E2E Clinic",
    "whatsapp_number": "+911111111111",
    "is_active": True,
    "config": {
        "razorpay_key_id": "rzp_test_x",
        "razorpay_key_secret": "rzp_secret_x",
        "razorpay_webhook_secret": SECRET,
        "cancellation_window_hours": 4,
    },
}


class _Store:
    """Minimal stateful PostgREST stand-in: eq/in_ filters, CAS updates, inserts."""

    def __init__(self):
        self.tables = {"appointments": [], "payment_events": [], "webhook_security_events": []}

    def table(self, name):
        return _Query(self, name)


class _Query:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.filters, self.op, self.payload = [], "select", None

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def __getattr__(self, _name):  # order/limit/neq/is_ ... not needed for these paths
        return lambda *a, **k: self

    def execute(self):
        rows = self.store.tables.setdefault(self.name, [])
        if self.op == "insert":
            rows.append(dict(self.payload))
            return MagicMock(data=[dict(self.payload)])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
        return MagicMock(data=[dict(r) for r in hit])


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Razorpay-Signature": sig, "Content-Type": "application/json"}


def _event(kind: str, payment_id: str, booking: dict) -> dict:
    if kind == "payment.captured":
        return {"event": kind, "payload": {"payment": {"entity": {
            "id": payment_id, "amount": booking["amount_paise"],
            "notes": {"booking_id": booking["id"]},
        }}}}
    return {"event": kind, "payload": {
        "payment": {"entity": {"id": payment_id, "amount": booking["amount_paise"]}},
        "payment_link": {"entity": {
            "id": booking["razorpay_payment_link_id"], "amount_paid": booking["amount_paise"],
        }},
    }}


def _booking(hours_ahead: float) -> dict:
    ist = timezone(timedelta(hours=5, minutes=30))
    slot = datetime.now(ist) + timedelta(hours=hours_ahead)
    return {
        "id": "b-e2e", "clinic_id": CLINIC_ID, "booking_ref": "MC-E2E", "status": "pending_payment",
        "amount_paise": 50000, "patient_phone": "+919876543210", "payment_id": None,
        "razorpay_payment_link_id": "plink_e2e", "doctor_name": "Dr. A",
        "appointment_date": slot.strftime("%Y-%m-%d"),
        "appointment_time": slot.strftime("%H:%M:00"),  # Postgres TIME shape
    }


@pytest.fixture
def world():
    from app.main import app
    from app.services import payment as pay_mod

    store = _Store()
    with patch.object(pay_mod, "supabase", store), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch.object(pay_mod.PaymentService, "_notify_payment_confirmed", new_callable=AsyncMock) as confirmed, \
         patch.object(pay_mod.PaymentService, "_increment_patient_visit_count", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "_alert_admin", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "notify_cancellation_outcome", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "_create_razorpay_refund", new_callable=AsyncMock,
                      return_value={"id": "rfnd_e2e"}) as gateway:
        yield TestClient(app), store, confirmed, gateway


def _post(client, body):
    raw, headers = _signed(body)
    return client.post(f"/webhooks/razorpay/{CLINIC_ID}", content=raw, headers=headers)


def _pay(client, store, hours_ahead):
    booking = _booking(hours_ahead)
    store.tables["appointments"].append(booking)
    assert _post(client, _event("payment.captured", "pay_e2e", booking)).status_code == 200
    return store.tables["appointments"][0]


def test_paid_webhook_confirms_once_and_duplicates_are_harmless(world):
    client, store, confirmed, _ = world
    row = _pay(client, store, hours_ahead=48)
    assert row["status"] == "confirmed" and row["payment_id"] == "pay_e2e"

    # Razorpay redelivers, and also sends payment_link.paid for the same payment.
    assert _post(client, _event("payment.captured", "pay_e2e", row)).status_code == 200
    assert _post(client, _event("payment_link.paid", "pay_e2e", row)).status_code == 200
    assert row["status"] == "confirmed"
    assert confirmed.await_count == 1


def test_forged_signature_changes_nothing(world):
    client, store, confirmed, _ = world
    booking = _booking(48)
    store.tables["appointments"].append(booking)
    raw, headers = _signed(_event("payment.captured", "pay_forged", booking))
    headers["X-Razorpay-Signature"] = "0" * 64
    assert client.post(f"/webhooks/razorpay/{CLINIC_ID}", content=raw, headers=headers).status_code == 400
    assert booking["status"] == "pending_payment"
    confirmed.assert_not_called()


@pytest.mark.asyncio
async def test_patient_cancel_inside_the_window_keeps_the_money(world):
    from app.services.payment import payment_service

    client, store, _, gateway = world
    row = _pay(client, store, hours_ahead=1)  # 4h window, slot in 1h
    result = await payment_service.initiate_refund(row["id"], reason="patient_cancelled", clinic=CLINIC)
    assert result["success"] is False and result["is_late"] is True
    gateway.assert_not_called()
    assert row["status"] == "confirmed"


@pytest.mark.asyncio
async def test_patient_cancel_outside_the_window_refunds_and_frees_the_slot(world):
    from app.services.payment import payment_service

    client, store, _, gateway = world
    row = _pay(client, store, hours_ahead=48)
    result = await payment_service.initiate_refund(row["id"], reason="patient_cancelled", clinic=CLINIC)
    assert result["success"] is True and result["refund_id"] == "rfnd_e2e"
    assert gateway.await_args.kwargs["idempotency_key"] == "ref_b-e2e_pay_e2e"
    assert row["status"] == "refunded"  # not in the slot index any more


@pytest.mark.asyncio
async def test_staff_refund_inside_the_window_still_refunds(world):
    from app.services.payment import payment_service

    client, store, _, gateway = world
    row = _pay(client, store, hours_ahead=1)
    result = await payment_service.initiate_refund(
        row["id"], reason="doctor absent", clinic=CLINIC, enforce_window=False
    )
    assert result["success"] is True
    gateway.assert_awaited_once()
