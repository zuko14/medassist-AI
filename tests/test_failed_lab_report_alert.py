"""Regression guard for the 2026-08-25 silent lab-report outage.

50 reports reached status='failed' and nobody was told, because the only
delivery alert watched the inbound `failed_messages` queue. These tests pin the
two properties that would have caught it.
"""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import SchedulerService


@asynccontextmanager
async def _lock_granted(*args, **kwargs):
    yield True


def _supabase_returning(rows):
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.gte.return_value.execute.return_value = (
        MagicMock(data=rows)
    )
    return db


@pytest.mark.asyncio
async def test_failed_lab_reports_alert_logs_even_when_whatsapp_is_the_broken_channel(caplog):
    """The outage that needs alerting most is the one where WhatsApp is down."""
    rows = [
        {"clinic_id": "clinic-1", "report_name": "LIPID PROFILE",
         "error_message": "132001 template does not exist in en", "delivery_updated_at": "2026-08-26T10:00:00Z"},
        {"clinic_id": "clinic-1", "report_name": "CBP",
         "error_message": "132001 template does not exist in en", "delivery_updated_at": "2026-08-26T10:05:00Z"},
    ]
    whatsapp = AsyncMock()
    whatsapp.send_text.side_effect = Exception("WABA unreachable")

    with caplog.at_level(logging.ERROR), \
         patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", _supabase_returning(rows)), \
         patch("app.services.scheduler.whatsapp_service", whatsapp), \
         patch("app.services.tenant.get_clinic_by_id",
               AsyncMock(return_value={"id": "clinic-1", "config": {"phone": "+919999999999"}})):
        await SchedulerService().alert_failed_lab_reports()

    logged = "\n".join(r.message for r in caplog.records)
    assert "ALERT failed_lab_reports" in logged
    assert "2 report(s)" in logged
    assert "132001" in logged  # the real Meta reason, not a guess


@pytest.mark.asyncio
async def test_failed_lab_reports_alert_stays_quiet_when_nothing_failed(caplog):
    whatsapp = AsyncMock()
    with caplog.at_level(logging.ERROR), \
         patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", _supabase_returning([])), \
         patch("app.services.scheduler.whatsapp_service", whatsapp):
        await SchedulerService().alert_failed_lab_reports()

    assert "ALERT failed_lab_reports" not in "\n".join(r.message for r in caplog.records)
    assert not whatsapp.send_text.called


def test_failed_lab_reports_alert_is_registered_on_the_scheduler():
    assert hasattr(SchedulerService(), "alert_failed_lab_reports")
