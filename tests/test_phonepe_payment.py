"""PhonePe gateway end-to-end: checkout, webhook, status recovery, refunds.

The real FastAPI route, the real PaymentService and a stateful in-memory
`appointments` table. PhonePe itself is a stateful fake behind
httpx.MockTransport, so the exact URLs, headers, OAuth form and JSON bodies the
service sends are what is asserted — not a mocked method's arguments.
"""

import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

CLINIC_ID = "22222222-2222-2222-2222-222222222222"
RZP_SECRET = "whsec_rzp"
PP = {
    "phonepe_client_id": "PPCLIENT",
    "phonepe_client_secret": "pp-secret",
    "phonepe_client_version": "1",
    "phonepe_webhook_username": "kriya",
    "phonepe_webhook_password": "hook-pass",
    "phonepe_env": "sandbox",
}
CLINIC = {
    "id": CLINIC_ID,
    "name": "PhonePe Clinic",
    "whatsapp_number": "+91 99999 00000",
    "is_active": True,
    "plan": "soloclinic",
    "config": {
        "payment_gateway": "phonepe",
        "payment_mode": "full",
        "razorpay_key_id": "rzp_test_x",
        "razorpay_key_secret": "rzp_secret_x",
        "razorpay_webhook_secret": RZP_SECRET,
        "cancellation_window_hours": 4,
        **PP,
    },
}
GOOD_AUTH = hashlib.sha256(b"kriya:hook-pass").hexdigest()
SANDBOX = "https://api-preprod.phonepe.com/apis/pg-sandbox"


# ── stateful PostgREST stand-in ───────────────────────────────────────────────

class _Store:
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

    def __getattr__(self, _name):  # order/limit/lt/gte/neq: not needed to model
        return lambda *a, **k: self

    def execute(self):
        rows = self.store.tables.setdefault(self.name, [])
        if self.op == "insert":
            row = dict(self.payload)
            row.setdefault("id", str(uuid.uuid4()))
            rows.append(row)
            return MagicMock(data=[dict(row)])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
        return MagicMock(data=[dict(r) for r in hit])


# ── stateful PhonePe stand-in ─────────────────────────────────────────────────

class FakePhonePe:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.orders: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.fail_pay = False
        self.status_error = False
        self.refund_create_error = False

    def calls(self, suffix):
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/apis/pg-sandbox/v1/oauth/token":
            form = dict(parse_qsl(request.content.decode()))
            if form != {"client_id": "PPCLIENT", "client_version": "1",
                        "client_secret": "pp-secret", "grant_type": "client_credentials"}:
                return httpx.Response(401, json={"code": "UNAUTHORIZED"})
            return httpx.Response(200, json={
                "access_token": "tok-1", "expires_at": int(time.time()) + 3600, "token_type": "O-Bearer",
            })
        if request.headers.get("Authorization") != "O-Bearer tok-1":
            return httpx.Response(401, json={"code": "UNAUTHORIZED"})

        if path == "/apis/pg-sandbox/checkout/v2/pay":
            if self.fail_pay:
                return httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"})
            body = json.loads(request.content)
            self.orders[body["merchantOrderId"]] = {"state": "PENDING", "amount": body["amount"]}
            return httpx.Response(200, json={
                "orderId": "OMO-" + body["merchantOrderId"][:8], "state": "PENDING",
                "expireAt": int(time.time() * 1000) + 600000,
                "redirectUrl": "https://mercury-uat.phonepe.com/transact/uat_v2?token=abc",
            })
        m = re.fullmatch(r"/apis/pg-sandbox/checkout/v2/order/([^/]+)/status", path)
        if m:
            if self.status_error:
                return httpx.Response(503, json={"code": "INTERNAL_SERVER_ERROR"})
            order = self.orders.get(m.group(1))
            if not order:
                return httpx.Response(404, json={"code": "ORDER_NOT_FOUND"})
            details = []
            if order["state"] != "PENDING":
                details = [{"transactionId": order.get("txn", "OM-TXN-1"), "state": order["state"],
                            "amount": order["amount"], "paymentMode": "UPI_QR"}]
            return httpx.Response(200, json={
                "orderId": "OMO-" + m.group(1)[:8], "state": order["state"],
                "amount": order.get("paid", order["amount"]), "paymentDetails": details,
            })
        if path == "/apis/pg-sandbox/payments/v2/refund":
            body = json.loads(request.content)
            if self.refund_create_error:
                return httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"})
            if body["merchantRefundId"] in self.refunds:
                return httpx.Response(400, json={"code": "DUPLICATE_REQUEST"})
            refund = {"refundId": f"OMR{len(self.refunds) + 1}", "state": "PENDING",
                      "amount": body["amount"], "order": body["originalMerchantOrderId"]}
            self.refunds[body["merchantRefundId"]] = refund
            return httpx.Response(200, json={"refundId": refund["refundId"], "amount": body["amount"],
                                             "state": "PENDING"})
        m = re.fullmatch(r"/apis/pg-sandbox/payments/v2/refund/([^/]+)/status", path)
        if m:
            refund = self.refunds.get(m.group(1))
            if not refund:
                return httpx.Response(404, json={"code": "REFUND_NOT_FOUND"})
            return httpx.Response(200, json={"merchantRefundId": m.group(1), **refund})
        return httpx.Response(404)


@pytest.fixture
def world():
    from app.main import app
    from app.services import payment as pay_mod

    pay_mod._phonepe_tokens.clear()
    store, fake = _Store(), FakePhonePe()
    transport = httpx.MockTransport(fake.handler)
    with patch.object(pay_mod, "supabase", store), \
         patch.object(pay_mod.PaymentService, "_phonepe_http",
                      lambda self: httpx.AsyncClient(transport=transport)), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch.object(pay_mod.PaymentService, "_get_doctor_fee_paise", new=AsyncMock(return_value=50000)), \
         patch.object(pay_mod.PaymentService, "_notify_payment_confirmed", new_callable=AsyncMock) as confirmed, \
         patch.object(pay_mod.PaymentService, "_increment_patient_visit_count", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "_alert_admin", new_callable=AsyncMock) as alert, \
         patch.object(pay_mod.PaymentService, "notify_cancellation_outcome", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "_notify_late_payment_refunded", new_callable=AsyncMock), \
         patch.object(pay_mod.PaymentService, "_create_razorpay_refund", new_callable=AsyncMock) as rzp_refund:
        yield {"client": TestClient(app), "store": store, "fake": fake, "confirmed": confirmed,
               "alert": alert, "rzp_refund": rzp_refund, "svc": pay_mod.payment_service}
    pay_mod._phonepe_tokens.clear()


async def _book(w) -> dict:
    ist = timezone(timedelta(hours=5, minutes=30))
    slot = datetime.now(ist) + timedelta(days=2)
    return await w["svc"].create_booking_with_payment(
        clinic_id=CLINIC_ID, patient_phone="+919876543210", patient_name="Asha",
        department="General", doctor_name="Dr. A", doctor_id="doc-1",
        appointment_date=slot.strftime("%Y-%m-%d"), appointment_time=slot.strftime("%H:%M:00"),
        clinic=CLINIC,
    )


def _row(w, booking_id):
    return next(r for r in w["store"].tables["appointments"] if r["id"] == booking_id)


def _webhook(w, body: dict, auth=GOOD_AUTH):
    return w["client"].post(f"/webhooks/phonepe/{CLINIC_ID}", content=json.dumps(body).encode(),
                            headers={"Authorization": auth, "Content-Type": "application/json"})


def _completed(booking_id):
    return {"event": "checkout.order.completed",
            "payload": {"merchantOrderId": booking_id, "state": "COMPLETED", "amount": 50000}}


def _pay(w, booking_id, amount=None, txn="OM-TXN-1"):
    order = w["fake"].orders[booking_id]
    order.update(state="COMPLETED", txn=txn)
    if amount is not None:
        order["paid"] = amount


# ── gateway selection ─────────────────────────────────────────────────────────

def test_payment_mode_follows_the_active_gateway_credentials():
    from app.services.payment import resolve_payment_mode

    assert resolve_payment_mode(CLINIC) == ("full", 100)
    missing = {**CLINIC, "config": {**CLINIC["config"], "phonepe_webhook_password": ""}}
    # Razorpay keys are present but PhonePe is the selected gateway: no gateway.
    assert resolve_payment_mode(missing)[0] == "none"
    legacy = {"config": {"razorpay_key_id": "k", "razorpay_key_secret": "s"}}
    assert resolve_payment_mode(legacy) == ("full", 100)  # pre-PhonePe clinics unchanged


# ── checkout ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkout_creates_phonepe_order_keyed_by_booking_id(world):
    res = await _book(world)

    assert res["success"] and res["gateway"] == "phonepe" and res["gateway_name"] == "PhonePe"
    assert res["payment_link"] == "https://mercury-uat.phonepe.com/transact/uat_v2?token=abc"
    row = _row(world, res["booking_id"])
    assert row["status"] == "pending_payment"
    assert row["payment_gateway"] == "phonepe"
    assert row["gateway_order_id"] == "OMO-" + res["booking_id"][:8]
    assert "razorpay_payment_link_id" not in row

    pay = world["fake"].calls("/checkout/v2/pay")[0]
    assert str(pay.url) == f"{SANDBOX}/checkout/v2/pay"
    body = json.loads(pay.content)
    assert body["merchantOrderId"] == res["booking_id"]
    assert body["amount"] == 50000
    assert 300 <= body["expireAfter"] <= 3600
    assert body["paymentFlow"] == {"type": "PG_CHECKOUT",
                                   "merchantUrls": {"redirectUrl": "https://wa.me/919999900000"}}


@pytest.mark.asyncio
async def test_checkout_failure_cancels_the_hold(world):
    world["fake"].fail_pay = True
    res = await _book(world)
    assert res == {"success": False, "reason": "gateway_error"}
    assert world["store"].tables["appointments"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_oauth_token_is_reused_until_expiry(world):
    await _book(world)
    await _book(world)
    assert len(world["fake"].calls("/v1/oauth/token")) == 1
    assert len(world["fake"].calls("/checkout/v2/pay")) == 2


# ── webhook ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_with_wrong_authorization_changes_nothing(world):
    res = await _book(world)
    _pay(world, res["booking_id"])
    for auth in ("", "deadbeef", hashlib.sha256(b"kriya:wrong").hexdigest()):
        assert _webhook(world, _completed(res["booking_id"]), auth=auth).status_code == 400
    assert _row(world, res["booking_id"])["status"] == "pending_payment"
    assert not world["fake"].calls("/status")
    world["confirmed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_webhook_confirms_once_from_order_status(world):
    res = await _book(world)
    _pay(world, res["booking_id"], txn="OM-TXN-77")

    assert _webhook(world, _completed(res["booking_id"])).status_code == 200
    row = _row(world, res["booking_id"])
    assert row["status"] == "confirmed" and row["payment_id"] == "OM-TXN-77"
    # Redelivery (PhonePe documents duplicate deliveries) is a no-op.
    assert _webhook(world, _completed(res["booking_id"])).status_code == 200
    assert world["confirmed"].await_count == 1


@pytest.mark.asyncio
async def test_webhook_body_is_not_trusted_without_order_status(world):
    """A valid header with a forged 'completed' body cannot confirm an unpaid order."""
    res = await _book(world)  # PhonePe still says PENDING
    assert _webhook(world, _completed(res["booking_id"])).status_code == 200
    assert _row(world, res["booking_id"])["status"] == "pending_payment"
    world["confirmed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_status_api_outage_asks_phonepe_to_redeliver(world):
    res = await _book(world)
    _pay(world, res["booking_id"])
    world["fake"].status_error = True
    assert _webhook(world, _completed(res["booking_id"])).status_code == 503
    assert _row(world, res["booking_id"])["status"] == "pending_payment"


@pytest.mark.asyncio
async def test_amount_mismatch_goes_to_review_never_confirmed(world):
    res = await _book(world)
    _pay(world, res["booking_id"], amount=100)
    assert _webhook(world, _completed(res["booking_id"])).status_code == 200
    assert _row(world, res["booking_id"])["status"] == "pending_review"
    world["confirmed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_late_payment_on_expired_hold_is_refunded_on_phonepe(world):
    res = await _book(world)
    _row(world, res["booking_id"])["status"] = "expired"
    _pay(world, res["booking_id"], txn="OM-LATE")

    assert _webhook(world, _completed(res["booking_id"])).status_code == 200
    row = _row(world, res["booking_id"])
    assert row["status"] == "refunded" and row["refund_reason"] == "late_payment"
    refund = json.loads(world["fake"].calls("/payments/v2/refund")[0].content)
    assert refund["originalMerchantOrderId"] == res["booking_id"]
    assert refund["amount"] == 50000
    assert row["refund_id"] == "OMR1"
    world["rzp_refund"].assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_order_ids_are_ignored(world):
    body = {"event": "checkout.order.completed", "payload": {"merchantOrderId": "WEBSITE-ORDER-9"}}
    assert _webhook(world, body).status_code == 200
    assert not world["fake"].calls("/status")


@pytest.mark.asyncio
async def test_razorpay_webhook_cannot_settle_a_phonepe_booking(world):
    res = await _book(world)
    event = {"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_rzp_1", "amount": 50000, "notes": {"booking_id": res["booking_id"]}}}}}
    raw = json.dumps(event).encode()
    sig = hmac.new(RZP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    resp = world["client"].post(f"/webhooks/razorpay/{CLINIC_ID}", content=raw,
                                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})
    assert resp.status_code == 200
    assert _row(world, res["booking_id"])["status"] == "pending_payment"
    world["confirmed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_failed_webhook_alerts_the_admin(world):
    res = await _book(world)
    body = {"event": "pg.refund.failed", "payload": {
        "originalMerchantOrderId": res["booking_id"], "merchantRefundId": "RF-x",
        "refundId": "OMR9", "state": "FAILED", "amount": 50000}}
    assert _webhook(world, body).status_code == 200
    assert "REFUND FAILED" in world["alert"].await_args.args[1]
    events = [e["event_type"] for e in world["store"].tables["payment_events"]]
    assert "gateway_refund_failed" in events


# ── polling / expiry recovery ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fast_poll_confirms_a_paid_phonepe_hold(world):
    res = await _book(world)
    _pay(world, res["booking_id"])
    assert await world["svc"].poll_recent_pending_payments() == 1
    assert _row(world, res["booking_id"])["status"] == "confirmed"


@pytest.mark.asyncio
async def test_expiry_recovers_paid_releases_unpaid_and_skips_unknown(world):
    paid, unpaid, unknown = await _book(world), await _book(world), await _book(world)
    _pay(world, paid["booking_id"])
    del world["fake"].orders[unknown["booking_id"]]  # status lookup 404 -> unknown
    await world["svc"].expire_stale_bookings()
    assert _row(world, paid["booking_id"])["status"] == "confirmed"
    assert _row(world, unpaid["booking_id"])["status"] == "expired"
    assert _row(world, unknown["booking_id"])["status"] == "pending_payment"


@pytest.mark.asyncio
async def test_hold_without_a_phonepe_order_expires_without_asking(world):
    res = await _book(world)
    _row(world, res["booking_id"])["gateway_order_id"] = None  # crash before order creation
    world["fake"].status_error = True
    await world["svc"].expire_stale_bookings()
    assert _row(world, res["booking_id"])["status"] == "expired"


# ── refunds ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_goes_to_phonepe_and_a_retry_never_refunds_twice(world):
    res = await _book(world)
    _pay(world, res["booking_id"])
    _webhook(world, _completed(res["booking_id"]))

    first = await world["svc"].initiate_refund(res["booking_id"], "admin", clinic=CLINIC, enforce_window=False)
    assert first["success"] and first["refund_id"] == "OMR1"
    assert _row(world, res["booking_id"])["status"] == "refunded"

    # Same business refund again (e.g. a retried request): PhonePe refuses the
    # duplicate merchantRefundId and the status lookup returns the original.
    _row(world, res["booking_id"])["status"] = "confirmed"
    again = await world["svc"].initiate_refund(res["booking_id"], "admin", clinic=CLINIC, enforce_window=False)
    assert again["success"] and again["refund_id"] == "OMR1"
    assert len(world["fake"].refunds) == 1
    world["rzp_refund"].assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_failure_is_reported_not_recorded(world):
    res = await _book(world)
    _pay(world, res["booking_id"])
    _webhook(world, _completed(res["booking_id"]))
    world["fake"].refund_create_error = True
    out = await world["svc"].initiate_refund(res["booking_id"], "admin", clinic=CLINIC, enforce_window=False)
    assert not out["success"] and out["reason"].startswith("phonepe_error")
    assert _row(world, res["booking_id"])["status"] == "confirmed"


@pytest.mark.asyncio
async def test_legacy_razorpay_booking_still_refunds_on_razorpay_after_switch(world):
    """Clinic now on PhonePe; a booking paid earlier on Razorpay (NULL gateway)."""
    world["store"].tables["appointments"].append({
        "id": "b-legacy", "clinic_id": CLINIC_ID, "status": "confirmed", "payment_id": "pay_old",
        "amount_paise": 30000, "appointment_date": "2099-01-01", "appointment_time": "10:00:00",
    })
    world["rzp_refund"].return_value = {"id": "rfnd_old"}
    out = await world["svc"].initiate_refund("b-legacy", "admin", clinic=CLINIC, enforce_window=False)
    assert out["success"] and out["refund_id"] == "rfnd_old"
    assert world["rzp_refund"].await_args.kwargs["payment_id"] == "pay_old"
    assert not world["fake"].calls("/payments/v2/refund")


# ── admin settings ────────────────────────────────────────────────────────────

def _settings_call(clinic, body):
    from app.routers.admin import AdminUser, PaymentSettingsUpdate, update_payment_settings

    admin = AdminUser("owner", role="clinic_admin", clinic_id=clinic["id"], user_id="u1")
    sb = MagicMock()
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[clinic])
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return sb, update_payment_settings(body=PaymentSettingsUpdate(**body), request=req,
                                       clinic_id=clinic["id"], user=admin), admin


@pytest.mark.asyncio
async def test_switching_to_phonepe_without_credentials_is_refused():
    from fastapi import HTTPException

    clinic = {"id": CLINIC_ID, "plan": "soloclinic", "whatsapp_number": "+9111",
              "config": {"razorpay_key_id": "k", "razorpay_key_secret": "s", "payment_mode": "full"}}
    sb, coro, _ = _settings_call(clinic, {"payment_gateway": "phonepe", "phonepe_client_id": "only-id"})
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=clinic)), \
         patch("app.routers.admin.supabase", sb), patch("app.routers.admin.invalidate_tenant_cache"), \
         pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == 422
    sb.table.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_switching_to_phonepe_saves_credentials_and_get_masks_secrets():
    from app.routers.admin import get_payment_settings

    clinic = {"id": CLINIC_ID, "plan": "soloclinic", "whatsapp_number": "+9111",
              "config": {"razorpay_key_id": "k", "razorpay_key_secret": "s", "payment_mode": "full"}}
    body = {"payment_gateway": "phonepe", **{k: v for k, v in PP.items()}}
    sb, coro, admin = _settings_call(clinic, body)
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=clinic)), \
         patch("app.routers.admin.supabase", sb), patch("app.routers.admin.invalidate_tenant_cache"):
        assert (await coro)["success"] is True
    saved = sb.table.return_value.update.call_args[0][0]["config"]
    assert saved["payment_gateway"] == "phonepe"
    assert saved["phonepe_client_secret"] == "pp-secret" and saved["razorpay_key_secret"] == "s"

    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value={**clinic, "config": saved})):
        got = await get_payment_settings(clinic_id=CLINIC_ID, user=admin)
    assert got["payment_gateway"] == "phonepe" and got["phonepe_configured"] is True
    assert got["phonepe_client_secret_masked"].endswith("cret")
    assert "pp-secret" not in json.dumps(got) and "hook-pass" not in json.dumps(got)
    assert got["phonepe_webhook_path"] == f"/webhooks/phonepe/{CLINIC_ID}"
