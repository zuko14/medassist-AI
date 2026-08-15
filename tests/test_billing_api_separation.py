"""Security contract tests for billing API separation.

These tests verify the CRITICAL SECURITY INVARIANT that the clinic-facing
messaging-usage API NEVER returns financial fields (costs, pricing, rates),
while the platform-owner API DOES return them.

This file should be run as part of CI/CD and before any deployment.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ═══════ SECURITY: API Response Separation Tests ═══════


class TestBillingApiSeparation:
    """Verify the hard boundary between clinic-facing and owner-facing APIs."""

    # Fields that MUST NEVER appear in clinic-facing responses
    FINANCIAL_FIELD_PATTERNS = [
        "cost", "price", "paise", "inr", "meta", "markup", "margin",
        "rate", "billing", "revenue", "profit",
    ]

    # Fields that MUST appear in owner-facing responses
    OWNER_REQUIRED_FIELDS = [
        "total_estimated_cost_inr",
        "pricing",
        "clinics",
    ]

    @pytest.mark.asyncio
    async def test_clinic_api_excludes_all_financial_fields(self):
        """CRITICAL SECURITY TEST: get_clinic_usage must return ZERO financial fields.

        If this test fails, it means a code change has leaked internal cost data
        into a customer-visible API endpoint. This is a P0 security violation.
        """
        from app.services.message_accounting import get_clinic_usage

        with patch("app.database.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.neq.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.lt.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[
                {"category": "utility", "sent_at": "2026-08-10T10:00:00Z", "send_success": True},
                {"category": "utility", "sent_at": "2026-08-10T10:05:00Z", "send_success": True},
                {"category": "marketing", "sent_at": "2026-08-11T10:00:00Z", "send_success": True},
            ])

            with patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock) as mock_tiers:
                mock_tiers.return_value = {
                    "essential": {"included_messages_month": 2500, "display_name": "Essential"},
                }
                result = await get_clinic_usage("test-clinic-id", "essential")

        # Deep scan ALL keys at all nesting levels
        violations = []
        def scan_keys(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    full_path = f"{path}.{k}" if path else k
                    for pattern in self.FINANCIAL_FIELD_PATTERNS:
                        if pattern in k.lower():
                            violations.append(f"'{full_path}' contains '{pattern}'")
                    scan_keys(v, full_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    scan_keys(item, f"{path}[{i}]")

        scan_keys(result)

        assert not violations, (
            f"P0 SECURITY VIOLATION: Financial fields found in clinic-facing API:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    @pytest.mark.asyncio
    async def test_platform_api_includes_financial_fields(self):
        """Owner-facing API must return full financial breakdown."""
        from app.services.message_accounting import get_platform_usage

        with patch("app.database.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.neq.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.order.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[])

            with patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as mock_pricing:
                mock_pricing.return_value = {
                    "utility_paise": 12,
                    "marketing_paise": 75,
                    "authentication_paise": 10,
                    "service_paise": 0,
                }
                with patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock) as mock_tiers:
                    mock_tiers.return_value = {
                        "essential": {"included_messages_month": 2500, "display_name": "Essential"},
                    }
                    result = await get_platform_usage(days=30)

        for field in self.OWNER_REQUIRED_FIELDS:
            assert field in result, (
                f"Owner-facing API missing required field: '{field}'"
            )

    @pytest.mark.asyncio
    async def test_clinic_vs_platform_field_disjointness(self):
        """The set of financial fields in platform response must NOT appear in clinic response."""
        from app.services.message_accounting import get_clinic_usage, get_platform_usage

        with patch("app.database.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.neq.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.lt.return_value = mock_table
            mock_table.order.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[])

            with patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as mock_pricing:
                mock_pricing.return_value = {
                    "utility_paise": 12, "marketing_paise": 75,
                    "authentication_paise": 10, "service_paise": 0,
                }
                with patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock) as mock_tiers:
                    mock_tiers.return_value = {
                        "essential": {"included_messages_month": 2500, "display_name": "Essential"},
                    }
                    clinic_result = await get_clinic_usage("test-clinic-id", "essential")
                    platform_result = await get_platform_usage(days=30)

        clinic_keys = set(clinic_result.keys())
        financial_only_keys = {"total_estimated_cost_inr", "pricing"}

        leaked = clinic_keys & financial_only_keys
        assert not leaked, (
            f"SECURITY VIOLATION: Financial-only fields found in clinic response: {leaked}"
        )
