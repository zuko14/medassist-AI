"""Universal Laboratory Connector Compliance Test Suite (Phase 9 Governance).

Every current and future laboratory connector (MocDoc, Crelio, CloudLIMS, etc.)
MUST pass this universal compliance suite to be accepted into production.
"""

import pytest
from typing import Type
from app.integrations.callmedex.connectors.base.connector import BaseLaboratoryConnector, JobCheckpoint
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.api.schemas import PatientIdentity
from app.integrations.callmedex.config.settings import callmedex_settings


# Parameterized list of all registered laboratory connectors
REGISTERED_CONNECTORS = [
    MocDocConnector,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_cls", REGISTERED_CONNECTORS)
async def test_connector_compliance_lifecycle_methods(connector_cls: Type[BaseLaboratoryConnector]):
    """Verify connector implements and passes all required compliance lifecycle methods."""
    connector = connector_cls()

    # 1. Health check capability compliance
    health = await connector.health_check()
    assert isinstance(health, dict)
    assert health.get("status") == "healthy"
    assert connector.capabilities.supports_barcode_search is True

    # 2. Login compliance
    login_ok = await connector.login({"username": "compliance_user", "password": "compliance_pass"})
    assert login_ok is True
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # 3. Search by barcode compliance
    barcode = "COMPLIANCE-BARCODE-001"
    metadata = await connector.search_by_barcode(barcode)
    assert metadata is not None
    assert metadata.report_id == barcode
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED

    # 4. Download report compliance
    pdf_bytes = await connector.download_report(barcode, callmedex_settings.download_dir)
    assert pdf_bytes is not None
    assert len(pdf_bytes) > 0
    assert connector.current_checkpoint == JobCheckpoint.PDF_DOWNLOADED

    # 5. Validate report compliance
    patient = PatientIdentity(patient_phone="+919966773300", patient_name="Compliance Patient")
    valid_ok = await connector.validate_report(pdf_bytes, patient)
    assert valid_ok is True
    assert connector.current_checkpoint == JobCheckpoint.VALIDATED

    # 6. Logout & resource disposal compliance
    logout_ok = await connector.logout()
    assert logout_ok is True


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_cls", REGISTERED_CONNECTORS)
async def test_connector_compliance_checkpoint_resume(connector_cls: Type[BaseLaboratoryConnector]):
    """Verify connector checkpoint recovery and resumption compliance."""
    connector = connector_cls()
    assert connector.current_checkpoint == JobCheckpoint.CREATED

    # Advance to AUTHENTICATED
    await connector.login({"username": "compliance_user", "password": "compliance_pass"})
    assert connector.current_checkpoint == JobCheckpoint.AUTHENTICATED

    # Resume directly from AUTHENTICATED checkpoint
    metadata = await connector.search_by_barcode("RESUME-BARCODE-002")
    assert metadata is not None
    assert connector.current_checkpoint == JobCheckpoint.REPORT_LOCATED
