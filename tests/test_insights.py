"""Tests for the admin Insights payload (app/services/analytics.py).

The page is read-only, so what can actually go wrong is the arithmetic:

  - days are bucketed on the CLINIC's calendar, not UTC's. A booking taken at
    02:00 IST belongs to that IST day; bucketing in UTC files it under
    yesterday, which is the defect GET /admin/diagnostic/stats had to fix.
  - "collected" counts a booking that carries a gateway payment id AND still
    stands. `completed` must count: the nightly scheduler job auto-completes
    every past confirmed appointment, so a confirmed-only rule would erase
    yesterday's takings from the chart every morning.
  - a refunded booking is refunded revenue, never collected revenue.
  - every day in the window gets a point, including the quiet ones.
  - the window is paged, because PostgREST caps a response at 1000 rows and a
    truncated page would silently plot a short month.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials
from app.services.analytics import (
    CLINIC_TZ,
    _INSIGHTS_PAGE_ROWS,
    AnalyticsService,
)

CLINIC_ID = "clinic-test-001"
CLINIC_UUID = "11111111-1111-1111-1111-111111111111"


def _today_ist():
    return datetime.now(CLINIC_TZ).date()


def _appt(**overrides):
    """One appointment row shaped like Supabase returns it."""
    booked_at = datetime.now(CLINIC_TZ).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    row = {
        "status": "confirmed",
        "department": "Cardiology",
        "doctor_name": "Dr Rao",
        "appointment_date": _today_ist().isoformat(),
        "created_at": booked_at.isoformat(),
        "booking_type": "consultation",
        "lab_test_name": None,
        "amount_paise": 50000,
        "payment_id": "pay_abc123",
        "patient_phone": "+919876500001",
    }
    row.update(overrides)
    return row


class _Page:
    def __init__(self, data):
        self.data = data


async def _insights(rows, days=7, branch_id=None, reports=None):
    """Run get_insights over canned pages, with the transport patched out."""
    svc = AnalyticsService()
    pages = [_Page(rows)]
    if reports is not None:
        pages.append(_Page(reports))

    async def fake_sb(_builder):
        return pages.pop(0) if pages else _Page([])

    with patch("app.services.analytics.sb", side_effect=fake_sb), patch(
        "app.services.analytics.scoped_query"
    ):
        return await svc.get_insights(
            CLINIC_ID, days=days, branch_id=branch_id, include_reports=reports is not None
        )


class TestWindowShape:
    @pytest.mark.asyncio
    async def test_every_day_in_the_window_gets_a_point(self):
        data = await _insights([_appt()], days=7)
        assert len(data["dates"]) == 7
        assert len(data["labels"]) == 7
        assert data["end_date"] == _today_ist().isoformat()
        assert data["start_date"] == (_today_ist() - timedelta(days=6)).isoformat()
        for series in data["bookings"]["series"].values():
            assert len(series) == 7
        for series in data["revenue"]["series"].values():
            assert len(series) == 7

    @pytest.mark.asyncio
    async def test_window_is_clamped_to_one_year(self):
        data = await _insights([], days=99999)
        assert data["period_days"] == 365

    @pytest.mark.asyncio
    async def test_no_rows_yields_zeroed_series_not_an_error(self):
        data = await _insights([], days=3)
        assert data["errors"] == []
        assert data["bookings"]["totals"]["total"] == 0
        assert data["revenue"]["collected_inr"] == 0
        assert data["bookings"]["series"]["created"] == [0, 0, 0]


class TestDayBucketing:
    @pytest.mark.asyncio
    async def test_early_morning_ist_booking_lands_on_the_ist_day(self):
        """02:00 IST today is 20:30 UTC yesterday — it must plot as today."""
        today = _today_ist()
        two_am_ist = datetime(
            today.year, today.month, today.day, 2, 0, tzinfo=CLINIC_TZ
        )
        data = await _insights([_appt(created_at=two_am_ist.isoformat())], days=7)
        # The last slot is today.
        assert data["bookings"]["series"]["created"][-1] == 1
        assert sum(data["bookings"]["series"]["created"]) == 1


class TestRevenue:
    @pytest.mark.asyncio
    async def test_completed_bookings_still_count_as_collected(self):
        """The nightly job flips confirmed -> completed; the money stayed."""
        data = await _insights(
            [_appt(status="completed", patient_phone="+919876500002")], days=3
        )
        assert data["revenue"]["collected_inr"] == 500.0
        assert data["revenue"]["paid_count"] == 1

    @pytest.mark.asyncio
    async def test_refund_is_refunded_revenue_not_collected(self):
        data = await _insights([_appt(status="refunded")], days=3)
        assert data["revenue"]["collected_inr"] == 0
        assert data["revenue"]["refunded_inr"] == 500.0

    @pytest.mark.asyncio
    async def test_unpaid_booking_is_pending_not_collected(self):
        data = await _insights(
            [_appt(status="pending_payment", payment_id=None)], days=3
        )
        assert data["revenue"]["collected_inr"] == 0
        assert data["revenue"]["pending_inr"] == 500.0

    @pytest.mark.asyncio
    async def test_counter_collected_booking_has_no_gateway_id_so_no_revenue(self):
        """A centre without Razorpay keys books, then collects at the desk."""
        data = await _insights([_appt(payment_id=None)], days=3)
        assert data["revenue"]["collected_inr"] == 0

    @pytest.mark.asyncio
    async def test_average_ticket_divides_by_paid_bookings_only(self):
        rows = [
            _appt(amount_paise=40000, patient_phone="+919876500011"),
            _appt(amount_paise=60000, patient_phone="+919876500012"),
            _appt(
                status="cancelled",
                amount_paise=90000,
                patient_phone="+919876500013",
            ),
        ]
        data = await _insights(rows, days=3)
        assert data["revenue"]["collected_inr"] == 1000.0
        assert data["revenue"]["avg_ticket_inr"] == 500.0


class TestServiceMix:
    @pytest.mark.asyncio
    async def test_lab_test_and_consultation_share_one_service_axis(self):
        rows = [
            _appt(patient_phone="+919876500021"),
            _appt(
                booking_type="lab_test",
                department="Diagnostics",
                doctor_name=None,
                lab_test_name="Thyroid Profile",
                patient_phone="+919876500022",
            ),
        ]
        data = await _insights(rows, days=3)
        names = {s["name"] for s in data["bookings"]["by_service"]}
        assert names == {"Cardiology", "Thyroid Profile"}
        # Departments stay consultation-only, so a lab centre's chart is honest.
        assert [d["name"] for d in data["bookings"]["by_department"]] == ["Cardiology"]
        assert data["bookings"]["totals"]["lab_tests"] == 1
        assert data["bookings"]["totals"]["consultations"] == 1

    @pytest.mark.asyncio
    async def test_repeat_patients_counted_by_distinct_phone(self):
        rows = [
            _appt(patient_phone="+919876500031"),
            _appt(patient_phone="+919876500031"),
            _appt(patient_phone="+919876500032"),
        ]
        data = await _insights(rows, days=3)
        assert data["bookings"]["unique_patients"] == 2
        assert data["bookings"]["repeat_patients"] == 1
        assert data["bookings"]["repeat_rate"] == 50.0


class TestReportDelivery:
    @pytest.mark.asyncio
    async def test_report_section_absent_unless_the_plan_dispatches_reports(self):
        data = await _insights([_appt()], days=3)
        assert data["reports"] is None

    @pytest.mark.asyncio
    async def test_delivery_rate_and_median_turnaround(self):
        now = datetime.now(CLINIC_TZ)
        reports = [
            {
                "status": "sent",
                "uploaded_at": now.isoformat(),
                "sent_at": (now + timedelta(minutes=10)).isoformat(),
                "report_type": "Blood",
                "has_abnormal_values": True,
            },
            {
                "status": "sent",
                "uploaded_at": now.isoformat(),
                "sent_at": (now + timedelta(minutes=30)).isoformat(),
                "report_type": "Blood",
                "has_abnormal_values": False,
            },
            {
                "status": "failed",
                "uploaded_at": now.isoformat(),
                "sent_at": None,
                "report_type": "Radiology",
                "has_abnormal_values": False,
            },
        ]
        data = await _insights([], days=3, reports=reports)
        r = data["reports"]
        assert r["total"] == 3
        assert r["delivered_total"] == 2
        assert r["failed_total"] == 1
        assert r["abnormal_total"] == 1
        assert r["delivery_rate"] == 66.7
        assert r["median_turnaround_minutes"] == 20.0

    @pytest.mark.asyncio
    async def test_a_stuck_report_does_not_drag_the_typical_turnaround(self):
        """Median, not mean — one retry loop must not rewrite the headline."""
        now = datetime.now(CLINIC_TZ)
        reports = [
            {
                "status": "sent",
                "uploaded_at": now.isoformat(),
                "sent_at": (now + timedelta(minutes=m)).isoformat(),
                "report_type": "Blood",
                "has_abnormal_values": False,
            }
            for m in (5, 6, 7, 10000)
        ]
        data = await _insights([], days=3, reports=reports)
        assert data["reports"]["median_turnaround_minutes"] == 6.5


class TestPaging:
    @pytest.mark.asyncio
    async def test_a_full_page_is_followed_by_another_request(self):
        """PostgREST caps at 1000 rows; stopping there truncates the month."""
        svc = AnalyticsService()
        pages = [
            _Page(
                [
                    _appt(patient_phone=f"+9198765{i:05d}")
                    for i in range(_INSIGHTS_PAGE_ROWS)
                ]
            ),
            _Page([_appt(patient_phone="+919876599999")]),
        ]
        calls = {"n": 0}

        async def fake_sb(_builder):
            calls["n"] += 1
            return pages.pop(0) if pages else _Page([])

        with patch("app.services.analytics.sb", side_effect=fake_sb), patch(
            "app.services.analytics.scoped_query"
        ):
            data = await svc.get_insights(CLINIC_ID, days=3)

        assert calls["n"] == 2
        assert data["bookings"]["totals"]["total"] == _INSIGHTS_PAGE_ROWS + 1


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_failing_query_names_the_section_instead_of_500ing(self):
        svc = AnalyticsService()
        with patch(
            "app.services.analytics.sb", side_effect=RuntimeError("db down")
        ), patch("app.services.analytics.scoped_query"):
            data = await svc.get_insights(CLINIC_ID, days=3)
        assert data["errors"] == ["bookings"]
        assert data["bookings"] is None
        assert data["revenue"] is None
        # The envelope still renders: the page shows an inline notice, not a
        # blank screen.
        assert len(data["dates"]) == 3


class TestRoute:
    """GET /admin/insights — who may call it, and what it asks the service."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        yield
        app.dependency_overrides.clear()

    def _as(self, role):
        app.dependency_overrides[verify_credentials] = lambda: AdminUser(
            username="u", role=role, clinic_id=CLINIC_UUID, permissions=["ALL"]
        )

    def test_front_desk_staff_cannot_read_revenue(self, client):
        """The payload carries collected revenue, so it is admin-only like
        the Payments tab. A staff account must be refused, not served."""
        self._as("staff")
        r = client.get("/admin/insights", params={"clinic_id": CLINIC_UUID})
        assert r.status_code == 403

    def test_admin_gets_the_window_it_asked_for(self, client):
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.get_clinic_by_id",
            new=AsyncMock(return_value={"id": CLINIC_UUID, "plan": "soloclinic"}),
        ), patch(
            "app.routers.admin.analytics_service.get_insights",
            new=AsyncMock(return_value={"period_days": 7}),
        ) as spy:
            r = client.get(
                "/admin/insights", params={"clinic_id": CLINIC_UUID, "days": 7}
            )
        assert r.status_code == 200
        assert spy.await_args.kwargs["days"] == 7

    @pytest.mark.parametrize(
        "plan,expected",
        [
            ("soloclinic", False),    # booking only — nothing to dispatch
            ("diagbooking", False),   # lab-test booking, no connector
            ("diagstream", True),     # report delivery is the whole product
            ("polyclinic", True),
            ("enterprise", True),     # wildcard plan
        ],
    )
    def test_report_query_runs_only_for_plans_that_dispatch_reports(
        self, client, plan, expected
    ):
        """An empty report chart is not worth a lab_reports scan."""
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.get_clinic_by_id",
            new=AsyncMock(return_value={"id": CLINIC_UUID, "plan": plan}),
        ), patch(
            "app.routers.admin.analytics_service.get_insights",
            new=AsyncMock(return_value={}),
        ) as spy:
            r = client.get("/admin/insights", params={"clinic_id": CLINIC_UUID})
        assert r.status_code == 200
        assert spy.await_args.kwargs["include_reports"] is expected

    def test_insights_route_forwards_branch_id(self, client):
        """When branch_id is provided and owned, it is resolved and passed to the service."""
        self._as("clinic_admin")
        branch_uuid = "22222222-2222-2222-2222-222222222222"
        with patch(
            "app.routers.admin.resolve_owned_branch",
            new=AsyncMock(return_value={"id": branch_uuid, "clinic_id": CLINIC_UUID}),
        ) as resolve_spy, patch(
            "app.routers.admin.get_clinic_by_id",
            new=AsyncMock(return_value={"id": CLINIC_UUID, "plan": "diagstream"}),
        ), patch(
            "app.routers.admin.analytics_service.get_insights",
            new=AsyncMock(return_value={"branch_id": branch_uuid}),
        ) as svc_spy:
            r = client.get(
                "/admin/insights",
                params={"clinic_id": CLINIC_UUID, "branch_id": branch_uuid},
            )
        assert r.status_code == 200
        assert resolve_spy.await_count == 1
        assert svc_spy.await_args.kwargs["branch_id"] == branch_uuid

    def test_insights_route_rejects_unowned_branch(self, client):
        """Cross-tenant or unassigned branch ID must fail fast with HTTP 404 or 403."""
        from fastapi import HTTPException
        self._as("clinic_admin")
        alien_branch = "33333333-3333-3333-3333-333333333333"
        with patch(
            "app.routers.admin.resolve_owned_branch",
            side_effect=HTTPException(status_code=404, detail="Branch not found"),
        ):
            r = client.get(
                "/admin/insights",
                params={"clinic_id": CLINIC_UUID, "branch_id": alien_branch},
            )
        assert r.status_code == 404
