"""Post-visit follow-up engine — the feature that makes this more than a booking bot.

Before these tests it had never sent a message in production, for three
independent reasons, each guarded below:

1. Nothing ever wrote `status='completed'`, the state send_followups() filters
   on, so the daily query matched zero rows.
2. The gate checked `has_feature(clinic, "reminders_post_visit")` — a flag that
   exists in no plan — and on failure marked `followup_sent=True`, permanently
   burning the appointment.
3. A refused send was also marked as sent, so one transient Meta error lost
   that patient's follow-up forever.

Also covers the admin-configurable offset/message and the lab-test CTA.
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.lab_reports import lab_booking_cta
from app.services.scheduler import (
    FOLLOWUP_LOOKBACK_DAYS,
    SchedulerService,
    followup_config,
)


# ── Fake Supabase ────────────────────────────────────────────────────────────

class FakeTable:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self._filters = {}
        self._payload = None
        self._op = "select"

    def select(self, *a, **k):
        self._op = "select"
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def eq(self, col, val):
        self._filters[("eq", col)] = val
        return self

    def lt(self, col, val):
        self._filters[("lt", col)] = val
        return self

    def gte(self, col, val):
        self._filters[("gte", col)] = val
        return self

    def lte(self, col, val):
        self._filters[("lte", col)] = val
        return self

    def limit(self, *a, **k):
        return self

    def _matches(self, row):
        for (op, col), val in self._filters.items():
            actual = row.get(col)
            if op == "eq" and actual != val:
                return False
            if op == "lt" and not (actual is not None and actual < val):
                return False
            if op == "gte" and not (actual is not None and actual >= val):
                return False
            if op == "lte" and not (actual is not None and actual <= val):
                return False
        return True

    def execute(self):
        rows = [r for r in self.db.rows(self.name) if self._matches(r)]
        if self._op == "update":
            for r in rows:
                r.update(self._payload)
                self.db.updates.append((r["id"], dict(self._payload)))
            return SimpleNamespace(data=rows)
        return SimpleNamespace(data=[dict(r) for r in rows])


class FakeDB:
    def __init__(self, appointments):
        self._appointments = appointments
        self.updates = []

    def rows(self, name):
        return self._appointments if name == "appointments" else []

    def table(self, name):
        return FakeTable(self, name)


class NullLock:
    async def __aenter__(self):
        return True

    async def __aexit__(self, *a):
        return False


CLINIC = {"id": "c1", "name": "Test Clinic", "plan": "essential", "config": {}}


def _run(db, coro_name, clinic, send_result=True):
    svc = SchedulerService()
    send = AsyncMock(return_value=send_result)

    with patch("app.services.scheduler.supabase", db), \
         patch("app.services.distributed_lock.distributed_job_lock",
               lambda *a, **k: NullLock()), \
         patch("app.services.scheduler.get_clinic_by_id",
               new=AsyncMock(return_value=clinic)), \
         patch("app.services.scheduler.whatsapp_service.send_template", new=send):
        asyncio.run(getattr(svc, coro_name)())
    return send


def _appt(**kw):
    row = {
        "id": "a1",
        "clinic_id": "c1",
        "patient_name": "Ravi Kumar",
        "patient_phone": "+919999999999",
        "status": "confirmed",
        "appointment_date": (date.today() - timedelta(days=1)).isoformat(),
        "followup_sent": False,
    }
    row.update(kw)
    return row


# ── 1. Auto-complete: the missing lifecycle step ─────────────────────────────

def test_past_confirmed_appointment_is_completed():
    rows = [_appt()]
    _run(FakeDB(rows), "auto_complete_appointments", CLINIC)
    assert rows[0]["status"] == "completed"
    assert rows[0]["completed_at"]


def test_future_and_cancelled_appointments_are_left_alone():
    future = _appt(id="future",
                   appointment_date=(date.today() + timedelta(days=2)).isoformat())
    today = _appt(id="today", appointment_date=date.today().isoformat())
    cancelled = _appt(id="cancelled", status="cancelled")
    rows = [future, today, cancelled]
    _run(FakeDB(rows), "auto_complete_appointments", CLINIC)
    assert future["status"] == "confirmed"
    assert today["status"] == "confirmed", "today's visit is not over yet"
    assert cancelled["status"] == "cancelled"


# ── 2 & 3. Follow-up sending ─────────────────────────────────────────────────

def test_followup_sends_for_a_completed_visit_on_a_standard_plan():
    """The old gate checked a flag no plan has, so this never fired."""
    rows = [_appt(status="completed")]
    send = _run(FakeDB(rows), "send_followups", CLINIC)
    send.assert_awaited_once()
    assert rows[0]["followup_sent"] is True


def test_refused_send_is_not_marked_sent():
    rows = [_appt(status="completed")]
    _run(FakeDB(rows), "send_followups", CLINIC, send_result=False)
    assert rows[0]["followup_sent"] is False, (
        "a refused send was burned — this patient would never be followed up"
    )


def test_followup_waits_for_the_configured_offset():
    clinic = {**CLINIC, "config": {"followup_days": 5}}

    two_days_ago = (date.today() - timedelta(days=2)).isoformat()
    rows = [_appt(status="completed", appointment_date=two_days_ago)]
    send = _run(FakeDB(rows), "send_followups", clinic)
    send.assert_not_awaited()
    assert rows[0]["followup_sent"] is False, "must stay eligible until day 5"

    five_days_ago = (date.today() - timedelta(days=5)).isoformat()
    rows = [_appt(status="completed", appointment_date=five_days_ago)]
    send = _run(FakeDB(rows), "send_followups", clinic)
    send.assert_awaited_once()


def test_disabled_clinic_sends_nothing():
    clinic = {**CLINIC, "config": {"followup_enabled": False}}
    rows = [_appt(status="completed")]
    send = _run(FakeDB(rows), "send_followups", clinic)
    send.assert_not_awaited()
    assert rows[0]["followup_sent"] is True, "suppressed, so stop rescanning it"


def test_stale_visit_past_the_retry_window_stops_being_rescanned():
    old = (date.today() - timedelta(days=1 + FOLLOWUP_LOOKBACK_DAYS + 1)).isoformat()
    rows = [_appt(status="completed", appointment_date=old)]
    send = _run(FakeDB(rows), "send_followups", CLINIC, send_result=False)
    send.assert_not_awaited()
    assert rows[0]["followup_sent"] is True


# ── Admin-configurable message and template ──────────────────────────────────

def test_custom_message_needs_a_message_carrying_template():
    """Without an approved 2-variable template Meta cannot carry the wording."""
    cfg = followup_config(
        {**CLINIC, "config": {"followup_message": "Hope you feel better."}}
    )
    template, components = SchedulerService._followup_template_and_components(cfg, "Ravi")
    assert template == "post_appointment_followup"
    assert len(components[0]["parameters"]) == 2
    assert components[0]["parameters"][1]["text"] != "Hope you feel better."


def test_custom_message_is_delivered_when_a_template_is_configured():
    cfg = followup_config({
        **CLINIC,
        "config": {
            "followup_message": "Hope you feel better.\n\nReply to book again.",
            "followup_message_template_name": "patient_followup_message",
        },
    })
    template, components = SchedulerService._followup_template_and_components(cfg, "Ravi")
    assert template == "patient_followup_message"
    params = components[0]["parameters"]
    assert params[0]["text"] == "Ravi"
    # Flattened: Meta rejects newlines inside a template parameter.
    assert params[1]["text"] == "Hope you feel better. Reply to book again."


def test_followup_days_is_clamped_to_a_sane_range():
    assert followup_config({**CLINIC, "config": {"followup_days": 0}})["days"] == 1
    assert followup_config({**CLINIC, "config": {"followup_days": 999}})["days"] == 30
    assert followup_config({**CLINIC, "config": {"followup_days": "junk"}})["days"] >= 1


# ── Lab-test CTA ─────────────────────────────────────────────────────────────

def test_cta_offered_only_to_clinics_that_sell_lab_tests():
    selling = {"id": "c1", "plan": "diagstream", "config": {}}
    not_selling = {"id": "c2", "plan": "essential", "config": {}}
    assert "BOOK TEST" in lab_booking_cta(selling)
    assert lab_booking_cta(not_selling) == ""
    assert lab_booking_cta(None) == ""


def test_cta_is_a_caption_suffix_not_an_extra_message():
    """It must start with a separator so it appends cleanly and costs nothing."""
    cta = lab_booking_cta({"id": "c1", "plan": "diagstream", "config": {}})
    assert cta.startswith("\n\n")
    assert len(cta) < 200, "a caption suffix, not a second message"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
