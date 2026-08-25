"""Phase C: Real Report Validation in Laboratory Connector.

Verifies:
1. validate_report rejects empty or non-PDF bytes.
2. validate_report rejects missing expected_patient metadata.
3. validate_report rejects report text with conflicting patient name / phone.
4. validate_report accepts report text matching patient name / phone.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import io
import pytest
from unittest.mock import patch, MagicMock

from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.api.schemas import PatientIdentity
from app.integrations.callmedex.api.exceptions import ValidationError
from app.utils.pdf_reader import PDFValidationError


@pytest.fixture
def connector():
    c = MocDocConnector()
    c.configure_center(
        base_url="https://mock.mocdoc.com",
        clinic_slug="test-clinic",
    )
    return c


@pytest.mark.asyncio
async def test_validate_report_rejects_empty_bytes(connector):
    patient = PatientIdentity(patient_name="John Doe", patient_phone="+919876543210")
    with pytest.raises(ValidationError, match="file is empty"):
        await connector.validate_report(b"", patient)


@pytest.mark.asyncio
async def test_validate_report_rejects_invalid_magic_header(connector):
    patient = PatientIdentity(patient_name="John Doe", patient_phone="+919876543210")
    with pytest.raises(ValidationError, match="magic header missing"):
        await connector.validate_report(b"NOT_A_PDF_FILE", patient)


@pytest.mark.asyncio
async def test_validate_report_rejects_missing_patient_identity(connector):
    fake_pdf = b"%PDF-1.4 Fake PDF Header"
    with pytest.raises(ValidationError, match="identity missing"):
        await connector.validate_report(fake_pdf, None)


@pytest.mark.asyncio
async def test_validate_report_rejects_conflicting_patient_name(connector):
    """P1-1: validate_report fails closed when report text belongs to a different patient."""
    patient = PatientIdentity(patient_name="Alice Wonderland", patient_phone="+919876543210")
    fake_pdf = b"%PDF-1.4 Fake PDF\n%%EOF"

    with patch("app.utils.pdf_reader.validate_pdf_report", side_effect=PDFValidationError("Report patient header mismatch")):
        with pytest.raises(ValidationError, match="patient header mismatch"):
            await connector.validate_report(fake_pdf, patient)


@pytest.mark.asyncio
async def test_validate_report_accepts_matching_patient_name(connector):
    """P1-1: validate_report succeeds when report text matches expected patient name."""
    patient = PatientIdentity(patient_name="Alice Wonderland", patient_phone="+919876543210")
    fake_pdf = b"%PDF-1.4 Fake PDF\n%%EOF"

    with patch("app.utils.pdf_reader.validate_pdf_report", return_value=True):
        valid = await connector.validate_report(fake_pdf, patient)
        assert valid is True
