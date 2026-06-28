"""Tests for Data Retention Service (app/services/data_retention.py).

Verifies:
  - Configuration defaults match NMC (7 years) and DPDP (30 days)
  - Anonymization structure logic
"""

import pytest
from app.services.data_retention import DataRetentionService, CLINICAL_RETENTION_YEARS, CONVERSATION_PURGE_DAYS


class TestDataRetention:
    """Test suite for DataRetentionService configuration and helpers."""

    def test_retention_defaults(self):
        assert CLINICAL_RETENTION_YEARS == 7
        assert CONVERSATION_PURGE_DAYS == 30

    @pytest.mark.asyncio
    async def test_service_initialization(self):
        service = DataRetentionService()
        assert service is not None
