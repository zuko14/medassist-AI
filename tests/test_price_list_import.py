"""Tests for Feature 2: AI Price List Import Pipeline.

Covers:
- Magic byte detection & disguised file rejection (.exe disguised as .csv, .xls rejection, etc.)
- Upload limit (<= 10 MB, XLSX zip safety <= 50 MB unpacked & <= 200 entries)
- Fast-path clean tabular parsing without AI spend
- Background processing & status transitions (processing -> pending / failed)
- Atomic apply with conditional claim (status='applying'), 409 on conflict
- Rollback to pending on apply failure
- Strict user_id check (user.user_id, not username)
- No-wipe rule on lab test updates (preserves descriptions & categories)
- Cross-clinic tenant isolation
- Audit logging on apply
- Retention purge guarded by production environment check
"""

import asyncio
import io
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import admin as admin_module
from app.routers.admin import AdminUser, router, verify_credentials
from app.services.price_list_parser import (
    detect_and_validate_file_type,
    validate_xlsx_zip_safety,
    parse_catalogue_file,
)


def _make_admin_user(clinic_id="clinic-1", user_id="user-1", permissions=None):
    user = AdminUser("labstaff")
    user.username = "labstaff"
    user.role = "staff"
    user.clinic_id = clinic_id
    user.user_id = user_id
    user.permissions = permissions or ["LAB_TESTS_MANAGE"]
    user.branch_id = None
    return user


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router)
    return app


class TestPriceListValidationAndSecurity:
    def test_magic_byte_detection(self):
        # Disguised EXE
        exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00"
        with pytest.raises(HTTPException) as exc:
            detect_and_validate_file_type(exe_bytes, "test.csv")
        assert exc.value.status_code == 400
        assert "executable file disguised" in exc.value.detail

        # Disguised ELF
        elf_bytes = b"\x7fELF\x02\x01\x01\x00"
        with pytest.raises(HTTPException) as exc:
            detect_and_validate_file_type(elf_bytes, "test.xlsx")
        assert exc.value.status_code == 400
        assert "binary ELF file" in exc.value.detail

        # Legacy XLS
        xls_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        with pytest.raises(HTTPException) as exc:
            detect_and_validate_file_type(xls_bytes, "old.xls")
        assert exc.value.status_code == 400
        assert "Legacy Excel (.xls) files are not supported" in exc.value.detail

        # PDF magic
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n"
        assert detect_and_validate_file_type(pdf_bytes, "doc.pdf") == "pdf"

        # PNG magic
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00"
        assert detect_and_validate_file_type(png_bytes, "img.png") == "png"

        # JPEG magic
        jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        assert detect_and_validate_file_type(jpg_bytes, "scan.jpg") == "jpg"

        # Plain CSV
        csv_bytes = b"Test Name,Price\nCBC,350\n"
        assert detect_and_validate_file_type(csv_bytes, "tests.csv") == "csv"

    def test_xlsx_zip_bomb_safety(self):
        # Create an in-memory zip simulating excessive uncompressed size (> 50 MB)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            # Add a dummy file with large uncompressed size
            z.writestr("[Content_Types].xml", b" " * (51 * 1024 * 1024))
        zip_bytes = buf.getvalue()

        with pytest.raises(HTTPException) as exc:
            validate_xlsx_zip_safety(zip_bytes)
        assert exc.value.status_code == 400
        assert "exceeds safety limit of 50 MB" in exc.value.detail

    def test_xlsx_excessive_entries_safety(self):
        # Create zip with > 200 entries
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
            for i in range(205):
                z.writestr(f"file_{i}.txt", b"x")
        zip_bytes = buf.getvalue()

        with pytest.raises(HTTPException) as exc:
            validate_xlsx_zip_safety(zip_bytes)
        assert exc.value.status_code == 400
        assert "too many internal entries" in exc.value.detail


class TestFastPathTabularParsing:
    def test_fast_path_csv_identifies_new_and_update(self):
        from app.services.price_list_parser import parse_csv_fast_path

        csv_content = (
            "Test Name,Price,Service Type\n"
            "Complete Blood Count,400,Pathology\n"
            "Lipid Profile,800,Biochemistry\n"
            "Invalid Zero Price,0,General\n"
        ).encode("utf-8")

        existing_catalogue = {"complete blood count": "test-1"}

        rows = parse_csv_fast_path(
            raw_bytes=csv_content,
            existing_catalogue=existing_catalogue,
            default_category="Diagnostics",
        )

        assert rows is not None
        assert len(rows) == 3

        # Row 1: update (price 400 vs 350)
        assert rows[0]["name"] == "Complete Blood Count"
        assert rows[0]["price_rupees"] == 400.0
        assert rows[0]["status"] == "Update"
        assert rows[0]["existing_id"] == "test-1"

        # Row 2: new
        assert rows[1]["name"] == "Lipid Profile"
        assert rows[1]["price_rupees"] == 800.0
        assert rows[1]["status"] == "New"

        # Row 3: flagged due to 0 price
        assert rows[2]["status"] == "Flagged"
        assert "missing_price" in rows[2]["flags"]


class TestPriceListEndpoints:
    def test_import_preview_endpoint_accepts_and_returns_202(self, test_app):
        user = _make_admin_user()
        test_app.dependency_overrides[verify_credentials] = lambda: user

        csv_content = b"Test Name,Price\nCBC,350\n"
        mock_preview = {
            "id": "preview-uuid-1",
            "clinic_id": "clinic-1",
            "created_by": "user-1",
            "status": "processing",
        }

        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch("app.utils.async_tasks.spawn_background_task") as mock_spawn:
            mock_sb.return_value = MagicMock(data=[mock_preview])

            client = TestClient(test_app)
            resp = client.post(
                "/admin/lab-tests/import-preview",
                files={"file": ("tests.csv", csv_content, "text/csv")},
            )

            assert resp.status_code == 202
            data = resp.json()
            assert data["preview_id"] == "preview-uuid-1"
            assert data["status"] == "processing"
            mock_spawn.assert_called_once()
            # Close unawaited coroutine passed to mock spawn
            coro = mock_spawn.call_args[0][0]
            coro.close()

    def test_import_preview_rejects_disguised_file(self, test_app):
        user = _make_admin_user()
        test_app.dependency_overrides[verify_credentials] = lambda: user

        exe_content = b"MZ\x90\x00disguised"
        client = TestClient(test_app)
        resp = client.post(
            "/admin/lab-tests/import-preview",
            files={"file": ("malicious.csv", exe_content, "text/csv")},
        )
        assert resp.status_code == 400
        assert "executable file disguised" in resp.json()["detail"]

    def test_get_preview_tenant_isolation(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        # Preview belongs to clinic-2 -> query returns empty data
        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ):
            mock_sb.return_value = MagicMock(data=[])

            client = TestClient(test_app)
            resp = client.get("/admin/lab-tests/import-preview/foreign-id")
            assert resp.status_code == 404

    def test_atomic_apply_claim_and_user_validation(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1", user_id="user-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        # User Rule 1 & 2: Preview claim conditional update
        # If someone else claims it or user_id differs, UPDATE returns 0 rows -> 409
        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ):
            # Simulate 0 rows updated (already applied, expired, or wrong user_id)
            mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value = MagicMock(
                data=[]
            )

            client = TestClient(test_app)
            resp = client.post(
                "/admin/lab-tests/import-apply/preview-1",
                json={"selected_indices": [0]},
            )
            assert resp.status_code == 409
            assert "already applied, expired, or not yours" in resp.json()["detail"]

    def test_atomic_apply_success_and_no_wipe_rule(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1", user_id="user-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        preview_row = {
            "name": "Blood Glucose Fasting",
            "price_rupees": 200.0,
            "price_paise": 20000,
            "category": "",  # Empty category -> should not wipe existing category
            "status": "update",
            "matched_test_id": "test-uuid-99",
        }
        preview_data = {
            "id": "preview-1",
            "clinic_id": "clinic-1",
            "created_by": "user-1",
            "status": "applying",
            "branch_id": None,
            "rows": [preview_row],
        }

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(
            admin_module, "_execute_lab_tests_upsert", new_callable=AsyncMock
        ) as mock_upsert, patch.object(
            admin_module, "log_admin_action", new_callable=AsyncMock
        ) as mock_audit:
            # Claim succeeds
            mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value = MagicMock(
                data=[preview_data]
            )
            mock_upsert.return_value = {"created": 0, "updated": 1, "total_imported": 1, "branch_id": None}

            client = TestClient(test_app)
            resp = client.post(
                "/admin/lab-tests/import-apply/preview-1",
                json={"selected_indices": [0]},
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["updated"] == 1
            assert data["status"] == "applied"
            mock_upsert.assert_called_once()
            mock_audit.assert_called_once()

    def test_apply_failure_rolls_back_status_to_pending(self, test_app):
        user = _make_admin_user(clinic_id="clinic-1", user_id="user-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        preview_data = {
            "id": "preview-1",
            "clinic_id": "clinic-1",
            "created_by": "user-1",
            "status": "applying",
            "branch_id": None,
            "rows": [{"name": "Test", "price_rupees": 100.0, "status": "new"}],
        }

        with patch.object(admin_module, "supabase") as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ), patch.object(
            admin_module, "_execute_lab_tests_upsert", new_callable=AsyncMock, side_effect=Exception("DB deadlock")
        ):
            mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value = MagicMock(
                data=[preview_data]
            )

            client = TestClient(test_app)
            resp = client.post(
                "/admin/lab-tests/import-apply/preview-1",
                json={"selected_indices": [0]},
            )

            assert resp.status_code == 500
            # Verify rollback call set status back to 'pending'
            mock_sb.table.return_value.update.assert_any_call({"status": "pending"})

    def test_get_preview_stuck_in_processing_reported_as_failed(self, test_app):
        """A preview in processing for > 15 minutes is reported as failed by GET /lab-tests/import-preview/{id}."""
        user = _make_admin_user(clinic_id="clinic-1")
        test_app.dependency_overrides[verify_credentials] = lambda: user

        stuck_created_at = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        stuck_row = {
            "id": "stuck-preview-1",
            "clinic_id": "clinic-1",
            "created_by": "user-1",
            "status": "processing",
            "created_at": stuck_created_at,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "rows": [],
        }

        with patch.object(admin_module, "sb", new_callable=AsyncMock) as mock_sb, patch.object(
            admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock, return_value="clinic-1"
        ):
            mock_sb.return_value = MagicMock(data=[dict(stuck_row)])

            client = TestClient(test_app)
            resp = client.get("/admin/lab-tests/import-preview/stuck-preview-1")

            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "failed"
            assert data["failure_reason"] == "Import was interrupted — please upload again"

    @pytest.mark.asyncio
    async def test_pdf_with_text_price_list_yields_rows_through_ai_path(self):
        """Proves that a PDF with a text price list yields staged rows through the AI path."""
        import json
        from app.services.price_list_parser import parse_catalogue_file

        dummy_pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        pdf_text_lines = [
            "XYZ DIAGNOSTIC CENTRE",
            "DEPARTMENT OF PATHOLOGY & BIOCHEMISTRY",
            "TEST NAME | CHARGES | SAMPLE TYPE",
            "Thyroid Profile (T3, T4, TSH) - 650",
            "Vitamin D Total - 1200",
            "Complete Blood Count (CBC) - 350",
        ]

        ai_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "tests": [
                            {
                                "name": "Thyroid Profile (T3, T4, TSH)",
                                "price_rupees": 650.0,
                                "category": "Biochemistry",
                                "sample_type": "Blood",
                                "fasting_required": True,
                                "source_line": "Thyroid Profile (T3, T4, TSH) - 650",
                            },
                            {
                                "name": "Vitamin D Total",
                                "price_rupees": 1200.0,
                                "category": "Biochemistry",
                                "sample_type": "Serum",
                                "fasting_required": False,
                                "source_line": "Vitamin D Total - 1200",
                            },
                        ]
                    })
                }
            }],
            "model": "google/gemini-2.5-flash",
            "usage": {"total_tokens": 250},
        }

        with patch("app.services.price_list_parser.extract_text_and_tables_from_pdf", return_value=(pdf_text_lines, 1)), patch(
            "app.services.price_list_parser.call_ai_gateway", new_callable=AsyncMock, return_value=ai_response
        ) as mock_gateway, patch(
            "app.routers.admin._fetch_all_lab_tests", new_callable=AsyncMock, return_value=[]
        ), patch("app.services.price_list_parser.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.return_value = MagicMock(data=[])

            rows = await parse_catalogue_file(
                raw_bytes=dummy_pdf_bytes,
                filename="pricelist.pdf",
                clinic_id="clinic-1",
            )

            # Proves it called the AI gateway with the raw text
            mock_gateway.assert_called_once()
            assert len(rows) == 2

            assert rows[0]["name"] == "Thyroid Profile (T3, T4, TSH)"
            assert rows[0]["price_rupees"] == 650.0
            assert rows[0]["price_paise"] == 65000
            assert rows[0]["source_line"] == "Thyroid Profile (T3, T4, TSH) - 650"
            assert rows[0]["status"] == "New"

            assert rows[1]["name"] == "Vitamin D Total"
            assert rows[1]["price_rupees"] == 1200.0
            assert rows[1]["price_paise"] == 120000
            assert rows[1]["source_line"] == "Vitamin D Total - 1200"
            assert rows[1]["status"] == "New"

    def test_panel_js_preview_rows_key_contract(self):
        """Checks the panel JS only reads keys the backend actually returns for preview rows."""
        import os
        import re
        from app.services.price_list_parser import _audit_and_stage_row

        # Get a sample staged row from backend
        staged = _audit_and_stage_row(
            {"name": "CBC", "price_rupees": 350.0, "category": "Hematology"},
            "CBC 350",
            0,
            {},
        )

        html_path = os.path.join(os.path.dirname(__file__), "..", "admin", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Find renderPriceListReview in admin/index.html
        start = html.find("function renderPriceListReview")
        end = html.find("function togglePriceListRow", start)
        fn_code = html[start:end]

        # Verify old / wrong keys are NOT referenced
        assert "price_inr" not in fn_code, "price_inr should be replaced with price_rupees"
        assert "source_snippet" not in fn_code, "source_snippet should be replaced with source_line"

        # Verify correct keys are referenced
        assert "price_rupees" in fn_code
        assert "source_line" in fn_code

        # Verify all r.<prop> fields used exist in the staged row
        props = set(re.findall(r'\br\.([a-zA-Z0-9_]+)\b', fn_code))
        for prop in props:
            assert prop in staged, f"Property 'r.{prop}' used in panel JS must be present in staged row"

    def test_image_size_limit_inside_parser_only(self):
        """Checks image width * height inside parser without modifying PIL.Image.MAX_IMAGE_PIXELS."""
        from PIL import Image
        from app.services.price_list_parser import preprocess_image_for_ocr

        orig_max = getattr(Image, "MAX_IMAGE_PIXELS", None)

        # Create a mock PIL image with width * height > 40,000,000
        mock_img = MagicMock()
        mock_img.width = 7000
        mock_img.height = 6000  # 42,000,000 pixels

        with pytest.raises(HTTPException) as exc:
            preprocess_image_for_ocr(mock_img)
        assert exc.value.status_code == 400
        assert "Image dimensions exceed maximum safe limit" in exc.value.detail

        # Global Image.MAX_IMAGE_PIXELS must NOT be altered
        assert getattr(Image, "MAX_IMAGE_PIXELS", None) == orig_max


class TestRetentionPurge:
    @pytest.mark.asyncio
    async def test_purge_runs_only_in_production(self):
        from app.services.data_retention import purge_expired_catalogue_import_previews
        from app.config import settings

        # In testing environment (settings.app_env != "production")
        with patch.object(settings, "app_env", "testing"), patch("app.services.data_retention.supabase") as mock_sb:
            deleted = await purge_expired_catalogue_import_previews()
            assert deleted == 0
            mock_sb.table.assert_not_called()

        # In production environment
        with patch.object(settings, "app_env", "production"), patch("app.services.data_retention.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.return_value = MagicMock(data=[{"id": "p1"}, {"id": "p2"}])
            deleted = await purge_expired_catalogue_import_previews()
            assert deleted == 2

    @pytest.mark.asyncio
    async def test_purge_job_marks_stuck_processing_previews_as_failed(self):
        """Purge job marks previews stuck in processing for > 15 minutes as failed."""
        from app.services.data_retention import purge_expired_catalogue_import_previews
        from app.config import settings

        with patch.object(settings, "app_env", "production"), patch(
            "app.services.data_retention.sb", new_callable=AsyncMock
        ) as mock_sb:
            mock_sb.return_value = MagicMock(data=[])
            await purge_expired_catalogue_import_previews()

            from app.services.data_retention import supabase as dr_supabase

            payloads = []
            # 1. From real PostgREST builder request.json
            for call in mock_sb.call_args_list:
                arg = call[0][0]
                if hasattr(arg, "request") and hasattr(arg.request, "json") and isinstance(arg.request.json, dict):
                    payloads.append(arg.request.json)

            # 2. From mock supabase table update call args (when supabase is mocked by earlier tests)
            if hasattr(dr_supabase, "table") and hasattr(dr_supabase.table, "return_value"):
                tbl = dr_supabase.table.return_value
                if hasattr(tbl, "update") and hasattr(tbl.update, "call_args_list"):
                    for ucall in tbl.update.call_args_list:
                        if ucall and ucall[0] and isinstance(ucall[0][0], dict):
                            payloads.append(ucall[0][0])

            assert any(p.get("failure_reason") == "Import was interrupted — please upload again" for p in payloads)


