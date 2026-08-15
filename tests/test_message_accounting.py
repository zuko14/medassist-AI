"""Tests for the message_accounting service module.

Tests resolve_category, billing period calculation, cache invalidation,
and the data contract of get_clinic_usage / get_platform_usage.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

# ── resolve_category tests ──────────────────────────────────────────────────

from app.services.message_accounting import resolve_category


class TestResolveCategory:
    """Test that resolve_category maps message types to correct billing categories."""

    def test_freeform_text_is_utility(self):
        assert resolve_category("text") == "utility"

    def test_freeform_interactive_is_utility(self):
        assert resolve_category("interactive_buttons") == "utility"

    def test_freeform_document_is_utility(self):
        assert resolve_category("document") == "utility"

    def test_freeform_location_is_utility(self):
        assert resolve_category("location") == "utility"

    def test_template_reminder_is_utility(self):
        assert resolve_category("template", "appointment_reminder_24h") == "utility"

    def test_template_confirmation_is_utility(self):
        assert resolve_category("template", "appointment_confirmation") == "utility"

    def test_template_followup_is_marketing(self):
        assert resolve_category("template", "followup_message") == "marketing"

    def test_template_promo_is_marketing(self):
        assert resolve_category("template", "promo_summer_offer") == "marketing"

    def test_template_campaign_is_marketing(self):
        assert resolve_category("template", "campaign_diwali_checkup") == "marketing"

    def test_template_re_engage_is_marketing(self):
        assert resolve_category("template", "re_engage_inactive_patients") == "marketing"

    def test_template_otp_is_authentication(self):
        assert resolve_category("template", "otp_verification") == "authentication"

    def test_template_auth_is_authentication(self):
        assert resolve_category("template", "auth_login_code") == "authentication"

    def test_template_verify_is_authentication(self):
        assert resolve_category("template", "verify_phone") == "authentication"

    def test_mark_read_is_service(self):
        assert resolve_category("mark_read") == "service"

    def test_unknown_template_is_utility(self):
        assert resolve_category("template", "some_unknown_template") == "utility"

    def test_case_insensitive_prefix_matching(self):
        assert resolve_category("template", "Followup_Test") == "marketing"
        assert resolve_category("template", "PROMO_test") == "marketing"


# ── Billing period tests ────────────────────────────────────────────────────

from app.services.message_accounting import _billing_period


class TestBillingPeriod:
    """Test calendar month billing period calculation."""

    def test_mid_month_returns_month_boundaries(self):
        ref = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _billing_period(ref)
        assert start.startswith("2026-08-01")
        assert end.startswith("2026-09-01")

    def test_first_day_returns_same_month(self):
        ref = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        start, end = _billing_period(ref)
        assert start.startswith("2026-01-01")
        assert end.startswith("2026-02-01")

    def test_december_wraps_to_next_year(self):
        ref = datetime(2026, 12, 25, 18, 30, 0, tzinfo=timezone.utc)
        start, end = _billing_period(ref)
        assert start.startswith("2026-12-01")
        assert end.startswith("2027-01-01")

    def test_last_day_of_month(self):
        ref = datetime(2026, 2, 28, 23, 59, 59, tzinfo=timezone.utc)
        start, end = _billing_period(ref)
        assert start.startswith("2026-02-01")
        assert end.startswith("2026-03-01")


# ── Cache invalidation tests ────────────────────────────────────────────────

from app.services.message_accounting import (
    invalidate_pricing_cache,
    invalidate_plan_tiers_cache,
)


class TestCacheInvalidation:
    """Test that cache invalidation resets the cached state."""

    def test_invalidate_pricing_cache(self):
        import app.services.message_accounting as mod
        mod._pricing_cache = {"utility_paise": 12}
        mod._pricing_cache_at = 99999.0
        invalidate_pricing_cache()
        assert mod._pricing_cache is None
        assert mod._pricing_cache_at == 0.0

    def test_invalidate_plan_tiers_cache(self):
        import app.services.message_accounting as mod
        mod._plan_tiers_cache = {"essential": {"included_messages_month": 2500}}
        mod._plan_tiers_cache_at = 99999.0
        invalidate_plan_tiers_cache()
        assert mod._plan_tiers_cache is None
        assert mod._plan_tiers_cache_at == 0.0


# ── get_clinic_usage data contract tests ─────────────────────────────────────

class TestGetClinicUsageContract:
    """Verify get_clinic_usage returns customer-safe fields only."""

    FORBIDDEN_FIELDS = {
        "cost", "price", "paise", "inr", "meta", "markup", "margin",
        "estimated_cost", "pricing", "rate",
    }

    @pytest.mark.asyncio
    async def test_no_financial_fields_in_response(self):
        """SECURITY: The customer-facing usage response must NEVER contain
        any field that could reveal costs, pricing, or Meta financials."""
        from app.services.message_accounting import get_clinic_usage

        with patch("app.database.supabase") as mock_sb:
            # Mock the ledger query
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.neq.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.lt.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[
                {"category": "utility", "sent_at": "2026-08-10T10:00:00Z", "send_success": True},
                {"category": "marketing", "sent_at": "2026-08-11T10:00:00Z", "send_success": True},
            ])

            # Mock plan tiers
            with patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock) as mock_tiers:
                mock_tiers.return_value = {
                    "essential": {"included_messages_month": 2500, "display_name": "Essential"},
                }
                result = await get_clinic_usage("test-clinic-id", "essential")

        # Verify NO financial fields leaked
        all_keys = set()
        def collect_keys(d, prefix=""):
            for k, v in d.items():
                full_key = f"{prefix}.{k}" if prefix else k
                all_keys.add(k.lower())
                if isinstance(v, dict):
                    collect_keys(v, full_key)

        collect_keys(result)

        for forbidden in self.FORBIDDEN_FIELDS:
            for key in all_keys:
                assert forbidden not in key, (
                    f"SECURITY VIOLATION: Field containing '{forbidden}' found in "
                    f"customer-facing response: '{key}'. This field MUST NOT be "
                    f"returned to clinic admins."
                )

    @pytest.mark.asyncio
    async def test_required_volumetric_fields_present(self):
        """Verify all expected volumetric fields are returned."""
        from app.services.message_accounting import get_clinic_usage

        with patch("app.database.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value = mock_table
            mock_table.eq.return_value = mock_table
            mock_table.neq.return_value = mock_table
            mock_table.gte.return_value = mock_table
            mock_table.lt.return_value = mock_table
            mock_table.execute.return_value = MagicMock(data=[])

            with patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock) as mock_tiers:
                mock_tiers.return_value = {
                    "essential": {"included_messages_month": 2500, "display_name": "Essential"},
                }
                result = await get_clinic_usage("test-clinic-id", "essential")

        required = {
            "plan", "plan_display_name", "period_start", "period_end",
            "included_messages", "is_unlimited", "messages_sent",
            "messages_remaining", "overage_count", "usage_percent",
            "daily_breakdown", "by_category",
        }
        assert required.issubset(set(result.keys())), (
            f"Missing fields: {required - set(result.keys())}"
        )
