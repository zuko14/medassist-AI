"""Phase A: Razorpay Refund Idempotency Tests.

Verifies:
1. initiate_refund generates a deterministic, stable idempotency key for identical booking + payment.
2. Multiple retries / concurrent calls pass the EXACT SAME idempotency key to Razorpay.
3. Explicit idempotency key is preserved.
4. Prevents duplicate payout/refund at the gateway layer.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.payment import PaymentService


@pytest.mark.asyncio
async def test_initiate_refund_deterministic_idempotency_key():
    """P0-3: initiate_refund must pass the same idempotency key on multiple calls."""
    service = PaymentService()
    booking_id = "00000000-0000-0000-0000-000000000101"
    payment_id = "pay_test_abc123"

    fake_booking = {
        "id": booking_id,
        "clinic_id": "00000000-0000-0000-0000-000000000001",
        "status": "confirmed",
        "payment_id": payment_id,
        "amount_paise": 50000,
        "appointment_date": "2026-12-31",
        "appointment_time": "10:00",
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[fake_booking]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[])

    idempotency_keys_used = []

    async def fake_create_razorpay_refund(**kwargs):
        idempotency_keys_used.append(kwargs.get("idempotency_key"))
        return {"id": "rfnd_test_999", "status": "processed"}

    with patch("app.services.payment.supabase.table", return_value=mock_table), \
         patch.object(service, "_create_razorpay_refund", side_effect=fake_create_razorpay_refund):

        # Call 1
        res1 = await service.initiate_refund(booking_id, reason="patient_requested")
        assert res1["success"] is True

        # Call 2 (simulating retry after timeout)
        res2 = await service.initiate_refund(booking_id, reason="patient_requested")
        assert res2["success"] is True

        # Call 3
        res3 = await service.initiate_refund(booking_id, reason="patient_requested")
        assert res3["success"] is True

    # Assert exactly 3 gateway calls were made and ALL 3 used the exact same deterministic key
    assert len(idempotency_keys_used) == 3
    expected_key = f"ref_{booking_id}_{payment_id}"
    assert idempotency_keys_used[0] == expected_key
    assert idempotency_keys_used[1] == expected_key
    assert idempotency_keys_used[2] == expected_key


@pytest.mark.asyncio
async def test_initiate_refund_respects_explicit_idempotency_key():
    """P0-3: Explicit idempotency_key parameter is passed directly to Razorpay."""
    service = PaymentService()
    booking_id = "00000000-0000-0000-0000-000000000102"
    custom_key = "custom_idemp_key_admin_456"

    fake_booking = {
        "id": booking_id,
        "clinic_id": "00000000-0000-0000-0000-000000000001",
        "status": "confirmed",
        "payment_id": "pay_test_custom",
        "amount_paise": 30000,
        "appointment_date": "2026-12-31",
        "appointment_time": "11:00",
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[fake_booking]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[])

    idempotency_keys_used = []

    async def fake_create_razorpay_refund(**kwargs):
        idempotency_keys_used.append(kwargs.get("idempotency_key"))
        return {"id": "rfnd_test_custom_999", "status": "processed"}

    with patch("app.services.payment.supabase.table", return_value=mock_table), \
         patch.object(service, "_create_razorpay_refund", side_effect=fake_create_razorpay_refund):

        res = await service.initiate_refund(
            booking_id,
            reason="admin_manual",
            idempotency_key=custom_key,
        )
        assert res["success"] is True

    assert len(idempotency_keys_used) == 1
    assert idempotency_keys_used[0] == custom_key


@pytest.mark.asyncio
async def test_concurrent_refund_attempts_share_identical_key():
    """P0-3: 10 concurrent racing refund attempts all use the identical idempotency key."""
    service = PaymentService()
    booking_id = "00000000-0000-0000-0000-000000000103"
    payment_id = "pay_test_concurrent"

    fake_booking = {
        "id": booking_id,
        "clinic_id": "00000000-0000-0000-0000-000000000001",
        "status": "confirmed",
        "payment_id": payment_id,
        "amount_paise": 75000,
        "appointment_date": "2026-12-31",
        "appointment_time": "14:00",
    }

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[fake_booking]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[])

    captured_keys = []

    async def fake_create_razorpay_refund(**kwargs):
        await asyncio.sleep(0.01)
        captured_keys.append(kwargs.get("idempotency_key"))
        return {"id": "rfnd_concurrent_1", "status": "processed"}

    with patch("app.services.payment.supabase.table", return_value=mock_table), \
         patch.object(service, "_create_razorpay_refund", side_effect=fake_create_razorpay_refund):

        tasks = [
            service.initiate_refund(booking_id, reason="concurrent_retry")
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 10
    assert all(r["success"] is True for r in results)
    assert len(captured_keys) == 10
    # Every single concurrent attempt must have used the exact same key
    assert len(set(captured_keys)) == 1
    assert captured_keys[0] == f"ref_{booking_id}_{payment_id}"
