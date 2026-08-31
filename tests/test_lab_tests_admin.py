"""Tests for Lab Tests admin CRUD, CSV import, and permission wiring."""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import asyncio
import app.database as app_db


class TestLabTestsManagePermission:
    def test_permission_registered(self):
        from app.services.permissions import PERMISSIONS

        assert "LAB_TESTS_MANAGE" in PERMISSIONS

    def test_diagnostic_operator_granted_by_default(self):
        from app.services.permissions import ROLE_PRESETS

        assert "LAB_TESTS_MANAGE" in ROLE_PRESETS["DIAGNOSTIC_OPERATOR"]

    def test_lab_operator_granted_by_default(self):
        from app.services.permissions import ROLE_PRESETS

        assert "LAB_TESTS_MANAGE" in ROLE_PRESETS["LAB_OPERATOR"]


class TestLabTestBookingFeatureFlag:
    def test_diagstream_has_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" in PLAN_FEATURES["diagstream"]

    def test_polyclinic_has_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" in PLAN_FEATURES["polyclinic"]

    def test_soloclinic_does_not_have_lab_test_booking(self):
        from app.services.tenant import PLAN_FEATURES

        assert "lab_test_booking" not in PLAN_FEATURES["soloclinic"]


class TestGetLabTests:
    @pytest.mark.asyncio
    async def test_get_lab_tests_filters_by_clinic_and_active(self):
        from app.database import get_lab_tests, supabase

        mock_result = MagicMock()
        mock_result.data = [
            {"id": "t1", "name": "CBC", "price_paise": 50000, "is_active": True}
        ]
        mock_select = MagicMock()
        mock_select.eq.return_value = mock_select
        mock_select.order.return_value = mock_select
        mock_select.execute.return_value = mock_result
        
        with patch.object(supabase, "table") as mock_table:
            mock_table.return_value.select.return_value = mock_select
            result = await get_lab_tests("clinic-1")
            assert result == mock_result.data

    @pytest.mark.asyncio
    async def test_get_lab_test_by_id_returns_none_when_missing(self):
        from app.database import get_lab_test_by_id, supabase

        mock_result = MagicMock()
        mock_result.data = []
        mock_select = MagicMock()
        mock_select.eq.return_value = mock_select
        mock_select.execute.return_value = mock_result
        
        with patch.object(supabase, "table") as mock_table:
            mock_table.return_value.select.return_value = mock_select
            result = await get_lab_test_by_id("clinic-1", "missing-id")
            assert result is None

    @pytest.mark.asyncio
    async def test_get_lab_collection_window_falls_back_to_clinic_config(self):
        from app.database import get_lab_collection_window

        clinic = {"config": {"lab_collection": {"start": "08:00", "end": "12:00", "days": "Mon,Wed,Fri"}}}
        result = await get_lab_collection_window(clinic, branch_id=None)
        assert result == {"start": "08:00", "end": "12:00", "days": "Mon,Wed,Fri"}

    @pytest.mark.asyncio
    async def test_get_lab_collection_window_returns_default_when_unset(self):
        from app.database import get_lab_collection_window

        clinic = {"config": {}}
        result = await get_lab_collection_window(clinic, branch_id=None)
        assert result["start"] and result["end"] and result["days"]


import io
from fastapi.testclient import TestClient


def _make_admin_user(permissions=None):
    from app.routers.admin import AdminUser

    user = AdminUser("staff-user")
    user.username = "labstaff"
    user.role = "staff"
    user.clinic_id = "clinic-1"
    user.user_id = "user-1"
    user.permissions = permissions or ["LAB_TESTS_MANAGE"]
    user.branch_id = None
    return user


class TestLabTestsCrudEndpoints:
    def test_create_lab_test_computes_price_paise_from_rupees(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        mock_new_test = {"id": "new-test-id", "name": "CBC", "price_paise": 50000}
        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[mock_new_test]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests",
                json={"name": "CBC", "price_rupees": 500},
            )

        assert resp.status_code == 200
        insert_call = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_call["price_paise"] == 50000
        assert "price_rupees" not in insert_call


class TestLabTestsCsvImport:
    def test_valid_rows_are_created(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = (
            "name,sample_type,price_rupees,turnaround_hours,fasting_required,prep_instructions\n"
            "CBC,Blood,500,24,false,None required\n"
            "Fasting Sugar,Blood,300,12,true,8 hour fast required\n"
        )

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 2
        assert body["errors"] == []

    def test_malformed_row_is_reported_without_aborting_import(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = (
            "name,sample_type,price_rupees,turnaround_hours,fasting_required,prep_instructions\n"
            "CBC,Blood,not_a_number,24,false,None required\n"
            "Lipid Profile,Blood,400,24,true,12 hour fast\n"
        )

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert body["created"] == 0
        assert len(body["errors"]) == 1
        assert body["errors"][0]["row"] == 2
        assert "Price must be a positive number" in body["errors"][0]["problem"]

    def test_accepts_real_world_aliased_headers(self):
        """Matches the actual ACCUMAX CHATBOT.csv export format: 'TEST NAME,PRICE IN RUPEES'."""
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = (
            "TEST NAME,PRICE IN RUPEES\n"
            "BRUCELLOSIS IGG/IGM (EACH),4430\n"
            "HSV PCR,6450\n"
        )

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 2
        assert body["errors"] == []

    def test_still_rejects_csv_missing_a_price_column_entirely(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = "TEST NAME,NOTES\nCBC,routine\n"

        with patch.object(admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"):
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 400

    def test_accepts_decimal_rupee_prices(self):
        """Real ACCUMAX export rows like 'PROTEIN - PLEURAL FLUID,3601.2'."""
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        csv_content = "TEST NAME,PRICE IN RUPEES\nPROTEIN - PLEURAL FLUID,3601.2\n"

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            resp = client.post(
                "/admin/lab-tests/import-csv",
                files={"file": ("tests.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )

        assert resp.status_code == 200
        assert resp.json()["created"] == 1
        insert_call = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_call["price_paise"] == 360120

    def test_import_real_accumax_csv_file(self):
        """Imports the actual issues/ACCUMAX CHATBOT.csv file if present."""
        import pathlib
        csv_path = pathlib.Path("issues/ACCUMAX CHATBOT.csv")
        if not csv_path.exists():
            pytest.skip("issues/ACCUMAX CHATBOT.csv not found")

        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id"}]
            )
            client = TestClient(app)
            with open(csv_path, "rb") as f:
                resp = client.post(
                    "/admin/lab-tests/import-csv",
                    files={"file": ("ACCUMAX CHATBOT.csv", f, "text/csv")},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 1392
        assert len(body["errors"]) == 4


class TestLabCollectionWindowEndpoint:
    def test_sets_clinic_level_window_when_no_branch_id(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"config": {}}]
            )
            mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": "clinic-1"}]
            )
            client = TestClient(app)
            resp = client.put(
                "/admin/lab-collection-window",
                json={"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"},
            )

        assert resp.status_code == 200
        assert resp.json()["lab_collection"]["start"] == "07:00"

    def test_rejects_bad_time_format(self):
        from app.routers.admin import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        client = TestClient(app)
        resp = client.put(
            "/admin/lab-collection-window",
            json={"start": "7am", "end": "11:00", "days": "Mon,Tue"},
        )
        assert resp.status_code == 422


class TestLabCollectionWindowSundayOverride:
    def test_persists_sunday_override_when_both_fields_given(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"config": {}}]
            )
            mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": "clinic-1"}]
            )
            client = TestClient(app)
            resp = client.put(
                "/admin/lab-collection-window",
                json={
                    "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
                    "sunday_start": "09:00", "sunday_end": "13:00",
                },
            )

        assert resp.status_code == 200
        saved = resp.json()["lab_collection"]
        assert saved["sunday_start"] == "09:00"
        assert saved["sunday_end"] == "13:00"

    def test_omits_sunday_override_when_not_given(self):
        from app.routers.admin import router
        from fastapi import FastAPI
        from app.routers import admin as admin_module

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"config": {}}]
            )
            mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": "clinic-1"}]
            )
            client = TestClient(app)
            resp = client.put(
                "/admin/lab-collection-window",
                json={"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"},
            )

        assert resp.status_code == 200
        saved = resp.json()["lab_collection"]
        assert "sunday_start" not in saved
        assert "sunday_end" not in saved

    def test_rejects_bad_sunday_time_format(self):
        from app.routers.admin import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return _make_admin_user()

        from app.routers.admin import verify_credentials
        app.dependency_overrides[verify_credentials] = fake_user

        client = TestClient(app)
        resp = client.put(
            "/admin/lab-collection-window",
            json={"start": "07:00", "end": "21:00", "days": "Sun", "sunday_start": "9am", "sunday_end": "13:00"},
        )
        assert resp.status_code == 422


class TestFormatCollectionWindow:
    def test_no_sunday_override_returns_flat_range(self):
        from app.database import format_collection_window

        window = {"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"}
        assert format_collection_window(window) == "07:00 - 21:00"

    def test_sunday_override_appended_when_sunday_is_an_operating_day(self):
        from app.database import format_collection_window

        window = {
            "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "sunday_start": "09:00", "sunday_end": "13:00",
        }
        assert format_collection_window(window) == "07:00 - 21:00 (Sun: 09:00 - 13:00)"

    def test_sunday_override_ignored_when_sunday_not_an_operating_day(self):
        from app.database import format_collection_window

        window = {
            "start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat",
            "sunday_start": "09:00", "sunday_end": "13:00",
        }
        assert format_collection_window(window) == "07:00 - 21:00"


class TestAdminManualLabReportUpload:
    def test_upload_valid_pdf_with_match_dispatch(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers.admin import router, verify_credentials, AdminUser

        app = FastAPI()
        app.include_router(router)

        async def fake_user():
            return AdminUser(username="admin", role="super_admin", clinic_id="default")

        app.dependency_overrides[verify_credentials] = fake_user
        client = TestClient(app)

        pdf_bytes = b"%PDF-1.4 test document content"

        # A real MatchResult, not a MagicMock: a mock fabricates any attribute
        # asked of it, which is exactly how admin.py came to read a
        # `matched_patient_name` field that MatchResult has never had.
        from app.services.patient_match import MatchResult

        mock_match = MatchResult(
            status="matched",
            is_safe_to_send=True,
            match_confidence=0.98,
            match_source="patients_table",
            normalized_phone="+919876543210",
            patient_name="Test Patient",
            matched_patient_id="pid-101",
        )

        mock_upload_res = {"id": "rep-101", "status": "sent"}

        with patch("app.services.patient_match.patient_match_service.match", new_callable=AsyncMock, return_value=mock_match), \
             patch("app.services.lab_reports.LabReportService.upload_and_send", new_callable=AsyncMock, return_value=mock_upload_res):
            resp = client.post(
                "/admin/lab-reports/upload",
                data={
                    "patient_phone": "+919876543210",
                    "patient_name": "Test Patient",
                    "report_name": "Lipid Profile",
                    "report_type": "Biochemistry",
                    "clinic_id": "default",
                },
                files={"file": ("report.pdf", pdf_bytes, "application/pdf")},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["report"]["id"] == "rep-101"


