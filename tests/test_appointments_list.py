"""GET /admin/appointments — the Appointments page's date/month history.

What can actually go wrong here:

  - "booked on 5 Sep" is 5 Sep in the CLINIC's calendar. created_at is a UTC
    timestamptz, so the window must open at 4 Sep 18:30 UTC, not 5 Sep 00:00.
  - a dashboard tile and the list it opens must show one number. The tiles
    count created_at >= dashboard_period_start(days); the list's "last N days"
    must cut on that very boundary, not a re-derived one.
  - the status chips count the whole window while one status is selected, and
    the pager total follows the selected status.
  - the count scan is paged: PostgREST caps a response at 1000 rows.
  - a database failure is an error, never an empty "no appointments" page, and
    never leaks the driver's message.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import (
    _APPOINTMENT_LIST_MAX_SPAN_DAYS,
    AdminUser,
    verify_credentials,
)
from app.services.analytics import _INSIGHTS_PAGE_ROWS, AnalyticsService

CLINIC_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_CLINIC_UUID = "99999999-9999-9999-9999-999999999999"


class _Query:
    """Records a PostgREST builder chain; every method returns the builder."""

    def __init__(self, columns):
        self.columns = columns
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return record

    def called(self, name):
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]


class _Page:
    def __init__(self, data):
        self.data = data


async def _run(pages, **kwargs):
    """list_appointments over canned pages; returns (result, built queries)."""
    built = []
    pages = list(pages)

    def fake_scoped_query(table, clinic_id, columns="*"):
        assert table == "appointments"
        assert clinic_id == CLINIC_UUID
        q = _Query(columns)
        built.append(q)
        return q

    async def fake_sb(_builder):
        return _Page(pages.pop(0)) if pages else _Page([])

    with patch("app.services.analytics.scoped_query", side_effect=fake_scoped_query), patch(
        "app.services.analytics.sb", side_effect=fake_sb
    ):
        result = await AnalyticsService().list_appointments(CLINIC_UUID, **kwargs)
    return result, built


def _statuses(*names):
    return [{"status": n} for n in names]


class TestWindow:
    @pytest.mark.asyncio
    async def test_visit_basis_is_an_inclusive_appointment_date_range(self):
        _, built = await _run(
            [_statuses("confirmed"), [{"id": "a1"}]],
            basis="visit", date_from=date(2026, 9, 5), date_to=date(2026, 9, 5),
        )
        assert len(built) == 2
        for q in built:
            assert q.called("gte") == [(("appointment_date", "2026-09-05"), {})]
            assert q.called("lte") == [(("appointment_date", "2026-09-05"), {})]

    @pytest.mark.asyncio
    async def test_booked_basis_is_the_clinic_calendar_day_not_utc(self):
        _, built = await _run(
            [_statuses("confirmed"), [{"id": "a1"}]],
            basis="booked", date_from=date(2026, 9, 5), date_to=date(2026, 9, 5),
        )
        assert len(built) == 2
        for q in built:
            assert q.called("gte") == [(("created_at", "2026-09-04T18:30:00+00:00"), {})]
            # Exclusive end: the next IST midnight, so no booking is counted twice.
            assert q.called("lt") == [(("created_at", "2026-09-05T18:30:00+00:00"), {})]

    @pytest.mark.asyncio
    async def test_period_days_cuts_on_the_dashboard_tiles_own_boundary(self):
        with patch(
            "app.services.analytics.dashboard_period_start", return_value="2026-08-14"
        ) as boundary:
            _, built = await _run([_statuses("confirmed"), [{"id": "a1"}]], period_days=30)
        boundary.assert_called_with(30)
        assert len(built) == 2
        for q in built:
            assert q.called("gte") == [(("created_at", "2026-08-14"), {})]
            assert q.called("lt") == []

    @pytest.mark.asyncio
    async def test_dashboard_stats_report_the_same_boundary_they_filter_on(self):
        """The tile and its list must share one boundary, not two derivations."""
        appts = MagicMock()
        appts.data = _statuses("confirmed", "cancelled")
        with patch(
            "app.services.analytics.dashboard_period_start", return_value="2026-08-14"
        ), patch("app.services.analytics.supabase") as mock_sb, patch(
            "app.services.analytics.sb", new=AsyncMock(return_value=appts)
        ), patch(
            "app.services.analytics.get_genuine_patients", new=AsyncMock(return_value=[])
        ):
            stats = await AnalyticsService().get_dashboard_stats(CLINIC_UUID, days=30)
        mock_sb.table.return_value.select.return_value.gte.assert_called_with(
            "created_at", "2026-08-14"
        )
        assert stats["period_start"] == "2026-08-14"
        assert stats["total_appointments"] == 2

    @pytest.mark.asyncio
    async def test_missing_dates_fail_closed_instead_of_scanning_all_history(self):
        with pytest.raises(ValueError):
            await _run([], basis="visit", date_from=None, date_to=None)


class TestCountsAndPaging:
    @pytest.mark.asyncio
    async def test_chips_count_the_window_while_total_follows_the_status(self):
        result, built = await _run(
            [_statuses("confirmed", "confirmed", "cancelled", None), [{"id": "c1"}]],
            basis="visit", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
            status="cancelled",
        )
        assert result["summary"] == {"confirmed": 2, "cancelled": 1}
        assert result["window_total"] == 4
        assert result["total"] == 1
        assert result["appointments"] == [{"id": "c1"}]
        scan, page = built
        assert scan.columns == "status"
        assert scan.called("eq") == []  # the scan ignores the status filter
        assert page.called("eq") == [(("status", "cancelled"), {})]

    @pytest.mark.asyncio
    async def test_a_zero_count_status_skips_the_page_query(self):
        result, built = await _run(
            [_statuses("confirmed")],
            basis="visit", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
            status="cancelled",
        )
        assert len(built) == 1
        assert result["total"] == 0
        assert result["appointments"] == []

    @pytest.mark.asyncio
    async def test_an_offset_past_the_end_skips_the_page_query(self):
        result, built = await _run(
            [_statuses("confirmed", "completed")],
            basis="visit", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
            offset=50,
        )
        assert len(built) == 1
        assert result["appointments"] == []
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_the_count_scan_pages_past_the_postgrest_cap(self):
        full = _statuses(*(["completed"] * _INSIGHTS_PAGE_ROWS))
        result, built = await _run(
            [full, _statuses("confirmed"), [{"id": "a1"}]],
            basis="visit", date_from=date(2026, 1, 1), date_to=date(2026, 12, 31),
        )
        assert len(built) == 3  # two scan pages, then the rows
        assert built[1].called("range") == [((_INSIGHTS_PAGE_ROWS, 2 * _INSIGHTS_PAGE_ROWS - 1), {})]
        assert result["window_total"] == _INSIGHTS_PAGE_ROWS + 1
        assert result["summary"]["completed"] == _INSIGHTS_PAGE_ROWS
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_page_range_and_order(self):
        _, built = await _run(
            [_statuses(*(["confirmed"] * 120)), [{"id": "a1"}]],
            basis="visit", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
            limit=50, offset=50,
        )
        page = built[-1]
        assert page.called("range") == [((50, 99), {})]
        assert [a[0] for a, _ in page.called("order")] == ["appointment_date", "appointment_time", "id"]

        _, built = await _run(
            [_statuses("confirmed"), [{"id": "a1"}]],
            basis="booked", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
        )
        assert built[-1].called("order") == [
            (("created_at",), {"desc": True}),
            (("id",), {"desc": True}),
        ]

    @pytest.mark.asyncio
    async def test_a_database_failure_raises_rather_than_reading_as_empty(self):
        with patch(
            "app.services.analytics.scoped_query", side_effect=lambda *a, **k: _Query("*")
        ), patch("app.services.analytics.sb", side_effect=RuntimeError("db down")):
            with pytest.raises(RuntimeError):
                await AnalyticsService().list_appointments(
                    CLINIC_UUID, basis="visit",
                    date_from=date(2026, 9, 1), date_to=date(2026, 9, 1),
                )


class TestRoute:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        yield
        app.dependency_overrides.clear()

    def _as(self, role, clinic_id=CLINIC_UUID):
        app.dependency_overrides[verify_credentials] = lambda: AdminUser(
            username="u", role=role, clinic_id=clinic_id, permissions=["ALL"]
        )

    def _get(self, client, **params):
        return client.get("/admin/appointments", params={"clinic_id": CLINIC_UUID, **params})

    @pytest.mark.parametrize(
        "params",
        [
            {},                                                       # no window at all
            {"date_from": "2026-09-05"},                              # half a range
            {"date_from": "2026-09-06", "date_to": "2026-09-05"},     # inverted
            {"date_from": "2026-01-01", "date_to": "2027-01-02"},     # 367 days
            {"period_days": 30},                                      # visit basis
            {"period_days": 30, "date_basis": "booked", "date_from": "2026-09-01"},
            {"period_days": 0, "date_basis": "booked"},
            {"date_from": "2026-09-01", "date_to": "2026-09-30", "status": "Confirmed'--"},
            {"date_from": "2026-09-01", "date_to": "2026-09-30", "limit": 101},
            {"date_from": "2026-09-01", "date_to": "2026-09-30", "date_basis": "created"},
            {"date_from": "05-09-2026", "date_to": "2026-09-30"},
        ],
    )
    def test_invalid_filters_are_422_and_never_reach_the_database(self, client, params):
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.analytics_service.list_appointments", new=AsyncMock()
        ) as spy:
            r = self._get(client, **params)
        assert r.status_code == 422, r.text
        spy.assert_not_awaited()

    def test_a_full_leap_year_is_allowed(self, client):
        assert _APPOINTMENT_LIST_MAX_SPAN_DAYS == 366
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.analytics_service.list_appointments",
            new=AsyncMock(return_value={"appointments": []}),
        ):
            r = self._get(client, date_from="2028-01-01", date_to="2028-12-31")
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("role", ["clinic_admin", "staff"])
    def test_forwards_the_filter_and_annotates_refunds(self, client, role):
        """Front-desk staff already see /appointments/upcoming; same audience."""
        self._as(role)
        rows = [{"id": "a1", "status": "cancelled", "payment_id": "pay_1"}]
        with patch(
            "app.routers.admin.analytics_service.list_appointments",
            new=AsyncMock(return_value={"appointments": rows, "total": 1}),
        ) as spy, patch(
            "app.routers.admin._annotate_refund_state", new=AsyncMock()
        ) as annotate:
            r = self._get(
                client, date_basis="booked", date_from="2026-09-05",
                date_to="2026-09-05", status="cancelled", limit=25, offset=25,
            )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1
        args, kwargs = spy.await_args
        assert args == (CLINIC_UUID,)
        assert kwargs == {
            "basis": "booked", "date_from": date(2026, 9, 5), "date_to": date(2026, 9, 5),
            "period_days": None, "status": "cancelled", "limit": 25, "offset": 25,
        }
        annotate.assert_awaited_once_with(rows)

    def test_dashboard_window_is_accepted(self, client):
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.analytics_service.list_appointments",
            new=AsyncMock(return_value={"appointments": []}),
        ) as spy:
            r = self._get(client, date_basis="booked", period_days=30, status="confirmed")
        assert r.status_code == 200, r.text
        assert spy.await_args.kwargs["period_days"] == 30

    def test_another_clinics_appointments_are_forbidden(self, client):
        self._as("clinic_admin", clinic_id=OTHER_CLINIC_UUID)
        with patch(
            "app.routers.admin.analytics_service.list_appointments", new=AsyncMock()
        ) as spy:
            r = self._get(client, date_from="2026-09-01", date_to="2026-09-30")
        assert r.status_code == 403
        spy.assert_not_awaited()

    def test_a_failure_is_a_generic_500_not_a_leak(self, client):
        self._as("clinic_admin")
        with patch(
            "app.routers.admin.analytics_service.list_appointments",
            new=AsyncMock(side_effect=RuntimeError("relation secret_table does not exist")),
        ):
            r = self._get(client, date_from="2026-09-01", date_to="2026-09-30")
        assert r.status_code == 500
        assert r.json() == {"detail": "Failed to load appointments"}
        assert "secret" not in r.text
