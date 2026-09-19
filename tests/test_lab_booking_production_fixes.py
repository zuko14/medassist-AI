"""Production defects found on the Accumx Diagnostics WhatsApp line (2026-09-08).

A patient picked "(1,3)-BETA-D-GLUCAN", chose a collection date, and got
"We couldn't initialize your booking." Both lab bookings that evening landed
in `appointments` as status='cancelled' with a NULL razorpay_payment_link_id,
and payment_events recorded a Razorpay 401 for each: the centre has no
Razorpay keys, and the lab-test flow asked for a payment link anyway.

Investigating that turned up four more defects on the same flow, all covered
here:

  * get_lab_tests() returned 1000 of the centre's 1392 active tests, because
    PostgREST caps a response at 1000 rows and the search filters that list in
    Python. 7 of the 10 "thyroid" tests -- the example the bot itself prints --
    were unreachable, as were both "widal" tests.
  * book_appointment() indexed data["doctor_id"] unconditionally, so the free
    booking path this fix routes lab tests through raised KeyError.
  * send_2h_reminders() sliced appointment_time outside its per-appointment
    try block. Lab bookings have a NULL appointment_time, so the first
    confirmed lab booking would abort the reminder sweep for every clinic on
    the platform.
  * get_patient_queue_status() keyed the collection queue on doctor_name,
    which is NULL for a lab booking, so every waiting patient was told nobody
    was ahead of them and the reply read "Doctor: None".
"""

import os
import sys
import pytest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Diagnostics")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "Accumax Diagnostics")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "AD")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from app.services.conversation import ConversationManager  # noqa: E402

CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111", "name": "Accumax"}
PHONE = "+919999999999"

# What PostgREST enforces server-side regardless of what the client asks for.
POSTGREST_MAX_ROWS = 1000


# ── Defect 1: the catalogue is truncated at the PostgREST row cap ────────────

class _FakeCatalogueQuery:
    """Stands in for a PostgREST builder that will not return more than
    POSTGREST_MAX_ROWS however wide a range is requested."""

    def __init__(self, rows):
        self._rows = rows
        self._lo = 0
        self._hi = POSTGREST_MAX_ROWS - 1

    def eq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def result(self):
        wanted = self._hi - self._lo + 1
        return self._rows[self._lo : self._lo + min(wanted, POSTGREST_MAX_ROWS)]


def _catalogue(n):
    return [
        {
            "id": f"test-{i:04d}",
            "name": f"TEST {i:04d}",
            "price_paise": 10000,
            "is_active": True,
            "branch_id": None,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_get_lab_tests_returns_the_whole_catalogue_past_the_row_cap():
    """Accumx has 1392 active tests. Returning 1000 stranded 392 of them --
    including "widal", which no patient could then find or book."""
    from app import database

    rows = _catalogue(1392)

    async def fake_sb(query):
        return SimpleNamespace(data=query.result())

    with patch.object(
        database, "scoped_query", side_effect=lambda *a, **k: _FakeCatalogueQuery(rows)
    ), patch.object(database, "sb", side_effect=fake_sb):
        tests = await database.get_lab_tests("clinic-1")

    assert len(tests) == 1392
    assert tests[-1]["name"] == "TEST 1391"


@pytest.mark.asyncio
async def test_get_lab_tests_makes_one_round_trip_for_a_small_catalogue():
    """A clinic with 40 tests must not pay for extra pages."""
    from app import database

    rows = _catalogue(40)
    calls = []

    async def fake_sb(query):
        calls.append(query)
        return SimpleNamespace(data=query.result())

    with patch.object(
        database, "scoped_query", side_effect=lambda *a, **k: _FakeCatalogueQuery(rows)
    ), patch.object(database, "sb", side_effect=fake_sb):
        tests = await database.get_lab_tests("clinic-1")

    assert len(tests) == 40
    assert len(calls) == 1


# ── Defect 2: book_appointment could not write a lab booking ────────────────

@pytest.mark.asyncio
async def test_book_appointment_accepts_a_lab_test_without_a_doctor():
    """The slot pre-check indexed data["doctor_id"], which a lab booking never
    carries. The KeyError surfaced to the patient as a generic failure."""
    from app import database

    inserted = {}

    async def fake_sb(builder):
        return SimpleNamespace(data=[{"id": "appt-1", "booking_ref": "AD-2026-ABCD1234"}])

    mock_table = MagicMock()

    def capture_insert(payload):
        inserted.update(payload)
        return mock_table

    mock_table.insert.side_effect = capture_insert

    with patch.object(database, "supabase") as mock_supabase, patch.object(
        database, "sb", side_effect=fake_sb
    ), patch.object(
        database, "get_patient_by_phone", new_callable=AsyncMock, return_value=None
    ):
        mock_supabase.table.return_value = mock_table
        result = await database.book_appointment(
            "clinic-1",
            {
                "patient_phone": PHONE,
                "patient_name": "Test Patient",
                "department": "Lab Test",
                "doctor_name": None,
                "appointment_date": "2026-09-10",
                "appointment_time": None,
                "status": "confirmed",
                "booking_type": "lab_test",
                "lab_test_id": "test-0001",
                "lab_test_name": "(1,3)-BETA-D-GLUCAN",
            },
        )

    assert result["success"] is True
    assert inserted["booking_type"] == "lab_test"
    assert inserted["appointment_time"] is None


@pytest.mark.asyncio
async def test_book_appointment_still_refuses_a_consultation_with_no_doctor():
    """Regression guard on KA-P0-01: the doctor_id requirement for
    consultations must survive the lab-test carve-out."""
    from app import database

    with patch.object(
        database, "get_doctor_by_name", new_callable=AsyncMock, return_value=None
    ):
        result = await database.book_appointment(
            "clinic-1",
            {
                "patient_phone": PHONE,
                "patient_name": "Test Patient",
                "doctor_name": "Dr. Nobody",
                "appointment_date": "2026-09-10",
                "appointment_time": "10:00",
                "status": "confirmed",
            },
        )

    assert result["success"] is False
    assert result["reason"] == "doctor_unavailable"


# ── Defect 3: the lab flow ignored the clinic's payment mode ────────────────

def _lab_context():
    return {
        "lab_test_id": "test-0001",
        "lab_test_name": "(1,3)-BETA-D-GLUCAN",
        "lab_test_price_paise": 1200000,
        "lab_collection_date": "2026-09-10",
        "lab_step": "who",
        "branch_id": None,
        "branch_name": None,
    }


@pytest.mark.asyncio
async def test_lab_booking_confirms_directly_when_the_centre_has_no_razorpay_keys():
    """The production failure. A centre with no Razorpay credentials must book
    the test and collect at the counter, exactly as a consultation does, rather
    than request a payment link and hand the patient a 401."""
    manager = ConversationManager()
    context = _lab_context()

    booked = {"success": True, "appointment": {"id": "appt-1", "booking_ref": "AD-2026-K4M2P8QN"}}

    with patch(
        "app.services.payment.resolve_payment_mode", return_value=("none", 100)
    ), patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
    ) as mock_paid_booking, patch(
        "app.services.conversation.book_appointment",
        new_callable=AsyncMock,
        return_value=booked,
    ) as mock_book, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send_text, patch.object(
        manager, "_send_main_menu", new_callable=AsyncMock
    ), patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update_state:
        await manager._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", context, {"id": "patient-1", "name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_self"},
        )

    mock_paid_booking.assert_not_called()
    mock_book.assert_called_once()

    written = mock_book.call_args[0][1]
    assert written["booking_type"] == "lab_test"
    assert written["status"] == "confirmed"
    assert written["appointment_time"] is None
    assert written["doctor_name"] is None
    assert written["lab_test_id"] == "test-0001"

    confirmation = mock_send_text.call_args[0][2]
    assert "AD-2026-K4M2P8QN" in confirmation
    assert "12000" in confirmation

    assert mock_update_state.call_args[0][2] != "awaiting_payment"


@pytest.mark.asyncio
async def test_lab_booking_still_takes_the_payment_path_when_keys_are_configured():
    """Regression guard: centres that DO collect online must keep doing so."""
    manager = ConversationManager()
    context = _lab_context()

    paid = {
        "success": True,
        "booking_id": "booking-1",
        "booking_ref": "AD-2026-1000",
        "payment_link": "https://rzp.io/i/xyz",
        "amount_paise": 1200000,
        "hold_expires_at": "2026-09-10T13:00:00Z",
    }

    with patch(
        "app.services.payment.resolve_payment_mode", return_value=("full", 100)
    ), patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
        return_value=paid,
    ) as mock_paid_booking, patch(
        "app.services.conversation.book_appointment", new_callable=AsyncMock
    ) as mock_book, patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send_text, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ) as mock_update_state:
        await manager._handle_confirming_collection_date(
            CLINIC, PHONE, "", "", context, {"id": "patient-1", "name": "Ravi Kumar"}, "en",
            interactive_data={"id": "labfor_self"},
        )

    mock_paid_booking.assert_called_once()
    mock_book.assert_not_called()
    assert "https://rzp.io/i/xyz" in mock_send_text.call_args[0][2]
    assert mock_update_state.call_args[0][2] == "awaiting_payment"


# ── Defect 4: one lab booking aborted the platform-wide reminder sweep ──────

@asynccontextmanager
async def _granted_lock(*_args, **_kwargs):
    """The sweeps run under a Postgres advisory lock. Grant it so the test
    exercises the loop rather than the early return."""
    yield True


@pytest.mark.asyncio
async def test_2h_reminder_sweep_survives_a_booking_with_no_appointment_time():
    """A confirmed lab booking has appointment_time NULL. Slicing it raised
    TypeError outside the per-appointment try, killing the sweep for every
    clinic. The consultation behind it must still get its reminder."""
    from app.services.scheduler import SchedulerService

    rows = [
        {
            "id": "appt-lab",
            "clinic_id": "clinic-1",
            "doctor_name": None,
            "patient_phone": PHONE,
            "appointment_date": "2026-09-10",
            "appointment_time": None,
            "booking_type": "lab_test",
            "reminder_2h_sent": False,
        },
        {
            "id": "appt-consult",
            "clinic_id": "clinic-1",
            "doctor_name": "Dr. Rao",
            "patient_phone": "+919888888888",
            "appointment_date": "2026-09-10",
            "appointment_time": "00:01",
            "booking_type": "consultation",
            "reminder_2h_sent": False,
        },
    ]

    mock_appointments = MagicMock()
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.execute.return_value.data = rows
    mock_appointments.select.return_value = mock_select
    mock_appointments.update.return_value.eq.return_value.execute.return_value.data = [{}]

    def table_router(name):
        if name == "appointments":
            return mock_appointments
        t = MagicMock()
        t.select.return_value.eq.return_value.execute.return_value.data = []
        return t

    with patch("app.services.scheduler.supabase.table", side_effect=table_router), patch(
        "app.services.distributed_lock.distributed_job_lock", _granted_lock
    ), patch(
        "app.services.scheduler.get_clinic_by_id",
        new_callable=AsyncMock,
        return_value={"id": "clinic-1", "name": "Accumax"},
    ), patch("app.services.tenant.has_feature", return_value=True), patch(
        "app.services.scheduler.automated_outbound_allowed", return_value=True
    ), patch(
        "app.services.scheduler.whatsapp_service.send_template", new_callable=AsyncMock
    ) as mock_send_tpl:
        await SchedulerService().send_2h_reminders()

    sent_to = [c.args[1] for c in mock_send_tpl.call_args_list]
    assert sent_to == ["+919888888888"]


# ── Defect 5: rows that truncate to the same 24 characters ─────────────────

@pytest.mark.asyncio
async def test_rows_whose_names_truncate_identically_stay_distinguishable():
    """57 groups in the Accumx catalogue share their first 24 characters, and
    21 of those are identical on price and sample type too -- so the row the
    patient saw twice, "17-HYDROXYPROGESTERONE (", carried no signal at all.
    These are the real names and price behind the reported screenshot."""
    manager = ConversationManager()
    tests = [
        {
            "id": "t1",
            "name": "17-HYDROXYPROGESTERONE ( 17-OHP) STIMULATION BY ACTH",
            "price_paise": 180000,
            "sample_type": None,
        },
        {
            "id": "t2",
            "name": "17-HYDROXYPROGESTERONE (17-OHP), NEWBORN SCREEN CAH SCREEN",
            "price_paise": 180000,
            "sample_type": None,
        },
    ]

    captured = {}

    async def capture_list(clinic, phone, body, button_text, sections, **kwargs):
        captured["sections"] = sections
        return True

    with patch(
        "app.database.get_lab_tests", new_callable=AsyncMock, return_value=tests
    ), patch.object(
        manager.whatsapp, "send_interactive_list", side_effect=capture_list
    ), patch.object(
        manager, "update_state", new_callable=AsyncMock
    ):
        await manager._show_lab_test_list(CLINIC, PHONE, {}, "en")

    rows = captured["sections"][0]["rows"]
    # Two tests plus the trailing Main Menu row every catalogue list carries.
    assert rows[-1]["id"] == "lab_menu"
    rows = rows[:-1]
    assert len(rows) == 2
    titles = [r["title"] for r in rows]
    assert titles[0] == titles[1], "precondition: these names truncate identically"

    labels = [f"{r['title']}|{r['description']}" for r in rows]
    assert labels[0] != labels[1]
    assert "STIMULATION" in labels[0].upper()
    assert "NEWBORN" in labels[1].upper()


# -- Defect 6: the sample-collection queue was keyed on a doctor that is NULL -

@pytest.mark.asyncio
async def test_lab_queue_status_uses_the_collection_queue_not_a_null_doctor():
    """check_in_appointment learned that a lab row is keyed on its branch;
    get_patient_queue_status never did. It filtered on doctor_name = NULL,
    which PostgREST never matches, so every waiting patient was told nobody
    was ahead of them -- and the reply said "Doctor: None"."""
    from app import database

    booking = {
        "id": "appt-lab",
        "booking_type": "lab_test",
        "doctor_name": None,
        "branch_id": "branch-1",
        "lab_test_name": "(1,3)-BETA-D-GLUCAN",
        "token_number": 7,
        "appointment_date": "2026-09-10",
    }
    filters = []

    class _Q:
        def eq(self, col, val):
            filters.append(("eq", col, val))
            return self

        def is_(self, col, val):
            filters.append(("is", col, val))
            return self

        def in_(self, *_a):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a):
            return self

    calls = {"n": 0}

    async def fake_sb(_q):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(data=[booking])
        return SimpleNamespace(data=[{"token_number": 4}])

    with patch.object(database, "scoped_query", side_effect=lambda *a, **k: _Q()), patch.object(
        database, "sb", side_effect=fake_sb
    ):
        status = await database.get_patient_queue_status("clinic-1", PHONE, "2026-09-10")

    assert ("eq", "doctor_name", None) not in filters
    assert ("is", "doctor_name", "null") in filters
    assert ("eq", "branch_id", "branch-1") in filters

    assert status["is_lab_test"] is True
    assert status["doctor_name"] == "(1,3)-BETA-D-GLUCAN"
    assert status["patients_ahead"] == 3


@pytest.mark.asyncio
async def test_lab_queue_reply_never_says_doctor_none():
    """The patient-facing half of the same defect."""
    manager = ConversationManager()

    with patch(
        "app.services.conversation.get_patient_queue_status",
        new_callable=AsyncMock,
        return_value={
            "checked_in": True,
            "is_lab_test": True,
            "token_number": 7,
            "currently_serving": 4,
            "patients_ahead": 3,
            "doctor_name": "(1,3)-BETA-D-GLUCAN",
        },
    ), patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ) as mock_send_text, patch(
        "app.services.conversation.log_analytics_event", new_callable=AsyncMock
    ):
        await manager._handle_queue_status(CLINIC, PHONE, "en")

    reply = mock_send_text.call_args[0][2]
    assert "None" not in reply
    assert "(1,3)-BETA-D-GLUCAN" in reply
    assert "7" in reply


# -- A booking nobody has paid for yet must read as pending, not as blank ----

def test_a_booked_test_with_no_payment_taken_reads_as_pending():
    """A centre that collects at the counter still has money outstanding. The
    bookings list showed an empty Payment ID cell, which is indistinguishable
    from a free booking, so the front desk had no way to see what to collect."""
    from app.routers.admin import _annotate_payment_state

    rows = [
        {"id": "a", "status": "confirmed", "amount_paise": 1200000, "payment_id": None},
        {"id": "b", "status": "confirmed", "amount_paise": 1200000, "payment_id": "pay_x"},
        {"id": "c", "status": "cancelled", "amount_paise": 1200000, "payment_id": None},
        {"id": "d", "status": "confirmed", "amount_paise": 0, "payment_id": None},
    ]
    _annotate_payment_state(rows)

    assert [r["payment_state"] for r in rows] == [
        "pending",
        "paid",
        "not_required",
        "not_required",
    ]
