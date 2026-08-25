"""Phase E: Failure Metric Semantics Verification.

Verifies:
1. Fail-closed counters accurately track database errors during message acquire.
2. get_fail_closed_count and get_fail_open_count remain semantically synchronized.
3. Database failure during acquire() fails closed (returns False) and increments the fail-closed counter.
4. Scheduler alert_message_queue_fail_closed alerts when threshold is exceeded.
"""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.message_queue import (
    MessageQueueManager,
    get_fail_closed_count,
    get_fail_open_count,
    _record_fail_closed,
)
from app.services.scheduler import SchedulerService
import app.services.scheduler as scheduler_mod


@pytest.mark.asyncio
async def test_fail_closed_counter_increments_on_database_error():
    """P1-5: acquire() fails closed on database error and increments fail-closed metric."""
    manager = MessageQueueManager()
    before = get_fail_closed_count()

    mock_table = MagicMock()
    mock_table.insert.side_effect = RuntimeError("Supabase connection timeout")

    with patch("app.database.supabase.table", return_value=mock_table):
        acquired = await manager.acquire("wamid.FAIL_CLOSED_TEST_01", clinic_id="clinic_test")
        assert acquired is False
        assert get_fail_closed_count() == before + 1
        # Check alias
        assert get_fail_open_count() == before + 1


@pytest.mark.asyncio
async def test_scheduler_alerts_on_elevated_fail_closed_rate():
    """P1-5: Scheduler detects elevated fail-closed rate (>5) and dispatches alert."""
    service = SchedulerService()
    scheduler_mod._last_fail_open_count = 0

    fake_clinic = {"id": "clinic_default", "phone": "+919999999999"}

    with patch("app.services.message_queue.get_fail_closed_count", return_value=8), \
         patch("app.services.tenant.resolve_tenant", new_callable=AsyncMock, return_value=fake_clinic), \
         patch("app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock) as mock_send:

        await service.alert_message_queue_fail_closed()
        assert scheduler_mod._last_fail_open_count == 8
        mock_send.assert_called_once()
        assert "fail-closed" in mock_send.call_args[0][2].lower()
