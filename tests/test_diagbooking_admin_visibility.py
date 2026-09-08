"""Diagnostic-test-booking visibility in the admin panel.

Covers the two halves of the defect a diagnostic centre hit: their lab-test
bookings existed in `appointments` (booking_type='lab_test', migration 039)
and were paid through Razorpay, but the panel could neither identify them nor
say what happened to a refund.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent


class TestRefundStateAnnotation:
    """`refund_state` is computed from payment_events — the ONLY place an
    in-flight or failed refund is recorded (initiate_refund writes refund_id
    to the appointments row only after Razorpay confirms)."""

    @staticmethod
    def _mock_events(rows):
        """Patch app.routers.admin.sb so the payment_events query returns rows."""
        result = MagicMock(data=rows)
        return patch("app.routers.admin.sb", new=AsyncMock(return_value=result))

    @pytest.mark.asyncio
    async def test_row_with_refund_id_needs_no_event_lookup(self):
        from app.routers.admin import _annotate_refund_state

        bookings = [{"id": "b1", "refund_id": "rfnd_abc", "status": "cancelled"}]
        with patch("app.routers.admin.sb", new=AsyncMock()) as mock_sb:
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "refunded"
        mock_sb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_initiated_without_completion_is_the_stuck_state(self):
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b1", "refund_id": None, "status": "cancelled", "payment_id": "pay_1"}
        ]
        with self._mock_events([{"booking_id": "b1", "event_type": "refund_initiated"}]):
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "initiated"

    @pytest.mark.asyncio
    async def test_failed_refund_is_not_reported_as_initiated(self):
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b1", "refund_id": None, "status": "cancelled", "payment_id": "pay_1"}
        ]
        with self._mock_events(
            [
                {"booking_id": "b1", "event_type": "refund_initiated"},
                {"booking_id": "b1", "event_type": "refund_failed"},
            ]
        ):
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "failed"

    @pytest.mark.asyncio
    async def test_cancelled_paid_booking_with_no_refund_attempt(self):
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b1", "refund_id": None, "status": "cancelled", "payment_id": "pay_1"}
        ]
        with self._mock_events([]):
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "not_refunded"

    @pytest.mark.asyncio
    async def test_confirmed_unpaid_booking_has_no_refund_state(self):
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b1", "refund_id": None, "status": "confirmed", "payment_id": None}
        ]
        with self._mock_events([]):
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "none"

    @pytest.mark.asyncio
    async def test_empty_list_makes_no_query(self):
        from app.routers.admin import _annotate_refund_state

        with patch("app.routers.admin.sb", new=AsyncMock()) as mock_sb:
            await _annotate_refund_state([])
        mock_sb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_query_failure_falls_back_to_row_data(self):
        """The bookings list must still render if payment_events is unreachable."""
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b1", "refund_id": None, "status": "cancelled", "payment_id": "pay_1"}
        ]
        with patch(
            "app.routers.admin.sb", new=AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await _annotate_refund_state(bookings)
        assert bookings[0]["refund_state"] == "not_refunded"

    @pytest.mark.asyncio
    async def test_one_query_covers_the_whole_page(self):
        """No N+1: a page of bookings costs a single payment_events read."""
        from app.routers.admin import _annotate_refund_state

        bookings = [
            {"id": "b%d" % i, "refund_id": None, "status": "cancelled", "payment_id": "p"}
            for i in range(25)
        ]
        with self._mock_events([]) as mock_sb:
            await _annotate_refund_state(bookings)
        assert mock_sb.await_count == 1


class TestBookingsEndpointSelectsLabTestColumns:
    @staticmethod
    def _bookings_block():
        source = (REPO / "app" / "routers" / "admin.py").read_text(encoding="utf-8")
        return source.split('@router.get("/bookings")')[1].split("@router.get")[0]

    def test_select_includes_lab_test_and_refund_columns(self):
        """A lab-test row carries doctor_name = NULL by design (migration 039),
        so lab_test_name must be selected or the panel renders an empty row."""
        block = self._bookings_block()
        for column in ("booking_type", "lab_test_name", "refund_id", "refunded_at"):
            assert column in block, "/admin/bookings must select %s" % column

    def test_booking_type_filter_rejects_unknown_values(self):
        block = self._bookings_block()
        assert "booking_type must be 'consultation' or 'lab_test'" in block


class TestAdminPanelGating:
    """The panel hid the data rather than lacking it: Appointments, Patients
    and Payments were gated on `booking`, which no diagnostics plan holds."""

    @staticmethod
    def _index():
        return (REPO / "admin" / "index.html").read_text(encoding="utf-8")

    @pytest.mark.parametrize("page", ["appointments", "patients", "payments"])
    def test_booking_table_pages_are_reachable_by_lab_test_plans(self, page):
        html = self._index()
        match = re.search('data-page="%s" data-feature="([^"]+)"' % page, html)
        assert match, "nav entry for %s not found" % page
        assert "lab_test_booking" in match.group(1).split()

    def test_feature_gate_is_an_any_of_match(self):
        html = self._index()
        assert "function planAllowsFeature(feature)" in html
        gate = html.split("function planAllowsFeature(feature)")[1][:400]
        assert ".some(" in gate, "planAllowsFeature must accept a space-separated list"

    def test_lab_bookings_render_their_test_name(self):
        html = self._index()
        assert "function bookingSubjectCell(b)" in html
        assert "bookingSubjectCell(a)" in html, "appointments table must use it"
        assert "${bookingSubjectCell(b)}" in html, "payments table must use it"


class TestPlatformPanelDailyLimit:
    """Daily Report Limit is only meaningful for plans that bundle
    lab_reports — report_dispatch_allowed() is reached only from
    app/services/lab_reports.py."""

    @staticmethod
    def _platform():
        return (REPO / "admin" / "platform.html").read_text(encoding="utf-8")

    def test_limit_field_is_conditional_on_the_plan(self):
        html = self._platform()
        assert 'id="ccDailyLimitGroup"' in html
        assert "function planHasReportAutomation(plan)" in html
        assert 'onchange="applyPlanFieldVisibility()"' in html

    def test_limit_is_not_submitted_for_plans_without_report_automation(self):
        html = self._platform()
        block = html.split("async function submitCreateClinic()")[1][:2500]
        assert "if (planHasReportAutomation(plan))" in block

    def test_diagbooking_is_offered_in_the_create_form(self):
        html = self._platform()
        assert 'value="diagbooking"' in html
        assert "Diagnostic Test Booking" in html

    def test_report_automation_list_matches_the_backend_registry(self):
        """The JS constant mirrors PLAN_FEATURES; drift would show the limit
        field for a plan that cannot dispatch, or hide it for one that can."""
        from app.services.tenant import PLAN_FEATURES

        html = self._platform()
        match = re.search(r"const PLANS_WITH_LAB_REPORTS = \[([^\]]+)\]", html)
        assert match
        js_plans = set(re.findall(r"'([a-z]+)'", match.group(1)))
        backend = {
            plan
            for plan, feats in PLAN_FEATURES.items()
            if "lab_reports" in feats or "*" in feats
        }
        assert js_plans == backend


class TestPlanRegistrationIsConsistent:
    """A new plan slug has to land in every list that validates or displays
    one, or client creation 422s / the panel renders an unstyled badge."""

    def test_diagbooking_accepted_by_every_plan_validator(self):
        from app.routers.clinics import CreateClinicRequest

        req = CreateClinicRequest(
            name="Apex Diagnostics",
            whatsapp_number="+919876543210",
            plan="diagbooking",
            meta_phone_number_id="000000000000",
            meta_access_token="EAAG_test",
        )
        assert req.plan == "diagbooking"

    def test_platform_panel_styles_the_new_badge(self):
        html = (REPO / "admin" / "platform.html").read_text(encoding="utf-8")
        assert ".badge-diagbooking" in html

    def test_migration_widens_both_plan_check_constraints(self):
        sql = (REPO / "migrations" / "072_diagbooking_plan.sql").read_text(
            encoding="utf-8"
        )
        assert "clinics_plan_check" in sql
        assert "plan_tiers_plan_name_check" in sql
        assert sql.count("diagbooking") >= 3
