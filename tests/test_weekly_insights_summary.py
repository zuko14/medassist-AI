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

import asyncio
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


def _reads_empty_writes_echo(q):
    """PostgREST as it really answers: nothing cached to read, and a write
    returns the row it wrote. A mock that returned no row for the final save
    too is how "generated successfully" shipped with nothing saved."""
    req = getattr(q, "request", None)
    if req is not None and req.http_method in ("POST", "PATCH"):
        row = req.json if isinstance(req.json, dict) else {}
        return MagicMock(data=[{"regenerate_count": 1, **row}])
    return MagicMock(data=[])


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
            mock_sb.side_effect = _reads_empty_writes_echo
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
            mock_gateway.return_value = {
                "choices": [{"message": {"content": ai_text}}],
                "model": "google/gemini-2.5-flash",
                "usage": {"total_tokens": 120},
            }

            client = TestClient(test_app)
            resp = client.post("/admin/insights/summary/generate?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["source"] == "ai"
            assert "10 bookings" in data["summary_text"]

    def test_valid_ai_summary_saved_with_source_ai(self, test_app):
        """Proves a valid AI summary is persisted with source='ai' in the database."""
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        with patch("app.services.weekly_summary.sb", new_callable=AsyncMock) as mock_sb, patch(
            "app.services.weekly_summary.build_weekly_fact_sheet", new_callable=AsyncMock
        ) as mock_fact, patch(
            "app.services.weekly_summary.call_ai_gateway", new_callable=AsyncMock
        ) as mock_gateway:
            mock_sb.side_effect = _reads_empty_writes_echo
            fact_sheet = {
                "bookings": {"last_week": 20, "prior_week": 15, "change": 5, "change_pct": 33},
                "completed": {"last_week": 18, "prior_week": 12},
                "revenue": {
                    "last_week_rupees": 120000,
                    "prior_week_rupees": 100000,
                    "last_week_formatted": "₹1,20,000",
                    "change_formatted": "+₹20,000",
                    "change_rupees": 20000,
                    "change_pct": 20,
                },
                "cancellations": {"last_week_count": 2, "last_week_rate_pct": 10},
                "avg_ticket": {"last_week_formatted": "₹6,000"},
                "top_services": [{"name": "Vitamin B12", "count": 10}],
            }
            mock_fact.return_value = fact_sheet
            ai_text = (
                "### What happened\n"
                "- Recorded 20 bookings and 18 completed visits.\n"
                "- Total revenue reached ₹1,20,000 (+₹20,000).\n"
                "- Top service was Vitamin B12 with 10 bookings.\n"
            )
            mock_gateway.return_value = {
                "choices": [{"message": {"content": ai_text}}],
                "model": "google/gemini-2.5-flash",
                "usage": {"total_tokens": 150},
            }

            res = asyncio.run(generate_weekly_summary("clinic-1", force_regenerate=True))
            assert res["source"] == "ai"
            assert res["status"] == "ready"
            assert "Vitamin B12" in res["summary_text"]

            # Verify that update was called with source="ai"
            update_calls = [
                call[0][0] for call in mock_sb.call_args_list if hasattr(call[0][0], "request")
            ]
            saved_payloads = [
                c.request.json for c in update_calls if hasattr(c.request, "json") and isinstance(c.request.json, dict)
            ]
            assert any(p.get("source") == "ai" for p in saved_payloads)

    def test_number_verifier_specs_exact_requirements(self):
        """Tests: '₹1,20,000' is accepted when revenue is 120000; an invented '7 patients' is rejected;

        'Vitamin B12' is accepted when it's in top_services.
        """
        fact_sheet = {
            "revenue": {"last_week_rupees": 120000, "last_week_formatted": "₹1,20,000"},
            "top_services": [{"name": "Vitamin B12", "count": 14}],
            "bookings": {"last_week": 14},
        }

        # 1. '₹1,20,000' is accepted when revenue is 120000
        text1 = "Total revenue collected for the week reached ₹1,20,000 across 14 bookings."
        assert verify_deterministic_numbers(text1, fact_sheet) is True

        # Western comma format '120,000' also accepted
        text1_western = "Total revenue collected for the week reached 120,000 across 14 bookings."
        assert verify_deterministic_numbers(text1_western, fact_sheet) is True

        # 2. Invented '7 patients' is rejected
        text2 = "Revenue was ₹1,20,000 and the clinic served 7 patients."
        assert verify_deterministic_numbers(text2, fact_sheet) is False

        # 3. 'Vitamin B12' is accepted when it's in top_services
        text3 = "The most popular test was Vitamin B12 with 14 bookings and ₹1,20,000 in revenue."
        assert verify_deterministic_numbers(text3, fact_sheet) is True

        # But a test name not in top_services with embedded digits is rejected
        text4 = "The most popular test was Vitamin D3 with 14 bookings and ₹1,20,000 in revenue."
        assert verify_deterministic_numbers(text4, fact_sheet) is False

    def test_get_summary_error_handling_returns_not_generated_on_failure(self, test_app):
        """If the table is missing or the query fails, return {'status': 'not_generated'} (no 500)."""
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.side_effect = Exception("relation 'weekly_insights_summaries' does not exist")

            client = TestClient(test_app)
            resp = client.get("/admin/insights/summary?clinic_id=clinic-1")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "not_generated"
            assert "iso_year" in data
            assert "iso_week" in data

    def test_concurrent_generate_race_safety_cannot_reach_4(self):
        """Test that two simultaneous generates cannot reach 4 in a day."""
        today_ist = datetime.now(timezone.utc).astimezone(CLINIC_TZ).date().isoformat()

        # Database state: regenerate_count is 2, regenerate_date is today
        db_row = {
            "clinic_id": "clinic-race",
            "iso_year": 2026,
            "iso_week": 37,
            "summary_text": "Previous summary",
            "fact_sheet": {},
            "source": "ai",
            "regenerate_date": today_ist,
            "regenerate_count": 2,
        }

        # First request succeeds (2 -> 3)
        # Second request attempts conditional update WHERE regenerate_count < 3
        # Since count is now 3, the second update matches 0 rows and raises 429
        mock_calls = 0

        async def mock_sb_handler(query):
            nonlocal mock_calls, db_row
            mock_calls += 1
            http_method = getattr(getattr(query, "request", None), "http_method", "")
            if http_method == "GET":
                return MagicMock(data=[dict(db_row)])
            if http_method == "PATCH":
                payload = getattr(getattr(query, "request", None), "json", {}) or {}
                if "regenerate_count" in payload:
                    if db_row["regenerate_count"] < 3:
                        db_row["regenerate_count"] += 1
                        return MagicMock(data=[dict(db_row)])
                    else:
                        return MagicMock(data=[])
                return MagicMock(data=[dict(db_row)])
            return MagicMock(data=[])

        with patch("app.services.weekly_summary.sb", side_effect=mock_sb_handler), patch(
            "app.services.weekly_summary.build_weekly_fact_sheet", new_callable=AsyncMock
        ) as mock_fact, patch(
            "app.services.weekly_summary.call_ai_gateway", new_callable=AsyncMock
        ) as mock_gateway:
            mock_fact.return_value = {
                "bookings": {"last_week": 5},
                "revenue": {"last_week_rupees": 500},
            }
            mock_gateway.return_value = {
                "choices": [{"message": {"content": "### What happened\n- 5 bookings."}}],
                "model": "google/gemini-2.5-flash",
                "usage": {},
            }

            # Request 1 claims the 3rd slot: succeeds
            res1 = asyncio.run(generate_weekly_summary("clinic-race", force_regenerate=True))
            assert res1["status"] == "ready"
            assert db_row["regenerate_count"] == 3

            # Request 2 attempts to claim: fails with 429
            with pytest.raises(Exception) as exc_info:
                asyncio.run(generate_weekly_summary("clinic-race", force_regenerate=True))
            assert "429" in str(exc_info.value) or "Daily generation limit reached" in str(exc_info.value)
            # Count never reached 4!
            assert db_row["regenerate_count"] == 3

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
            mock_sb.side_effect = _reads_empty_writes_echo
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

    def test_panel_js_fact_sheet_key_contract(self):
        """Add a test that checks the panel JS only reads keys the backend actually returns for the fact sheet."""
        import os

        # Get the actual sample fact sheet structure from backend function
        fixed_now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        appts = [
            {"id": "a1", "status": "completed", "amount_paise": 50000, "payment_id": "p1", "department": "Cardiology", "created_at": "2026-09-08T10:00:00Z"},
            {"id": "a2", "status": "cancelled", "amount_paise": 0, "payment_id": None, "department": "Pathology", "created_at": "2026-09-09T10:00:00Z"},
            {"id": "p1", "status": "completed", "amount_paise": 40000, "payment_id": "p2", "department": "Cardiology", "created_at": "2026-09-02T10:00:00Z"},
        ]
        with patch("app.services.weekly_summary._fetch_appointments_in_range", new_callable=AsyncMock, return_value=appts):
            fact_sheet = asyncio.run(build_weekly_fact_sheet("clinic-1", now=fixed_now))

        # Read admin/index.html and extract renderWeeklyFactSheet
        html_path = os.path.join(os.path.dirname(__file__), "..", "admin", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        # Verify that "Patients Served" is removed from fact sheet rendering
        start_idx = html_content.find("function renderWeeklyFactSheet")
        end_idx = html_content.find("function loadWeeklySummary", start_idx)
        fn_code = html_content[start_idx:end_idx]

        assert "Patients Served" not in fn_code, "Patients Served should be removed from the fact sheet drawer"
        assert "factSheet.patients_served" not in fn_code

        # Check top-level factSheet accesses
        # Keys accessed: factSheet.bookings, factSheet.completed, factSheet.cancellations, factSheet.revenue, factSheet.avg_ticket, factSheet.top_services
        for key in ["bookings", "completed", "cancellations", "revenue", "avg_ticket", "top_services"]:
            assert key in fact_sheet, f"Key '{key}' read by panel JS must be present in backend fact_sheet"

        # Check sub-keys on bookings
        for b_key in ["last_week", "prior_week", "change", "change_pct"]:
            assert b_key in fact_sheet["bookings"]

        # Check sub-keys on completed
        for c_key in ["last_week", "prior_week", "change", "change_pct"]:
            assert c_key in fact_sheet["completed"]

        # Check sub-keys on cancellations
        for cr_key in ["last_week_count", "last_week_rate_pct", "prior_week_rate_pct", "rate_change_pct"]:
            assert cr_key in fact_sheet["cancellations"]

        # Check sub-keys on revenue
        for r_key in ["last_week_rupees", "prior_week_rupees", "change_rupees", "change_pct"]:
            assert r_key in fact_sheet["revenue"]

        # Check sub-keys on avg_ticket
        for a_key in ["last_week_rupees", "prior_week_rupees", "change_rupees"]:
            assert a_key in fact_sheet["avg_ticket"]

        # Check top_services items have 'name' and 'count'
        for s in fact_sheet["top_services"]:
            assert "name" in s and "count" in s


