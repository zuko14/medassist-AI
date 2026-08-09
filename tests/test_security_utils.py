"""Tests for app/utils/security.py — webhook signature fail-closed behavior (Finding #3)."""

import hashlib
import hmac
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from app.utils.security import verify_webhook_signature


class TestWebhookSignatureFailClosed:
    def test_missing_secret_fails_closed_in_production(self):
        """No META_APP_SECRET + app_env=production -> REJECT, not accept."""
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is False

    def test_missing_secret_fails_closed_by_default_in_development(self):
        """Even in development, missing secret rejects UNLESS the explicit
        opt-in flag is set — no more silent accept-by-default."""
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is False

    def test_missing_secret_allowed_only_with_explicit_dev_opt_in(self):
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.allow_unsigned_webhooks_dev = True
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is True

    def test_valid_signature_still_accepted(self):
        secret = "my_app_secret"
        body = b'{"test": "payload"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(body, sig, secret)
        assert result is True

    def test_invalid_signature_still_rejected(self):
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=bad", "secret")
        assert result is False


class TestPersistentRateLimiterAtomicCheck:
    def test_check_and_record_uses_single_rpc_call(self):
        """The atomic path must be ONE round trip (an .rpc() call), not a
        separate select-then-insert/update — that's the actual fix."""
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=5, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=3)

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            is_limited = limiter.check_and_record("1.2.3.4")

        assert is_limited is False  # 3 attempts < max_attempts=5
        mock_supabase.rpc.assert_called_once_with(
            "check_and_record_rate_limit",
            {"p_key": "1.2.3.4", "p_max_attempts": 5, "p_window_seconds": 60},
        )

    def test_check_and_record_returns_true_when_limit_exceeded(self):
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=5, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=6)

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            is_limited = limiter.check_and_record("1.2.3.4")

        assert is_limited is True

    def test_check_and_record_falls_back_to_in_memory_on_rpc_error(self):
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=2, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.side_effect = Exception("rpc not found")

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            assert limiter.check_and_record("1.2.3.4") is False
            assert limiter.check_and_record("1.2.3.4") is False
            assert limiter.check_and_record("1.2.3.4") is True  # 3rd attempt, max=2
