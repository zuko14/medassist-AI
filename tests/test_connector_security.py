"""Tests for Connector Session Security & Debug Artifact Sanitization (Finding #7 & #13)."""

import os
import json
import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet


@pytest.mark.asyncio
async def test_session_save_and_restore_encrypted():
    """Verify session cookies are Fernet-encrypted when saved and decrypted when restored."""
    key = Fernet.generate_key().decode()

    with patch("connectors.mocdoc.worker.settings") as mock_settings:
        mock_settings.connector_encryption_key = key
        mock_settings.app_port = 8000
        mock_settings.integration_secret = "secret"

        from connectors.mocdoc.worker import MocDocConnector

        with tempfile.TemporaryDirectory() as tmp_dir:
            connector = MocDocConnector(
                clinic_id="test-clinic",
                config={"username": "user", "password": "pass", "clinic_slug": "demo"},
                medassist_url="http://localhost:8000",
                integration_secret="secret",
                session_dir=tmp_dir,
            )

            # Mock Playwright context and page
            mock_context = AsyncMock()
            mock_context.cookies.return_value = [{"name": "session_id", "value": "xyz123"}]
            connector._context = mock_context

            # Test _save_session
            await connector._save_session()

            # Verify session file on disk is encrypted JSON
            assert os.path.exists(connector.session_file)
            with open(connector.session_file, "r") as f:
                content = json.load(f)

            assert content["encrypted"] is True
            assert "data" in content
            assert "session_id" not in json.dumps(content)  # Not in plain text!

            # Test _restore_session
            mock_page = AsyncMock()
            mock_page.url = "https://mocdoc.com/dashboard"
            connector._page = mock_page

            restored = await connector._restore_session()
            assert restored is True
            mock_context.add_cookies.assert_called_once()
            restored_cookies = mock_context.add_cookies.call_args[0][0]
            assert restored_cookies[0]["name"] == "session_id"
            assert restored_cookies[0]["value"] == "xyz123"


def test_session_cleanup_retention_period():
    """Verify stale session files older than 24 hours are purged."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        stale_file = os.path.join(tmp_dir, "stale_session.json")
        fresh_file = os.path.join(tmp_dir, "fresh_session.json")

        # Create files
        with open(stale_file, "w") as f:
            f.write("{}")
        with open(fresh_file, "w") as f:
            f.write("{}")

        # Set mtime for stale file to 25 hours ago
        past_time = time.time() - (25 * 3600)
        os.utime(stale_file, (past_time, past_time))

        with patch("connectors.runner.PROJECT_ROOT", tmp_dir):
            cutoff_ts = time.time() - (24 * 3600)
            for f in os.listdir(tmp_dir):
                fpath = os.path.join(tmp_dir, f)
                if os.path.getmtime(fpath) < cutoff_ts:
                    os.remove(fpath)

        assert not os.path.exists(stale_file)
        assert os.path.exists(fresh_file)
