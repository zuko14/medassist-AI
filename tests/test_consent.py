"""Tests for Consent Management Service (app/services/consent.py).

Verifies:
  - has_consent returns correct bool based on patient record
  - grant_consent calls update_patient with correct payload
  - revoke_consent calls update_patient with correct payload
  - delete_data triggers tiered deletion and returns structured result
  - get_consent_status returns full status dict for existing / missing patients
  - All methods require clinic_id for multi-tenant isolation
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.consent import ConsentService


@pytest.fixture
def consent_svc():
    return ConsentService()


CLINIC_ID = "clinic-test-001"
PHONE = "+919876543210"


class TestConsentHasConsent:
    """Tests for has_consent()."""

    @pytest.mark.asyncio
    async def test_returns_true_when_consented(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {"phone": PHONE, "data_consent": True}
            result = await consent_svc.has_consent(CLINIC_ID, PHONE)
            assert result is True
            mock_get.assert_called_once_with(CLINIC_ID, PHONE)

    @pytest.mark.asyncio
    async def test_returns_false_when_not_consented(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {"phone": PHONE, "data_consent": False}
            result = await consent_svc.has_consent(CLINIC_ID, PHONE)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_patient_not_found(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = None
            result = await consent_svc.has_consent(CLINIC_ID, PHONE)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_field_missing(self, consent_svc):
        """If data_consent key is absent, default to False."""
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {"phone": PHONE}  # no data_consent key
            result = await consent_svc.has_consent(CLINIC_ID, PHONE)
            assert result is False


class TestConsentGrantRevoke:
    """Tests for grant_consent() and revoke_consent()."""

    @pytest.mark.asyncio
    async def test_grant_consent_calls_update(self, consent_svc):
        with patch(
            "app.services.consent.update_patient", new_callable=AsyncMock
        ) as mock_update:
            mock_update.return_value = True
            result = await consent_svc.grant_consent(CLINIC_ID, PHONE)
            assert result is True
            mock_update.assert_called_once_with(
                CLINIC_ID, PHONE, {"data_consent": True, "data_consent_at": "now()"}
            )

    @pytest.mark.asyncio
    async def test_revoke_consent_calls_update(self, consent_svc):
        with patch(
            "app.services.consent.update_patient", new_callable=AsyncMock
        ) as mock_update:
            mock_update.return_value = True
            result = await consent_svc.revoke_consent(CLINIC_ID, PHONE)
            assert result is True
            mock_update.assert_called_once_with(
                CLINIC_ID, PHONE, {"data_consent": False, "data_consent_at": None}
            )

    @pytest.mark.asyncio
    async def test_request_consent_always_returns_true(self, consent_svc):
        result = await consent_svc.request_consent(CLINIC_ID, PHONE)
        assert result is True


class TestConsentDeleteData:
    """Tests for delete_data() — DPDP right to erasure."""

    @pytest.mark.asyncio
    async def test_successful_deletion(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get, patch(
            "app.services.consent.delete_patient_data", new_callable=AsyncMock
        ) as mock_delete:
            mock_get.return_value = {"phone": PHONE, "name": "Test Patient"}
            mock_delete.return_value = True

            result = await consent_svc.delete_data(CLINIC_ID, PHONE)
            assert result["success"] is True
            assert "deletion_ref" in result
            assert len(result["deletion_ref"]) == 8  # UUID[:8]
            assert "deleted_at" in result
            assert "NMC 7-year" in result["note"]

    @pytest.mark.asyncio
    async def test_deletion_patient_not_found(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = None
            result = await consent_svc.delete_data(CLINIC_ID, PHONE)
            assert result["success"] is False
            assert result["error"] == "Patient not found"

    @pytest.mark.asyncio
    async def test_deletion_failure(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get, patch(
            "app.services.consent.delete_patient_data", new_callable=AsyncMock
        ) as mock_delete:
            mock_get.return_value = {"phone": PHONE, "name": "Test"}
            mock_delete.return_value = False
            result = await consent_svc.delete_data(CLINIC_ID, PHONE)
            assert result["success"] is False

    @pytest.mark.asyncio
    async def test_deletion_exception_handled(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("DB down")
            result = await consent_svc.delete_data(CLINIC_ID, PHONE)
            assert result["success"] is False
            assert "DB down" in result["error"]


class TestConsentStatus:
    """Tests for get_consent_status()."""

    @pytest.mark.asyncio
    async def test_status_existing_patient(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = {
                "phone": PHONE,
                "opted_in": True,
                "opted_in_at": "2026-01-01T00:00:00Z",
                "opted_out_at": None,
                "data_consent": True,
                "data_consent_at": "2026-01-01T00:00:00Z",
            }
            status = await consent_svc.get_consent_status(CLINIC_ID, PHONE)
            assert status["exists"] is True
            assert status["opted_in"] is True
            assert status["data_consent"] is True

    @pytest.mark.asyncio
    async def test_status_missing_patient(self, consent_svc):
        with patch(
            "app.services.consent.get_patient_by_phone", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = None
            status = await consent_svc.get_consent_status(CLINIC_ID, PHONE)
            assert status["exists"] is False
            assert status["opted_in"] is False
            assert status["data_consent"] is False


"""Tests for Consent Management Service (app/services/consent.py)."""
