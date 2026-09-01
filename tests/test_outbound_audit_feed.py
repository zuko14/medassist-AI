"""Granular outbound classification and the platform-owner audit feed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.message_accounting import (
    APPOINTMENT_REMINDER,
    BOOKING_CONFIRMATION,
    BROADCAST,
    FOLLOW_UP,
    LAB_REPORT,
    OTHER,
    OUTBOUND_CLASS_LABELS,
    OUTBOUND_CLASSES,
    PRESCRIPTION,
    classify_source,
)


# -- The six required classifications ---------------------------------------


def test_all_six_required_classes_exist():
    for required in (
        "LAB_REPORT", "PRESCRIPTION", "APPOINTMENT_REMINDER",
        "FOLLOW_UP", "BOOKING_CONFIRMATION", "BROADCAST",
    ):
        assert required in OUTBOUND_CLASSES
        assert OUTBOUND_CLASS_LABELS[required]


@pytest.mark.parametrize("source,expected", [
    ("lab_reports", LAB_REPORT),
    ("lab_reports_retry", LAB_REPORT),
    ("prescriptions", PRESCRIPTION),
    ("appointment_reminder", APPOINTMENT_REMINDER),
    ("follow_up", FOLLOW_UP),
    ("booking_confirmation", BOOKING_CONFIRMATION),
    ("broadcast", BROADCAST),
])
def test_source_service_maps_to_its_class(source, expected):
    assert classify_source(source) == expected


def test_classification_is_case_and_whitespace_insensitive():
    assert classify_source("  Lab_Reports  ") == LAB_REPORT


def test_an_unknown_source_is_other_not_a_crash():
    assert classify_source("something_new") == OTHER
    assert classify_source(None) == OTHER
    assert classify_source("") == OTHER


# -- Retro-classification of pre-existing rows -------------------------------
# Every ledger row written before the granular sources existed says
# source_service='scheduler'. The template name still says what it was.


@pytest.mark.parametrize("template,expected", [
    ("appointment_reminder_24h", APPOINTMENT_REMINDER),
    ("appointment_reminder_2h", APPOINTMENT_REMINDER),
    ("followup_visit", FOLLOW_UP),
    ("health_checkin_day3", FOLLOW_UP),
    ("lab_report_ready", LAB_REPORT),
])
def test_legacy_scheduler_rows_classify_by_template_name(template, expected):
    assert classify_source("scheduler", template) == expected


def test_source_service_wins_over_the_template_fallback():
    assert classify_source("lab_reports", "appointment_reminder_24h") == LAB_REPORT


def test_a_legacy_row_with_no_usable_template_is_other():
    assert classify_source("scheduler", "some_custom_template") == OTHER


# -- The audit feed ----------------------------------------------------------

CLINIC = "11111111-1111-1111-1111-111111111111"

LEDGER_ROWS = [
    {"id": "r1", "clinic_id": CLINIC, "recipient_phone": "+919000000001",
     "message_type": "template", "template_name": "lab_report_ready",
     "category": "utility", "send_success": True, "source_service": "lab_reports",
     "meta_message_id": "wamid.1", "sent_at": "2026-09-01T10:00:00+00:00"},
    {"id": "r2", "clinic_id": CLINIC, "recipient_phone": "+919000000002",
     "message_type": "template", "template_name": "appointment_reminder_24h",
     "category": "utility", "send_success": True, "source_service": "appointment_reminder",
     "meta_message_id": "wamid.2", "sent_at": "2026-09-01T09:00:00+00:00"},
    {"id": "r3", "clinic_id": CLINIC, "recipient_phone": "+919000000003",
     "message_type": "text", "template_name": None,
     "category": "utility", "send_success": False, "source_service": "prescriptions",
     "meta_message_id": None, "sent_at": "2026-09-01T08:00:00+00:00"},
]


def _feed_mocks(rows=None):
    """Route the three tables the feed reads through one MagicMock chain."""
    rows = LEDGER_ROWS if rows is None else rows

    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        for m in ("eq", "neq", "gte", "lt", "in_", "order", "limit"):
            getattr(obj, m).return_value = obj
        if name == "outbound_message_ledger":
            obj._rows = rows
        elif name == "clinics":
            obj._rows = [{"id": CLINIC, "name": "Apex Diagnostics"}]
        elif name == "patients":
            obj._rows = [
                {"phone": "+919000000001", "name": "Asha Rao"},
                {"phone": "+919000000002", "name": "Vikram Nair"},
            ]
        else:
            obj._rows = []
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    return table, fake_sb


@pytest.mark.asyncio
async def test_feed_returns_one_entry_per_message_with_full_provenance():
    from app.services.message_accounting import get_outbound_audit_feed

    table, fake_sb = _feed_mocks()
    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", side_effect=fake_sb), \
         patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as pricing:
        sb_mod.table.side_effect = table
        pricing.return_value = {"utility_paise": 25, "marketing_paise": 75,
                                "authentication_paise": 10, "service_paise": 0}
        feed = await get_outbound_audit_feed()

    assert feed["success"] is True
    entries = {e["id"]: e for e in feed["entries"]}
    assert len(entries) == 3

    lab = entries["r1"]
    assert lab["source_class"] == LAB_REPORT
    assert lab["source_class_label"] == "Lab Report"
    assert lab["patient_name"] == "Asha Rao"
    assert lab["recipient_phone"] == "+919000000001"
    assert lab["delivery_status"] == "delivered"
    assert lab["clinic_name"] == "Apex Diagnostics"
    assert lab["sent_at"] == "2026-09-01T10:00:00+00:00"
    assert lab["estimated_cost_paise"] == 25


@pytest.mark.asyncio
async def test_a_failed_send_is_reported_and_costs_nothing():
    from app.services.message_accounting import get_outbound_audit_feed

    table, fake_sb = _feed_mocks()
    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", side_effect=fake_sb), \
         patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as pricing:
        sb_mod.table.side_effect = table
        pricing.return_value = {"utility_paise": 25, "marketing_paise": 75,
                                "authentication_paise": 10, "service_paise": 0}
        feed = await get_outbound_audit_feed()

    failed = next(e for e in feed["entries"] if e["id"] == "r3")
    assert failed["delivery_status"] == "failed"
    assert failed["estimated_cost_paise"] == 0
    assert failed["patient_name"] is None  # not in the patients table


@pytest.mark.asyncio
async def test_feed_can_be_filtered_to_one_class():
    from app.services.message_accounting import get_outbound_audit_feed

    table, fake_sb = _feed_mocks()
    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", side_effect=fake_sb), \
         patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as pricing:
        sb_mod.table.side_effect = table
        pricing.return_value = {"utility_paise": 25, "marketing_paise": 75,
                                "authentication_paise": 10, "service_paise": 0}
        feed = await get_outbound_audit_feed(source_class=APPOINTMENT_REMINDER)

    assert [e["id"] for e in feed["entries"]] == ["r2"]


@pytest.mark.asyncio
async def test_feed_totals_group_by_class():
    from app.services.message_accounting import get_outbound_audit_feed

    table, fake_sb = _feed_mocks()
    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", side_effect=fake_sb), \
         patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as pricing:
        sb_mod.table.side_effect = table
        pricing.return_value = {"utility_paise": 25, "marketing_paise": 75,
                                "authentication_paise": 10, "service_paise": 0}
        feed = await get_outbound_audit_feed()

    assert feed["totals"][LAB_REPORT]["count"] == 1
    assert feed["totals"][APPOINTMENT_REMINDER]["count"] == 1
    assert feed["totals"][PRESCRIPTION]["count"] == 1
    assert feed["total_cost_paise"] == 50  # the failed send is free


@pytest.mark.asyncio
async def test_an_invalid_clinic_scope_returns_nothing_rather_than_everything():
    """The 'default' sentinel must never widen a scoped query to all tenants."""
    from app.services.message_accounting import get_outbound_audit_feed

    table, fake_sb = _feed_mocks()
    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", side_effect=fake_sb), \
         patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock) as pricing:
        sb_mod.table.side_effect = table
        pricing.return_value = {"utility_paise": 25}
        feed = await get_outbound_audit_feed(clinic_id="default")

    assert feed["entries"] == []


# -- The single write path ---------------------------------------------------
# log_outbound() is the only code that writes the ledger, so it is also the
# only place daily usage may be counted. These pin that wiring.


@pytest.mark.asyncio
async def test_a_successful_send_writes_the_ledger_and_advances_usage():
    from app.services.message_accounting import log_outbound

    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", new_callable=AsyncMock), \
         patch("app.services.subscription.record_outbound_usage", new_callable=AsyncMock) as usage:
        sb_mod.table.return_value = MagicMock()
        await log_outbound(
            clinic_id=CLINIC,
            recipient_phone="+919000000001",
            message_type="template",
            source_service="lab_reports",
            template_name="lab_report_ready",
        )

    usage.assert_awaited_once_with(CLINIC, LAB_REPORT)
    assert sb_mod.table.call_args[0][0] == "outbound_message_ledger"


@pytest.mark.asyncio
async def test_a_failed_send_consumes_no_daily_quota():
    from app.services.message_accounting import log_outbound

    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", new_callable=AsyncMock), \
         patch("app.services.subscription.record_outbound_usage", new_callable=AsyncMock) as usage:
        sb_mod.table.return_value = MagicMock()
        await log_outbound(
            clinic_id=CLINIC,
            recipient_phone="+919000000001",
            message_type="template",
            source_service="lab_reports",
            send_success=False,
        )

    usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_read_is_bookkeeping_not_a_message():
    from app.services.message_accounting import log_outbound

    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", new_callable=AsyncMock), \
         patch("app.services.subscription.record_outbound_usage", new_callable=AsyncMock) as usage:
        sb_mod.table.return_value = MagicMock()
        await log_outbound(
            clinic_id=CLINIC,
            recipient_phone="+919000000001",
            message_type="mark_read",
            source_service="conversation",
        )

    usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_usage_failure_still_lets_the_ledger_row_land():
    """Accounting must degrade independently; neither half may take the other down."""
    from app.services.message_accounting import log_outbound

    with patch("app.database.supabase") as sb_mod, \
         patch("app.services.message_accounting.sb", new_callable=AsyncMock) as ledger, \
         patch("app.services.subscription.record_outbound_usage",
               new_callable=AsyncMock, side_effect=RuntimeError("rpc down")):
        sb_mod.table.return_value = MagicMock()
        await log_outbound(
            clinic_id=CLINIC,
            recipient_phone="+919000000001",
            message_type="text",
            source_service="lab_reports",
        )

    # The ledger is the billing source of truth: it must land even when the
    # derived counter blows up.
    assert ledger.await_count == 1
    assert sb_mod.table.call_args[0][0] == "outbound_message_ledger"
