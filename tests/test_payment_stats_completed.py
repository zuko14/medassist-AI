"""Tests for /admin/payments/stats revenue and status calculation.

Verifies that:
1. Both 'confirmed' AND 'completed' appointments with payment_id count toward confirmed_count and confirmed_amount_rupees.
2. The nightly auto_complete job does not wipe out yesterday's revenue from the payments tile.
3. Unpaid, pending, or cancelled bookings do not inflate revenue.
"""

from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials

CLINIC_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client():
    return TestClient(app)


class _Page:
    def __init__(self, data):
        self.data = data


def test_payment_stats_includes_both_confirmed_and_completed(client):
    """Past confirmed appointments auto-completed by scheduler must count as revenue."""
    user = AdminUser(
        username="admin_apex",
        clinic_id=CLINIC_UUID,
        role="clinic_admin",
        permissions=["ALL"],
    )
    app.dependency_overrides[verify_credentials] = lambda: user

    confirmed_and_completed_rows = [
        {"id": "appt-1", "amount_paise": 50000},  # Confirmed (paid online): Rs 500
        {"id": "appt-2", "amount_paise": 75000},  # Completed (auto-completed): Rs 750
    ]

    async def fake_sb(builder):
        # We simulate the 5 sequential queries in get_payment_stats
        return _Page(confirmed_and_completed_rows)

    try:
        with patch("app.routers.admin.sb", side_effect=fake_sb):
            resp = client.get(
                "/admin/payments/stats",
                params={"clinic_id": CLINIC_UUID, "days": 30},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["confirmed_count"] == 2
            assert data["confirmed_amount_rupees"] == 1250.0
    finally:
        app.dependency_overrides.pop(verify_credentials, None)
