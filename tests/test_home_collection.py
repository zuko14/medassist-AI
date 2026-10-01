"""Home sample collection (migration 097).

Covers: the schema guarantees (real PostgreSQL), slot/area/fee/eligibility
rules, phlebotomist choice and compare-and-set assignment, the WhatsApp steps,
payment pricing, the phlebotomist login's confinement, and visit ownership.
"""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import home_collection as router_mod
from app.routers.admin import AdminUser, _enforce_phlebotomist_scope
from app.services import home_collection as hc
from app.services import home_collection_flow as flow
from app.services.tenant import home_collection_available

CLINIC = "11111111-2222-3333-4444-555555555555"
IST = hc.IST
client = TestClient(app)


# ═══════ schema (real PostgreSQL) ═══════


def _clinic_id(cur) -> str:
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'diagbooking', true) RETURNING id",
        ("HC " + uuid.uuid4().hex[:6], "x-" + uuid.uuid4().hex),
    )
    return cur.fetchone()[0]


def _insert_appt(cur, clinic_id, **cols):
    row = {
        "clinic_id": clinic_id, "patient_phone": "+919876543210", "patient_name": "Ravi",
        "department": "Lab Test", "appointment_date": "2026-10-05", "status": "confirmed",
        "booking_type": "lab_test", "booking_ref": "HC-" + uuid.uuid4().hex[:8],
    }
    row.update(cols)
    cur.execute(
        f"INSERT INTO appointments ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))}) RETURNING id, collection_mode",
        list(row.values()),
    )
    return cur.fetchone()


HOME = {
    "collection_mode": "home", "collection_slot": "07:00-08:00", "collection_address": "Flat 302, Sai Residency",
    "collection_lat": 17.72, "collection_lng": 83.30, "collection_contact_phone": "+919876543210",
    "collection_status": "unassigned",
}


def test_existing_style_rows_default_to_centre(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    _, mode = _insert_appt(cur, cid)
    assert mode == "centre"
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_a_complete_home_booking_is_accepted(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    _, mode = _insert_appt(cur, cid, **HOME)
    assert mode == "home"
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


@pytest.mark.parametrize("missing", ["collection_slot", "collection_address", "collection_lat",
                                     "collection_contact_phone", "collection_status"])
def test_an_incomplete_home_booking_is_refused(real_pg_conn, missing):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_appt(cur, cid, **{k: v for k, v in HOME.items() if k != missing})
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_only_a_lab_test_can_be_collected_at_home(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_appt(cur, cid, booking_type="consultation", appointment_time="10:00", **HOME)
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_unknown_collection_status_is_refused(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_appt(cur, cid, **{**HOME, "collection_status": "teleported"})
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_dpdp_erasure_of_a_home_booking_satisfies_the_check(real_pg_conn):
    """data_retention.anonymize_clinical_records' two updates, as SQL."""
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    appt_id, _ = _insert_appt(cur, cid, **HOME)
    cur.execute("UPDATE appointments SET patient_name='[REDACTED]', patient_phone='[REDACTED]' WHERE id=%s", (appt_id,))
    cur.execute(
        "UPDATE appointments SET collection_address='[REDACTED]', collection_landmark=NULL, collection_lat=NULL, "
        "collection_lng=NULL, collection_contact_phone='[REDACTED]', collection_notes=NULL WHERE id=%s", (appt_id,))
    cur.execute("SELECT collection_lat, collection_address FROM appointments WHERE id=%s", (appt_id,))
    assert cur.fetchone() == (None, "[REDACTED]")
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_deleting_a_phlebotomist_unassigns_rather_than_deletes_visits(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _clinic_id(cur)
    cur.execute(
        "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, permissions, is_active, full_name, phone) "
        "VALUES (%s, %s, 'x', 'staff', 'PHLEBOTOMIST', '{}', true, 'Suresh', '+919000000001') RETURNING id",
        (cid, "ph_" + uuid.uuid4().hex[:8]))
    phleb = cur.fetchone()[0]
    appt_id, _ = _insert_appt(cur, cid, **{**HOME, "phlebotomist_id": phleb, "collection_status": "assigned"})
    cur.execute("DELETE FROM clinic_admins WHERE id = %s", (phleb,))
    cur.execute("SELECT phlebotomist_id FROM appointments WHERE id=%s", (appt_id,))
    assert cur.fetchone()[0] is None
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


# ═══════ rules ═══════


WINDOW = {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
          "sunday_start": "08:00", "sunday_end": "10:00"}


def test_slots_cover_the_window_and_cut_the_last_one_short():
    s = hc.normalize_settings({"enabled": True, "slot_minutes": 90, "lead_minutes": 0})
    early = datetime(2026, 10, 5, 0, 0, tzinfo=IST)  # a Monday
    assert hc.slots_for(WINDOW, s, "2026-10-05", early) == ["07:00-08:30", "08:30-10:00", "10:00-11:00"]


def test_sunday_uses_sunday_hours():
    s = hc.normalize_settings({"slot_minutes": 60, "lead_minutes": 0})
    assert hc.slots_for(WINDOW, s, "2026-10-04", datetime(2026, 10, 1, tzinfo=IST)) == ["08:00-09:00", "09:00-10:00"]


def test_today_drops_slots_inside_the_notice_period():
    s = hc.normalize_settings({"slot_minutes": 60, "lead_minutes": 60})
    now = datetime(2026, 10, 5, 7, 30, tzinfo=IST)
    assert hc.slots_for(WINDOW, s, "2026-10-05", now) == ["09:00-10:00", "10:00-11:00"]


@pytest.mark.asyncio
async def test_full_slots_are_not_offered():
    s = hc.normalize_settings({"slot_minutes": 60, "lead_minutes": 0, "slot_capacity": 2})
    with patch.object(hc, "slot_load", AsyncMock(return_value={"07:00-08:00": 2, "08:00-09:00": 1})):
        slots = await hc.open_slots(CLINIC, WINDOW, s, "2026-10-05", now=datetime(2026, 10, 1, tzinfo=IST))
    assert slots == ["08:00-09:00", "09:00-10:00", "10:00-11:00"]


def test_settings_are_normalized_defensively():
    s = hc.normalize_settings({"enabled": "yes", "fee_paise": "-5", "slot_minutes": 45, "centre_lat": 999})
    assert s["enabled"] is False and s["fee_paise"] == 0 and s["slot_minutes"] == 60 and s["centre_lat"] is None
    assert hc.normalize_settings(None) == hc.DEFAULT_SETTINGS


def test_fee_and_free_threshold():
    s = hc.normalize_settings({"fee_paise": 10000, "free_above_paise": 150000})
    assert hc.fee_for(s, 50000) == 10000
    assert hc.fee_for(s, 150000) == 0


@pytest.mark.parametrize("test,ok", [
    ({"name": "Complete Blood Count", "category": "Lab Tests (Pathology)"}, True),
    ({"name": "Master Health Checkup", "category": "Health Packages"}, True),
    ({"name": "HbA1c"}, True),
    ({"name": "Chest X-Ray (PA View)"}, False),
    ({"name": "MRI Brain"}, False),
    ({"name": "ECG"}, False),
    ({"name": "Abdomen study", "category": "Radiology & Imaging"}, False),
])
def test_only_sample_based_tests_are_home_collectable(test, ok):
    assert hc.is_home_collectable(test) is ok


def test_coordinates_from_typed_text_and_maps_links():
    assert hc.parse_coordinates("17.7231, 83.3012") == (17.7231, 83.3012)
    assert hc.parse_coordinates("https://maps.google.com/?q=17.7231,83.3012") == (17.7231, 83.3012)
    assert hc.parse_coordinates("https://www.google.com/maps/@17.7231,83.3012,15z") == (17.7231, 83.3012)
    assert hc.parse_coordinates("Flat 302 near temple") is None
    assert hc.parse_coordinates("0.0000, 0.0000") is None


def test_service_radius():
    s = hc.normalize_settings({"max_distance_km": 10, "centre_lat": 17.7231, "centre_lng": 83.3012})
    assert hc.outside_service_area(s, 17.75, 83.31) is None  # ~3 km
    assert hc.outside_service_area(s, 17.98, 83.50) > 10  # ~35 km
    assert hc.outside_service_area(hc.normalize_settings({"max_distance_km": 10}), 30.0, 70.0) is None


@pytest.mark.parametrize("raw,want", [
    ("9876543210", "+919876543210"), ("+91 98765 43210", "+919876543210"),
    ("09876543210", "+919876543210"), ("919876543210", "+919876543210"),
    ("1234567890", None), ("98765", None),
])
def test_contact_number_normalization(raw, want):
    assert flow.normalize_contact(raw) == want


def test_slot_button_round_trip():
    assert flow.slot_from_button(flow.slot_button_id("07:00-08:30")) == "07:00-08:30"
    assert flow.slot_from_button("hcslot_x") is None


@pytest.mark.parametrize("clinic,ok", [
    ({"plan": "diagstream"}, True),
    ({"plan": "diagbooking"}, True),
    ({"plan": "polyclinic"}, False),
    ({"plan": "enterprise"}, False),
    ({"plan": "diagbooking", "account_type": "corporate_partner"}, False),
    ({"plan": "diagbooking", "features": {"lab_test_booking": False}}, False),
])
def test_only_diagnostic_plans_get_home_collection(clinic, ok):
    assert home_collection_available(clinic) is ok


# ═══════ assignment ═══════


P1 = {"id": "p1", "full_name": "Anil", "branch_id": None, "is_active": True}
P2 = {"id": "p2", "full_name": "Bala", "branch_id": "b2", "is_active": True}
P3 = {"id": "p3", "full_name": "Chitra", "branch_id": "b1", "is_active": True}


def test_branch_pinned_phlebotomists_only_serve_their_branch():
    appt = {"branch_id": "b1", "collection_slot": "07:00-08:00"}
    assert hc.pick_phlebotomist(appt, [P2], []) is None
    # Same load: the one pinned to the booking's branch wins over a floater.
    assert hc.pick_phlebotomist(appt, [P1, P3], [])["id"] == "p3"


def test_least_busy_in_the_slot_then_the_day():
    appt = {"branch_id": None, "collection_slot": "07:00-08:00"}
    loads = [{"phlebotomist_id": "p1", "collection_slot": "07:00-08:00"},
             {"phlebotomist_id": "p2", "collection_slot": "09:00-10:00"},
             {"phlebotomist_id": "p2", "collection_slot": "10:00-11:00"}]
    assert hc.pick_phlebotomist(appt, [P1, P2], loads)["id"] == "p2"
    assert hc.pick_phlebotomist(appt, [P1, {**P2, "is_active": False}], loads)["id"] == "p1"


@pytest.mark.asyncio
async def test_auto_assign_writes_once_and_tells_the_patient():
    visit = {"id": "a1", "clinic_id": CLINIC, "status": "confirmed", "phlebotomist_id": None,
             "appointment_date": "2026-10-05", "collection_slot": "07:00-08:00", "branch_id": None}
    with patch.object(hc, "get_visit", AsyncMock(return_value=visit)), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[P1])), \
         patch.object(hc, "_day_loads", AsyncMock(return_value=[])), \
         patch.object(hc, "_write_assignment", AsyncMock(return_value=True)) as write, \
         patch.object(hc, "notify_patient", AsyncMock()) as notify:
        assert (await hc.auto_assign(CLINIC, "a1"))["id"] == "p1"
    write.assert_awaited_once_with(CLINIC, visit, "p1", expect_unassigned=True)
    notify.assert_awaited_once_with(visit, "assigned", P1)


@pytest.mark.asyncio
async def test_auto_assign_lost_race_sends_nothing():
    visit = {"id": "a1", "status": "confirmed", "phlebotomist_id": None, "appointment_date": "2026-10-05"}
    with patch.object(hc, "get_visit", AsyncMock(return_value=visit)), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[P1])), \
         patch.object(hc, "_day_loads", AsyncMock(return_value=[])), \
         patch.object(hc, "_write_assignment", AsyncMock(return_value=False)), \
         patch.object(hc, "notify_patient", AsyncMock()) as notify:
        assert await hc.auto_assign(CLINIC, "a1") is None
    notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("visit", [
    None,
    {"id": "a1", "status": "pending_payment", "phlebotomist_id": None},
    {"id": "a1", "status": "confirmed", "phlebotomist_id": "p9"},
])
async def test_auto_assign_skips_unpaid_or_already_assigned(visit):
    with patch.object(hc, "get_visit", AsyncMock(return_value=visit)), \
         patch.object(hc, "_write_assignment", AsyncMock()) as write:
        assert await hc.auto_assign(CLINIC, "a1") is None
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_phlebotomist_alerts_the_centre_once():
    visit = {"id": "a1", "status": "confirmed", "phlebotomist_id": None, "appointment_date": "2026-10-05",
             "booking_ref": "R1", "branch_id": "b1"}
    with patch.object(hc, "get_visit", AsyncMock(return_value=visit)), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[P2])), \
         patch.object(hc, "_day_loads", AsyncMock(return_value=[])), \
         patch.object(hc, "_notify_admins", AsyncMock()) as alert:
        assert await hc.auto_assign(CLINIC, "a1", alert_if_none=True) is None
        assert await hc.auto_assign(CLINIC, "a1") is None  # the sweep: silent
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_assign_never_raises():
    with patch.object(hc, "get_visit", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await hc.auto_assign(CLINIC, "a1") is None


@pytest.mark.asyncio
async def test_status_moves_follow_the_lifecycle():
    visit = {"id": "a1", "collection_status": "collected", "phlebotomist_id": None}
    with patch.object(hc, "sb", AsyncMock()) as db:
        assert await hc.set_status(CLINIC, visit, "en_route") is False
    db.assert_not_awaited()


# ═══════ WhatsApp steps ═══════


def _manager():
    m = MagicMock()
    m.whatsapp.send_interactive_buttons = AsyncMock(return_value=True)
    m.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    m.whatsapp.send_text = AsyncMock(return_value=True)
    m.whatsapp.send_location_request = AsyncMock(return_value=True)
    m.update_state = AsyncMock()
    m._ask_lab_test_patient = AsyncMock()
    m._finalize_lab_booking = AsyncMock()
    m._lab_date_buttons = MagicMock(return_value=[])
    return m


CLINIC_ROW = {"id": CLINIC, "plan": "diagbooking", "config": {"home_collection": {"enabled": True, "fee_paise": 10000}}}
SETTINGS = hc.normalize_settings(CLINIC_ROW["config"]["home_collection"])


def _btn(i):
    return {"id": i, "type": "button_reply"}


@pytest.mark.asyncio
async def test_full_home_booking_conversation():
    m = _manager()
    ctx = {"lab_collection_date": "2026-10-05", "hc_available": True, "lab_test_name": "CBC",
           "lab_test_price_paise": 40000}
    with patch.object(flow, "_open_slots", AsyncMock(return_value=["07:00-08:00", "08:00-09:00"])), \
         patch.object(hc, "get_settings", AsyncMock(return_value=SETTINGS)), \
         patch.object(hc, "last_home_address", AsyncMock(return_value=None)):
        await flow.ask_mode(m, CLINIC_ROW, "919876543210", ctx, "en")
        assert ctx["lab_step"] == "hc_mode"

        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "", ctx, None, "en", _btn("hcmode_home"))
        assert ctx["lab_step"] == "hc_slot"
        rows = m.whatsapp.send_interactive_list.call_args.kwargs["sections"][0]["rows"]
        assert [r["id"] for r in rows] == ["hcslot_0700-0800", "hcslot_0800-0900"]

        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "", ctx, None, "en", _btn("hcslot_0800-0900"))
        assert ctx["hc_slot"] == "08:00-09:00"
        m._ask_lab_test_patient.assert_awaited_once()  # who is it for: the existing step

        # The lab flow then reaches finalize with the name; we park it and ask for the location.
        assert await flow.intercept_finalize(m, CLINIC_ROW, "919876543210", ctx, "en", "Ravi Kumar")
        assert ctx["lab_step"] == "hc_location"
        m.whatsapp.send_location_request.assert_awaited_once()

        loc = {"type": "location", "latitude": 17.72, "longitude": 83.30, "name": "Sai Residency"}
        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "", ctx, None, "en", loc)
        assert ctx["lab_step"] == "hc_address" and ctx["hc_pin"] == "Sai Residency"

        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "hi", ctx, None, "en", None)
        assert ctx["lab_step"] == "hc_address"  # too short: asked again

        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "Flat 302, MVP Colony, near SBI ATM",
                                      ctx, None, "en", None)
        assert ctx["lab_step"] == "hc_contact"

        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "", ctx, None, "en", _btn("hccontact_other"))
        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "12345", ctx, None, "en", None)
        assert ctx["lab_step"] == "hc_contact_number"
        assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "98480 22338", ctx, None, "en", None)
        assert ctx["hc_contact"] == "+919848022338" and ctx["lab_step"] == "hc_confirm"
        summary = m.whatsapp.send_interactive_buttons.call_args.kwargs["body"]
        assert "₹400" in summary and "₹100" in summary and "₹500" in summary

        with patch("app.services.tenant.home_collection_available", return_value=True):
            assert await flow.handle_step(m, CLINIC_ROW, "919876543210", "", ctx, None, "en", _btn("hcconfirm_yes"))
    assert ctx["hc_ready"] is True
    m._finalize_lab_booking.assert_awaited_once()
    assert m._finalize_lab_booking.call_args.args[-1] == "Ravi Kumar"
    fields = flow.booking_fields(ctx)
    assert fields["collection_mode"] == "home" and fields["home_collection_fee_paise"] == 10000
    assert fields["collection_lat"] == 17.72 and fields["collection_status"] == "unassigned"


@pytest.mark.asyncio
async def test_visit_centre_keeps_the_existing_path():
    m = _manager()
    ctx = {"lab_collection_date": "2026-10-05", "hc_available": True, "lab_step": "hc_mode"}
    assert await flow.handle_step(m, CLINIC_ROW, "91", "", ctx, None, "en", _btn("hcmode_centre"))
    assert ctx["hc_mode"] == "centre"
    m._ask_lab_test_patient.assert_awaited_once()
    assert not await flow.intercept_finalize(m, CLINIC_ROW, "91", ctx, "en", "Ravi")


@pytest.mark.asyncio
async def test_lab_steps_are_left_to_the_lab_flow():
    m = _manager()
    ctx = {"hc_mode": "home", "lab_step": "who"}
    assert not await flow.handle_step(m, CLINIC_ROW, "91", "", ctx, None, "en", _btn("labfor_self"))
    assert not await flow.handle_step(m, CLINIC_ROW, "91", "", {}, None, "en", _btn("labfor_self"))


@pytest.mark.asyncio
async def test_location_outside_the_radius_offers_centre_or_another_pin():
    m = _manager()
    far = hc.normalize_settings({"enabled": True, "max_distance_km": 5, "centre_lat": 17.72, "centre_lng": 83.30})
    ctx = {"hc_mode": "home", "lab_step": "hc_location", "hc_patient_name": "Ravi"}
    with patch.object(hc, "get_settings", AsyncMock(return_value=far)):
        await flow.handle_step(m, CLINIC_ROW, "91", "", ctx, None, "en",
                               {"type": "location", "latitude": 18.2, "longitude": 83.9})
    assert ctx.get("hc_lat") is None
    ids = [b["id"] for b in m.whatsapp.send_interactive_buttons.call_args.kwargs["buttons"]]
    assert ids == ["hcaddr_new", "hcmode_centre"]


@pytest.mark.asyncio
async def test_a_pasted_maps_link_is_accepted_on_whatsapp_web():
    m = _manager()
    ctx = {"hc_mode": "home", "lab_step": "hc_location"}
    with patch.object(hc, "get_settings", AsyncMock(return_value=SETTINGS)):
        await flow.handle_step(m, CLINIC_ROW, "91", "https://maps.google.com/?q=17.7231,83.3012", ctx, None, "en", None)
    assert (ctx["hc_lat"], ctx["hc_lng"]) == (17.7231, 83.3012)


@pytest.mark.asyncio
async def test_a_slot_filled_before_confirm_asks_again():
    m = _manager()
    ctx = {"hc_mode": "home", "lab_step": "hc_confirm", "hc_slot": "07:00-08:00", "hc_patient_name": "R",
           "hc_lat": 1.0, "hc_lng": 1.0, "hc_address": "Flat 1 street", "hc_contact": "+919848022338",
           "lab_collection_date": "2026-10-05"}
    with patch.object(hc, "get_settings", AsyncMock(return_value=SETTINGS)), \
         patch("app.services.tenant.home_collection_available", return_value=True), \
         patch.object(flow, "_open_slots", AsyncMock(return_value=["08:00-09:00"])):
        await flow.handle_step(m, CLINIC_ROW, "91", "", ctx, None, "en", _btn("hcconfirm_yes"))
    m._finalize_lab_booking.assert_not_awaited()
    assert ctx["hc_slot"] is None and ctx["lab_step"] == "hc_slot"


@pytest.mark.asyncio
async def test_test_card_offers_home_only_when_switched_on():
    ctx = {}
    off = hc.normalize_settings({"enabled": False})
    with patch.object(hc, "get_settings", AsyncMock(return_value=off)):
        assert await flow.annotate_test(CLINIC_ROW, ctx, {"name": "CBC", "price_paise": 30000}, "en") == ""
    assert ctx["hc_available"] is False
    with patch.object(hc, "get_settings", AsyncMock(return_value=SETTINGS)):
        line = await flow.annotate_test(CLINIC_ROW, ctx, {"name": "CBC", "price_paise": 30000}, "en")
    assert ctx["hc_available"] is True and "₹100" in line
    ctx2 = {}
    assert await flow.annotate_test({**CLINIC_ROW, "plan": "polyclinic"}, ctx2, {"name": "CBC"}, "en") == ""
    assert await flow.annotate_test(CLINIC_ROW, ctx2, {"name": "MRI Brain"}, "en") == ""


# ═══════ payment ═══════


@pytest.mark.asyncio
async def test_payment_adds_the_fee_and_writes_only_visit_columns():
    from app.services.payment import PaymentService

    service = PaymentService()
    home = {**flow.booking_fields({"hc_slot": "07:00-08:00", "hc_address": "A", "hc_lat": 1.0, "hc_lng": 2.0,
                                   "hc_contact": "+919848022338", "hc_fee_paise": 10000}),
            "status": "confirmed"}  # must be ignored
    with patch("app.services.payment.supabase") as mock_sb, \
         patch.object(service, "_get_lab_test_fee_paise", AsyncMock(return_value=40000)), \
         patch.object(service, "_create_payment_link", AsyncMock(return_value={"id": "pl", "short_url": "u"})), \
         patch.object(service, "_log_payment_event", AsyncMock()):
        table = MagicMock()
        mock_sb.table.return_value = table
        table.insert.return_value.execute.return_value = MagicMock(data=[{"id": "b1"}])
        result = await service.create_booking_with_payment(
            clinic_id=CLINIC, patient_phone="+91", patient_name="R", department="Lab Test",
            doctor_name=None, appointment_date="2026-10-05", appointment_time=None,
            booking_type="lab_test", lab_test_id="t1", lab_test_name="CBC", home_collection=home,
        )
    assert result["amount_paise"] == 50000
    row = table.insert.call_args.args[0]
    assert row["status"] == "pending_payment" and row["collection_mode"] == "home"
    assert row["collection_slot"] == "07:00-08:00"


# ═══════ phlebotomist login confinement ═══════


PHLEB = AdminUser(username="ph1", role="staff", clinic_id=CLINIC, user_id="p1", staff_role="PHLEBOTOMIST")


def _req(method, path):
    r = MagicMock()
    r.method, r.url.path = method, path
    return r


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/me"), ("GET", "/admin/home-collection/my"),
    ("POST", "/admin/home-collection/visits/abc/status"), ("PUT", "/admin/change-password"),
])
def test_phlebotomist_reaches_its_own_routes(method, path):
    assert _enforce_phlebotomist_scope(_req(method, path), PHLEB) is PHLEB


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/patients"), ("GET", "/admin/appointments"), ("GET", "/admin/home-collection/visits"),
    ("POST", "/admin/home-collection/visits/abc/assign"), ("PUT", "/admin/home-collection/settings"),
    ("GET", "/admin/staff"), ("GET", "/fhir/Patient"),
])
def test_phlebotomist_is_refused_everything_else(method, path):
    with pytest.raises(Exception) as e:
        _enforce_phlebotomist_scope(_req(method, path), PHLEB)
    assert getattr(e.value, "status_code", None) == 403


def test_other_staff_are_untouched():
    desk = AdminUser(username="d", role="staff", clinic_id=CLINIC, user_id="d1", staff_role="STAFF")
    assert _enforce_phlebotomist_scope(_req("GET", "/admin/patients"), desk) is desk


def test_confinement_is_enforced_through_the_real_dependency():
    with patch("app.routers.admin._authenticate_request", AsyncMock(return_value=PHLEB)):
        r = client.get("/admin/patients", params={"clinic_id": CLINIC})
    assert r.status_code == 403


# ═══════ visit ownership through the router ═══════


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[router_mod.verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(router_mod.verify_credentials, None)


VID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _visit(**kw):
    return {"id": VID, "clinic_id": CLINIC, "status": "confirmed", "collection_status": "assigned",
            "phlebotomist_id": "p1", "branch_id": None, **kw}


def test_a_phlebotomist_cannot_update_someone_elses_visit(as_user):
    as_user(PHLEB)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(router_mod, "_phleb_account", AsyncMock(return_value={"id": "p1"})), \
         patch.object(hc, "get_visit", AsyncMock(return_value=_visit(phlebotomist_id="p2"))), \
         patch.object(hc, "set_status", AsyncMock()) as set_status:
        r = client.post(f"/admin/home-collection/visits/{VID}/status", json={"status": "en_route"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 404
    set_status.assert_not_awaited()


def test_a_phlebotomist_updates_their_own_visit(as_user):
    as_user(PHLEB)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(router_mod, "_phleb_account", AsyncMock(return_value={"id": "p1"})), \
         patch.object(hc, "get_visit", AsyncMock(return_value=_visit())), \
         patch.object(hc, "set_status", AsyncMock(return_value=True)), \
         patch.object(router_mod, "log_admin_action", AsyncMock()):
        r = client.post(f"/admin/home-collection/visits/{VID}/status", json={"status": "en_route"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 200


def test_failed_needs_a_reason(as_user):
    as_user(PHLEB)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(router_mod, "_phleb_account", AsyncMock(return_value={"id": "p1"})), \
         patch.object(hc, "get_visit", AsyncMock(return_value=_visit())):
        r = client.post(f"/admin/home-collection/visits/{VID}/status", json={"status": "failed"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 422


def test_a_cancelled_booking_cannot_be_worked(as_user):
    as_user(PHLEB)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(router_mod, "_phleb_account", AsyncMock(return_value={"id": "p1"})), \
         patch.object(hc, "get_visit", AsyncMock(return_value=_visit(status="cancelled"))):
        r = client.post(f"/admin/home-collection/visits/{VID}/status", json={"status": "en_route"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 409


def test_front_desk_without_the_grant_cannot_manage(as_user):
    as_user(AdminUser(username="d", role="staff", clinic_id=CLINIC, user_id="d1", staff_role="STAFF"))
    r = client.get("/admin/home-collection/visits", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_another_clinics_admin_cannot_reach_this_clinic(as_user):
    as_user(AdminUser(username="x", role="clinic_admin", clinic_id="99999999-9999-9999-9999-999999999999", user_id="x"))
    r = client.get("/admin/home-collection/visits", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_a_non_diagnostic_plan_is_refused(as_user):
    as_user(AdminUser(username="a", role="clinic_admin", clinic_id=CLINIC, user_id="a1"))
    with patch.object(router_mod, "get_clinic_by_id", AsyncMock(return_value={"id": CLINIC, "plan": "polyclinic"})):
        r = client.get("/admin/home-collection/visits", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_assigning_a_phlebotomist_from_another_branch_is_refused(as_user):
    as_user(AdminUser(username="a", role="clinic_admin", clinic_id=CLINIC, user_id="a1"))
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(hc, "get_visit", AsyncMock(return_value=_visit(branch_id="b1"))), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[{**P2, "id": "p2"}])), \
         patch.object(hc, "assign", AsyncMock()) as assign:
        r = client.post(f"/admin/home-collection/visits/{VID}/assign", json={"phlebotomist_id": "p2"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 422
    assign.assert_not_awaited()


# ═══════ staff accounts ═══════


def test_a_phlebotomist_needs_a_name_and_mobile():
    from fastapi import HTTPException
    from app.routers.admin import _staff_contact

    with pytest.raises(HTTPException):
        _staff_contact("Suresh", None, required=True)
    with pytest.raises(HTTPException):
        _staff_contact("Suresh", "12345", required=True)
    assert _staff_contact(" Suresh  Kumar ", "98480 22338", required=True) == {
        "full_name": "Suresh Kumar", "phone": "+919848022338"}
    assert _staff_contact(None, None, required=False) == {}
