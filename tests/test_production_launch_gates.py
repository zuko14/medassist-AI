"""Production Launch Gates Validation Suite (Gates 1 to 8).

Verifies:
1. Process Isolation (render.yaml RUN_CONNECTORS_IN_WEB="false" & worker config).
2. Distributed Lock on connector polling sweeps across multi-worker Uvicorn.
3. Multi-Tenant Patient Data Isolation (fail-closed get_genuine_patients & admin API).
4. PHI Walk-in Safety gating (diagnostic vs consultation clinic behavior).
5. Database Hardening & Migration 071 schema validation.
6. Pre-flight Production Secret & Security Controls.
"""

import os
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.database import (
    get_genuine_patients,
    create_patient,
    update_patient,
    TenantIsolationError,
)
from app.tenancy import is_valid_clinic_scope, TENANT_OWNED_TABLES
from app.services.patient_match import PatientMatchService
from app.config import settings


# ─── GATE 1: PROCESS ISOLATION & DISTRIBUTED LOCKING ──────────────────────────

def test_gate1_render_yaml_process_isolation():
    """Verify render.yaml isolates Playwright from web service and configures worker."""
    yaml_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "render.yaml")
    assert os.path.exists(yaml_path), "render.yaml must exist"

    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Web service must have RUN_CONNECTORS_IN_WEB set to "false"
    assert re.search(r'key:\s*RUN_CONNECTORS_IN_WEB\s*\n\s*value:\s*"false"', content), (
        "render.yaml web service must set RUN_CONNECTORS_IN_WEB to 'false' to keep Chromium out of web container"
    )

    # Worker service must exist and declare MEDASSIST_URL
    assert "mediassist-connector-worker" in content, (
        "mediassist-connector-worker must be declared in render.yaml"
    )
    assert "MEDASSIST_URL" in content, (
        "mediassist-connector-worker must declare MEDASSIST_URL to communicate with web container"
    )


@pytest.mark.asyncio
async def test_gate1_connector_polling_uses_distributed_lock():
    """Verify run_all_connectors uses distributed_job_lock to prevent concurrent worker sweeps."""
    from connectors.runner import run_all_connectors

    # Test lock acquisition skip
    mock_lock = MagicMock()
    mock_lock.__aenter__ = AsyncMock(return_value=False)  # Lock already held by another worker
    mock_lock.__aexit__ = AsyncMock(return_value=None)

    with patch("connectors.runner.distributed_job_lock", return_value=mock_lock), \
         patch("connectors.runner.sb") as mock_sb:
        await run_all_connectors()
        # If lock was not acquired, no database queries should be executed
        mock_sb.assert_not_called()


# ─── GATE 2 & 3: MULTI-TENANT HARDENING & PATIENT DATA ISOLATION ─────────────

@pytest.mark.asyncio
async def test_gate3_get_genuine_patients_rejects_invalid_scope():
    """Verify get_genuine_patients fails closed on empty or sentinel clinic scope."""
    for bad_scope in ("", None, "default", "   ", "all", "system"):
        with pytest.raises(TenantIsolationError) as exc_info:
            await get_genuine_patients(bad_scope)
        assert "Refusing get_genuine_patients on invalid clinic_id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate3_create_patient_rejects_invalid_scope():
    """Verify create_patient fails closed on empty or sentinel clinic scope."""
    for bad_scope in ("", None, "default", "null"):
        with pytest.raises(TenantIsolationError) as exc_info:
            await create_patient(bad_scope, "+919876543210", "Test Patient")
        assert "Refusing create_patient on invalid clinic_id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate3_update_patient_rejects_invalid_scope():
    """Verify update_patient fails closed on empty or sentinel clinic scope."""
    for bad_scope in ("", None, "default"):
        with pytest.raises(TenantIsolationError) as exc_info:
            await update_patient(bad_scope, "+919876543210", {"name": "Hacked"})
        assert "Refusing update_patient on invalid clinic_id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate3_admin_patients_endpoint_cross_tenant_rejection():
    """Verify that a clinic admin for Hospital A cannot access Hospital B's patient records."""
    from app.routers.admin import enforce_clinic_access, AdminUser
    from fastapi import HTTPException

    user_a = AdminUser(
        username="hospital_a_admin",
        role="clinic_admin",
        clinic_id="c1111111-1111-1111-1111-111111111111",
        branch_id=None,
    )

    # Attempt to request Hospital B's patient data
    hospital_b_id = "c2222222-2222-2222-2222-222222222222"
    with pytest.raises(HTTPException) as exc_info:
        enforce_clinic_access(user_a, hospital_b_id)

    assert exc_info.value.status_code == 403
    assert "restricted" in exc_info.value.detail.lower() or "different clinic" in exc_info.value.detail.lower()


# ─── GATE 4: PHI WALK-IN SAFETY GATING ────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate4_diagnostic_walkin_delivers_with_unverified_flag():
    """Diagnostic centers rely on walk-ins whose phones are not pre-registered;
    they deliver with recipient_unverified=True and admin alert."""
    service = PatientMatchService(similarity_threshold=0.75)

    async def _diagnostic_clinic(_cid):
        return {
            "id": _cid,
            "clinic_type": "diagnostic",
            "config": {"clinic_type": "diagnostic"},
        }

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    with patch("app.services.tenant.get_clinic_by_id", new=_diagnostic_clinic), \
         patch("app.services.patient_match.supabase", mock_sb):
        result = await service.match(
            clinic_id="c-diagnostic",
            scraped_name="Mr. Suresh Rao",
            scraped_phone="+919876543210",
        )

    assert result.status == "matched"
    assert result.is_safe_to_send is True
    assert result.recipient_unverified is True
    assert result.match_source == "moc_doc_only"


@pytest.mark.asyncio
async def test_gate4_consultation_hospital_holds_unknown_numbers_for_phi_safety():
    """Consultation clinics and general hospitals pre-register patients during booking;
    an unknown phone number is a high risk of PHI misrouting and must be held in needs_review."""
    service = PatientMatchService(similarity_threshold=0.75)

    async def _hospital_clinic(_cid):
        return {
            "id": _cid,
            "clinic_type": "hospital",
            "config": {"clinic_type": "hospital"},
        }

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    with patch("app.services.tenant.get_clinic_by_id", new=_hospital_clinic), \
         patch("app.services.patient_match.supabase", mock_sb):
        result = await service.match(
            clinic_id="c-hospital",
            scraped_name="Mrs. Geeta Devi",
            scraped_phone="+919876543210",
        )

    assert result.status == "needs_review"
    assert result.is_safe_to_send is False
    assert result.match_confidence == 0.0
    assert "not registered with this clinic" in (result.review_reason or "")


# ─── GATE 5: PRE-FLIGHT SECRET VALIDATION & PRODUCTION BOOT ───────────────────

@pytest.mark.asyncio
async def test_gate5_production_boot_rejects_placeholder_secrets(monkeypatch):
    """Verify application startup raises RuntimeError if app_env='production' and secrets are default/placeholder."""
    from app.main import lifespan, app

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "meta_app_secret", "change_me_in_production")
    monkeypatch.setattr(settings, "admin_password", "admin123")

    with pytest.raises(RuntimeError) as exc_info:
        async with lifespan(app):
            pass

    assert "Refusing to boot in production mode" in str(exc_info.value)
    assert "META_APP_SECRET" in str(exc_info.value)


# ─── GATE 6: MIGRATION 071 VERIFICATION ───────────────────────────────────────

def test_gate6_migration_071_exists_and_enforces_rls():
    """Verify migration 071 exists and contains FORCE ROW LEVEL SECURITY and unique index."""
    mig_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "migrations",
        "071_production_readiness_hardening.sql",
    )
    assert os.path.exists(mig_path), "071_production_readiness_hardening.sql must exist"

    with open(mig_path, "r", encoding="utf-8") as f:
        sql = f.read()

    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "integration_processed_reports" in sql
    assert "clinic_daily_usage" in sql
    assert "uq_clinic_daily_usage_clinic_date" in sql
