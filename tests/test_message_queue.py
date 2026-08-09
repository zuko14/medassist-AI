"""Tests for app/services/message_queue.py idempotency + fail-open alerting."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")


@pytest.mark.asyncio
async def test_acquire_fail_open_increments_counter():
    """A non-duplicate DB error on acquire() must still fail open (process
    the message — don't drop real patient messages) but now increments a
    counter the scheduler can alert on if this becomes sustained."""
    from app.services.message_queue import MessageQueueManager, get_fail_open_count

    manager = MessageQueueManager()
    before = get_fail_open_count()

    mock_db_module = MagicMock()
    mock_db_module.supabase.table.return_value.insert.return_value.execute.side_effect = Exception(
        "connection refused"
    )

    with patch.dict("sys.modules", {"app.database": mock_db_module}):
        result = await manager.acquire("msg-1", clinic_id=None)

    assert result is True  # still fails open
    assert get_fail_open_count() == before + 1


@pytest.mark.asyncio
async def test_acquire_duplicate_does_not_increment_fail_open_counter():
    """A genuine duplicate (unique violation) is expected behavior, not a
    failure — must not count toward the fail-open alert threshold."""
    from app.services.message_queue import MessageQueueManager, get_fail_open_count

    manager = MessageQueueManager()
    before = get_fail_open_count()

    mock_db_module = MagicMock()
    mock_db_module.supabase.table.return_value.insert.return_value.execute.side_effect = Exception(
        "duplicate key value violates unique constraint"
    )

    with patch.dict("sys.modules", {"app.database": mock_db_module}):
        result = await manager.acquire("msg-1", clinic_id=None)

    assert result is False
    assert get_fail_open_count() == before


@pytest.mark.asyncio
async def test_alert_message_queue_fail_open_triggers_above_threshold():
    """If > 5 fail-open events occurred since last check, send admin WhatsApp alert."""
    from app.services.scheduler import SchedulerService
    import app.services.scheduler as scheduler_mod

    service = SchedulerService()
    scheduler_mod._last_fail_open_count = 0

    with patch(
        "app.services.message_queue.get_fail_open_count", return_value=6
    ), patch(
        "app.services.tenant.resolve_tenant", new_callable=AsyncMock, return_value={"id": "default"}
    ), patch(
        "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
    ) as mock_send:
        await service.alert_message_queue_fail_open()

    mock_send.assert_called_once()
    assert "fail-open rate elevated" in mock_send.call_args.args[2]
    assert scheduler_mod._last_fail_open_count == 6


@pytest.mark.asyncio
async def test_alert_message_queue_fail_open_silent_below_threshold():
    """If <= 5 fail-open events occurred, stay silent (don't alert)."""
    from app.services.scheduler import SchedulerService
    import app.services.scheduler as scheduler_mod

    service = SchedulerService()
    scheduler_mod._last_fail_open_count = 0

    with patch(
        "app.services.message_queue.get_fail_open_count", return_value=3
    ), patch(
        "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
    ) as mock_send:
        await service.alert_message_queue_fail_open()

    mock_send.assert_not_called()
    assert scheduler_mod._last_fail_open_count == 3

