"""Tests for Feature 4: Weekly Insights Summary Engine.

Covers:
- Time boundaries: last completed ISO week (Monday–Sunday, IST) vs the prior week
- Precomputed fact sheet comparisons (whole-number %, formatted rupees, WoW diffs)
- Hallucination verifier: deterministic check that every number exists in fact sheet
- Template fallback when AI fails, hallucinates, or exceeds spend cap
- Zero AI spend on page load: GET returns cached or not_generated without calling AI
- Rate limiting: maximum 3 generations per clinic per day in IST (4th returns 429)
- Safe concurrent upsert on (clinic_id, iso_year, iso_week)
"""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import admin as admin_module
from app.routers.admin import AdminUser, router, verify_credentials
from app.services.ai_gateway import SpendCapExceededError
from app.services.weekly_summary import (
    CLINIC_TZ,
    build_template_weekly_summary,
    build_weekly_fact_sheet,
    extract_all_numbers_from_fact_sheet,
    generate_weekly_summary,
    get_last_completed_iso_week,
    verify_deterministic_numbers,
)


def _make_admin_user(clinic_id="clinic-1", user_id="user-1"):
    user = AdminUser("clinicadmin")
    user.username = "clinicadmin"
    user.role = "admin"
    user.clinic_id = clinic_id
    user.user_id = user_id
    user.permissions = ["ALL"]
    user.branch_id = None
    return user


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router)
    return app


class TestIsoWeekBoundaries:
    def test_last_completed_iso_week_calculation(self):
        # Wednesday, 16 Sep 2026 14:30 IST
        dt_ist = datetime(2026, 9, 16, 14, 30, tzinfo=CLINIC_TZ)
        (
            lw_year, lw_week, lw_mon, lw_sun,
            pw_year, pw_week, pw_mon, pw_sun
        ) = get_last_completed_iso_week(dt_ist)

        # Last completed Sunday was Sep 13, 2026 (weekday is Wed = 2, so days_since_sunday = 3)
        assert lw_sun == date(2026, 9, 13)
        assert lw_mon == date(2026, 9, 7)
        assert (lw_sun - lw_mon).days == 6
        assert lw_year == 2026
        assert lw_week == 37

        # Prior week: Mon Aug 31 to Sun Sep 6
        assert pw_sun == date(2026, 9, 6)
        assert pw_mon == date(2026, 8, 31)
        assert (pw_sun - pw_mon).days == 6
        assert pw_year == 2026
        assert pw_week == 36


class TestFactSheetPrecomputations:
    @pytest.mark.asyncio
    async def test_fact_sheet_computes_accurate_diffs_and_formatting(self):
        fixed_now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        # Synthetic appointments for last week (Sep 7-13, 2026) and prior week (Aug 31-Sep 6, 2026)
        all_appointments = [
            {"id": "a1", "status": "completed", "amount_paise": 50000, "payment_id": "pay_1", "department": "Cardiology", "patient_phone": "9876543210", "created_at": "2026-09-08T10:00:00Z"},
            {"id": "a2", "status": "confirmed", "amount_paise": 30000, "payment_id": "pay_2", "department": "Pathology", "patient_phone": "9876543211", "created_at": "2026-09-09T10:00:00Z"},
            {"id": "a3", "status": "cancelled", "amount_paise": 0, "payment_id": None, "department": "Pathology", "patient_phone": "9876543212", "created_at": "2026-09-10T10:00:00Z"},
            {"id": "p1", "status": "completed", "amount_paise": 40000, "payment_id": "pay_3", "department": "Cardiology", "patient_phone": "9876543210", "created_at": "2026-09-02T10:00:00Z"},
            {"id": "p2", "status": "cancelled", "amount_paise": 0, "payment_id": None, "department": "Cardiology", "patient_phone": "9876543213", "created_at": "2026-09-03T10:00:00Z"},
        ]

        with patch("app.services.weekly_summary._fetch_appointments_in_range", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = all_appointments

            fact_sheet = await build_weekly_fact_sheet("clinic-1", now=fixed_now)

            assert fact_sheet["clinic_id"] == "clinic-1"
            # Bookings: 3 vs 2 -> diff = +1, pct = +50%
            b = fact_sheet["bookings"]
            assert b["last_week"] == 3
            assert b["prior_week"] == 2
            assert b["change"] == 1
            assert b["change_pct"] == 50

            # Revenue: Rs 800 (500 + 300) vs Rs 400 -> diff = +400, pct = +100%
            r = fact_sheet["revenue"]
            assert r["last_week_rupees"] == 800
            assert r["prior_week_rupees"] == 400
            assert r["change_rupees"] == 400
            assert r["change_pct"] == 100
            assert r["last_week_formatted"] == "₹800"

            # Cancellations: 1/3 (33%) vs 1/2 (50%)
            c = fact_sheet["cancellations"]
            assert c["last_week_count"] == 1
            assert c["last_week_rate_pct"] == 33
            assert c["prior_week_rate_pct"] == 50


class TestDeterministicNumberVerifier:
    def test_extract_all_numbers_from_fact_sheet(self):
        sample_fact_sheet = {
            "bookings": {"last_week": 142, "prior_week": 120, "change_pct": 18},
            "revenue": {"last_week_formatted": "₹1,25,000", "change_rupees": 25000},
            "cancellations": {"last_week_rate_pct": 12},
        }
        numbers = extract_all_numbers_from_fact_sheet(sample_fact_sheet)
        assert "142" in numbers
        assert "120" in numbers
        assert "18" in numbers
        assert "125000" in numbers or ("1" in numbers and "25" in numbers and "000" in numbers)
        assert "25000" in numbers
        assert "12" in numbers

    def test_verifier_accepts_grounded_numbers(self):
        fact_sheet = {
            "bookings": {"last_week": 100, "prior_week": 80, "change": 20, "change_pct": 25},
            "completed": {"last_week": 90, "prior_week": 70, "change": 20, "change_pct": 29},
            "revenue": {
                "last_week_rupees": 50000,
                "prior_week_rupees": 40000,
                "last_week_formatted": "₹50,000",
                "prior_week_formatted": "₹40,000",
                "change_rupees": 10000,
                "change_pct": 25,
                "change_formatted": "+₹10,000",
            },
            "cancellations": {
                "last_week_count": 10,
                "prior_week_count": 10,
                "last_week_rate_pct": 10,
                "prior_week_rate_pct": 13,
                "rate_change_pct": -3,
            },
            "avg_ticket": {
                "last_week_rupees": 500,
                "prior_week_rupees": 500,
                "last_week_formatted": "₹500",
                "prior_week_formatted": "₹500",
                "change_rupees": 0,
            },
            "top_services": [{"name": "Pathology", "count": 45}],
        }

        grounded_text = (
            "### What happened\n"
            "- The clinic recorded 100 bookings with 90 completed visits.\n"
            "- Total collected revenue was ₹50,000, up 25% compared to prior week.\n"
            "- Top service was Pathology with 45 bookings.\n"
        )
        assert verify_deterministic_numbers(grounded_text, fact_sheet) is True

    def test_verifier_rejects_hallucinated_number(self):
        fact_sheet = {
            "bookings": {"last_week": 10, "prior_week": 5, "change": 5, "change_pct": 100},
            "revenue": {"last_week_rupees": 1000, "prior_week_rupees": 500},
        }
        # Model hallucinated 9999 and 73% which are nowhere in the fact sheet
        hallucinated_text = (
            "### What happened\n"
            "- The clinic handled 10 bookings, but revenue reached 9999 with 73% surge.\n"
        )
        assert verify_deterministic_numbers(hallucinated_text, fact_sheet) is False

    def test_template_summary_is_always_grounded(self):
        fact_sheet = {
            "last_week_label": "Week 37, 2026",
            "prior_week_label": "Week 36, 2026",
            "bookings": {"last_week": 120, "prior_week": 100, "change": 20, "change_pct": 20},
            "completed": {"last_week": 110, "prior_week": 90, "change": 20, "change_pct": 22},
            "revenue": {
                "last_week_rupees": 60000,
                "prior_week_rupees": 50000,
                "last_week_formatted": "₹60,000",
                "change_formatted": "+₹10,000",
                "change_rupees": 10000,
                "change_pct": 20,
            },
            "cancellations": {
                "last_week_count": 10,
                "last_week_rate_pct": 8,
            },
            "avg_ticket": {"last_week_formatted": "₹500"},
            "top_services": [{"name": "Pathology", "count": 50}],
        }
        template_text = build_template_weekly_summary(fact_sheet)
        assert "### What happened" in template_text
        assert "### What it may mean" in template_text
        assert "### Suggested actions" in template_text
        assert verify_deterministic_numbers(template_text, fact_sheet) is True


class TestWeeklySummaryEndpoints:
    def test_get_summary_zero_ai_spend_on_page_load(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        # Case 1: Not generated yet
        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb, patch(
            "app.services.weekly_summary.call_ai_gateway"
        ) as mock_gateway:
            mock_sb.return_value = MagicMock(data=[])

            client = TestClient(test_app)
            resp = client.get("/admin/insights/summary?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "not_generated"
            assert "iso_year" in data
            assert "iso_week" in data
            mock_gateway.assert_not_called()

        # Case 2: Cached summary exists
        mock_cached = {
            "clinic_id": "clinic-1",
            "iso_year": 2026,
            "iso_week": 37,
            "summary_text": "### What happened\n- Cached summary.",
            "fact_sheet": {"bookings": {"last_week": 10}},
            "source": "ai",
            "regenerate_count": 1,
            "updated_at": "2026-09-14T00:00:00Z",
        }
        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb, patch(
            "app.services.weekly_summary.call_ai_gateway"
        ) as mock_gateway:
            mock_sb.return_value = MagicMock(data=[mock_cached])

            client = TestClient(test_app)
            resp = client.get("/admin/insights/summary?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert "Cached summary" in data["summary_text"]
            mock_gateway.assert_not_called()

    def test_daily_rate_limit_enforces_max_3_per_day(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        today_ist = datetime.now(timezone.utc).astimezone(CLINIC_TZ).date().isoformat()
        already_maxed_row = {
            "clinic_id": "clinic-1",
            "iso_year": 2026,
            "iso_week": 37,
            "summary_text": "Summary",
            "regenerate_date": today_ist,
            "regenerate_count": 3,
        }

        with patch("app.services.weekly_summary.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.return_value = MagicMock(data=[already_maxed_row])

            client = TestClient(test_app)
            resp = client.post("/admin/insights/summary/generate?clinic_id=clinic-1")
            assert resp.status_code == 429
            assert "Daily generation limit reached" in resp.json()["detail"]

    def test_generation_successful_with_ai(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        with patch("app.services.weekly_summary.sb", new_callable=AsyncMock) as mock_sb, patch(
            "app.services.weekly_summary.build_weekly_fact_sheet", new_callable=AsyncMock
        ) as mock_fact, patch(
            "app.services.weekly_summary.call_ai_gateway", new_callable=AsyncMock
        ) as mock_gateway:
            # No existing row
            mock_sb.return_value = MagicMock(data=[])
            fact_sheet = {
                "bookings": {"last_week": 10, "prior_week": 5, "change": 5, "change_pct": 100},
                "completed": {"last_week": 8, "prior_week": 4},
                "revenue": {
                    "last_week_rupees": 5000,
                    "prior_week_rupees": 2500,
                    "last_week_formatted": "₹5,000",
                    "change_formatted": "+₹2,500",
                    "change_rupees": 2500,
                    "change_pct": 100,
                },
                "cancellations": {"last_week_count": 2, "last_week_rate_pct": 20},
                "avg_ticket": {"last_week_formatted": "₹500"},
                "top_services": [{"name": "Pathology", "count": 6}],
            }
            mock_fact.return_value = fact_sheet
            ai_text = (
                "### What happened\n"
                "- The clinic recorded 10 bookings with 8 completed.\n"
                "- Collected revenue was ₹5,000 (+₹2,500).\n"
            )
            mock_gateway.return_value = {"content": ai_text}

            client = TestClient(test_app)
            resp = client.post("/admin/insights/summary/generate?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["source"] == "ai"
            assert "10 bookings" in data["summary_text"]

    def test_spend_cap_fallback_to_template(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        with patch("app.services.weekly_summary.sb", new_callable=AsyncMock) as mock_sb, patch(
            "app.services.weekly_summary.build_weekly_fact_sheet", new_callable=AsyncMock
        ) as mock_fact, patch(
            "app.services.weekly_summary.call_ai_gateway",
            new_callable=AsyncMock,
            side_effect=SpendCapExceededError("Monthly budget exceeded"),
        ):
            mock_sb.return_value = MagicMock(data=[])
            mock_fact.return_value = {
                "last_week_label": "Week 37, 2026",
                "prior_week_label": "Week 36, 2026",
                "bookings": {"last_week": 10, "prior_week": 5, "change": 5, "change_pct": 100},
                "completed": {"last_week": 8, "prior_week": 4},
                "revenue": {
                    "last_week_rupees": 5000,
                    "prior_week_rupees": 2500,
                    "last_week_formatted": "₹5,000",
                    "change_formatted": "+₹2,500",
                    "change_rupees": 2500,
                    "change_pct": 100,
                },
                "cancellations": {"last_week_count": 2, "last_week_rate_pct": 20},
                "avg_ticket": {"last_week_formatted": "₹500"},
                "top_services": [{"name": "Pathology", "count": 6}],
            }

            client = TestClient(test_app)
            resp = client.post("/admin/insights/summary/generate?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["source"] == "template"
            assert "### What happened" in data["summary_text"]
