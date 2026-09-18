"""Unit tests for AI Gateway and spend ledger (Feature 7)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.ai_gateway import (
    calculate_cost_paise,
    check_admin_spend_cap,
    record_ai_usage,
    call_ai_gateway,
    SpendCapExceededError,
    _clean_clinic_uuid,
)


class TestAIGateway:
    def test_clean_clinic_uuid(self):
        valid = "11111111-1111-1111-1111-111111111111"
        assert _clean_clinic_uuid(valid) == valid
        assert _clean_clinic_uuid("default") is None
        assert _clean_clinic_uuid(None) is None
        assert _clean_clinic_uuid("invalid-string") is None

    def test_calculate_cost_paise_from_usd(self):
        # $0.001 at 87.0 INR/USD = 0.087 INR = 8.7 paise -> 9 paise
        usage = {"total_cost": 0.001}
        paise = calculate_cost_paise(usage, total_tokens=1000, usd_to_inr_rate=87.0)
        assert paise == 9

    def test_calculate_cost_paise_estimate_from_tokens(self):
        # 100,000 tokens at $0.20/1M = $0.02 * 87 * 100 paise = 174 paise
        usage = {}
        paise = calculate_cost_paise(usage, total_tokens=100_000, usd_to_inr_rate=87.0)
        assert paise == 174

    @pytest.mark.asyncio
    async def test_check_admin_spend_cap_under_budget(self):
        clinic_id = "11111111-1111-1111-1111-111111111111"
        fake_clinic_res = MagicMock(data=[{"config": {"ai_budget_paise": 50000}}])
        fake_ledger_res = MagicMock(data=[{"cost_paise": 1200}, {"cost_paise": 800}])

        with patch("app.database.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.side_effect = [fake_clinic_res, fake_ledger_res]
            exceeded, spend, budget = await check_admin_spend_cap(clinic_id)
            assert exceeded is False
            assert spend == 2000
            assert budget == 50000

    @pytest.mark.asyncio
    async def test_check_admin_spend_cap_exceeded(self):
        clinic_id = "11111111-1111-1111-1111-111111111111"
        fake_clinic_res = MagicMock(data=[{"config": {"ai_budget_paise": 50000}}])
        fake_ledger_res = MagicMock(data=[{"cost_paise": 30000}, {"cost_paise": 25000}])

        with patch("app.database.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.side_effect = [fake_clinic_res, fake_ledger_res]
            exceeded, spend, budget = await check_admin_spend_cap(clinic_id)
            assert exceeded is True
            assert spend == 55000
            assert budget == 50000

    @pytest.mark.asyncio
    async def test_call_ai_gateway_blocks_admin_on_budget_exceeded(self):
        clinic_id = "11111111-1111-1111-1111-111111111111"
        with patch("app.services.ai_gateway.check_admin_spend_cap", new_callable=AsyncMock) as mock_cap:
            mock_cap.return_value = (True, 60000, 50000)
            with pytest.raises(SpendCapExceededError):
                await call_ai_gateway(
                    messages=[{"role": "user", "content": "hello"}],
                    task_type="test_details",
                    clinic_id=clinic_id,
                )

    @pytest.mark.asyncio
    async def test_record_ai_usage_never_raises(self):
        with patch("app.database.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.side_effect = Exception("DB Connection Error")
            # Should not raise
            await record_ai_usage(
                clinic_id="11111111-1111-1111-1111-111111111111",
                task_type="test_details",
                provider="openrouter",
                model="deepseek/deepseek-chat",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cost_paise=10,
            )

    @pytest.mark.asyncio
    async def test_check_admin_spend_cap_counts_all_2500_rows(self):
        """With 2,500 ledger rows across multiple 1,000-row pages, the cap counts all of them."""
        clinic_id = "11111111-1111-1111-1111-111111111111"
        fake_clinic_res = MagicMock(data=[{"config": {"ai_budget_paise": 50000}}])

        page1 = MagicMock(data=[{"cost_paise": 10} for _ in range(1000)])
        page2 = MagicMock(data=[{"cost_paise": 10} for _ in range(1000)])
        page3 = MagicMock(data=[{"cost_paise": 10} for _ in range(500)])

        with patch("app.database.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.side_effect = [fake_clinic_res, page1, page2, page3]
            exceeded, spend, budget = await check_admin_spend_cap(clinic_id)
            assert spend == 25000
            assert exceeded is False
            assert budget == 50000
            assert mock_sb.call_count == 4

    @pytest.mark.asyncio
    async def test_record_ai_usage_truncates_error_message_to_300_chars(self):
        long_err = "E" * 500
        inserted_payloads = []

        class FakeTable:
            def insert(self, payload):
                inserted_payloads.append(payload)
                return self

        with patch("app.database.supabase.table", return_value=FakeTable()), \
             patch("app.database.sb", new_callable=AsyncMock) as mock_sb:
            mock_sb.return_value = MagicMock(data=[{"id": "test"}])
            await record_ai_usage(
                clinic_id="11111111-1111-1111-1111-111111111111",
                task_type="test_details",
                provider="openrouter",
                model="deepseek/deepseek-chat",
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                cost_paise=1,
                success=False,
                error_message=long_err,
            )
            assert len(inserted_payloads) == 1
            err = inserted_payloads[0].get("error_message")
            assert len(err) == 300
            assert err == "E" * 300
