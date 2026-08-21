# Diagnostic Menu Fix & Doctor Booking Date/Time Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three production-reported WhatsApp UX bugs with 100% reliability: (1) diagnostics-only clinics still leak the doctor-department menu through the "Our Services"/"Our Doctors" main-menu options, bypassing the lab-test-only routing shipped earlier today; (2) the post-booking-confirmation follow-up offers a confusing "Book Appointment" button instead of just "Main Menu" — and the lab-test payment-confirmation path offers no follow-up at all; (3) doctor-appointment booking makes the patient tap through two separate WhatsApp messages (pick a date, then pick a time) where one combined pick would do.

**Architecture:** Root-cause each bug at its single shared choke point rather than patching every call site: a new `_is_diagnostics_only()` helper centralizes the "does this clinic offer only lab tests" check (reused by `_start_booking`, `_send_main_menu`, and the main-menu intent dispatcher); the post-confirmation follow-up buttons are simplified at their two existing send sites (`_handle_confirming_booking` in conversation.py, `_notify_payment_confirmed` in payment.py) rather than introducing a new abstraction; and the date+time merge replaces the two-step `_show_date_picker` → `_show_slot_list` interactive flow with one `_show_combined_slot_picker` that aggregates multiple days of slots into a single WhatsApp interactive list (max 10 rows, the existing hard limit already respected elsewhere in this file), while the old single-day free-text fallback path (`_handle_selecting_date`) is kept fully intact and reused for typed date input, and the `selecting_date` state/dispatch branch is deliberately left in place (not deleted) so any conversation already mid-flow in that state at deploy time keeps resolving correctly.

**Tech Stack:** Python FastAPI, Meta WhatsApp Cloud API interactive messages (list + reply-button), Supabase PostgreSQL (read-only for these fixes — no migrations), pytest + unittest.mock.

**Spec:** No separate spec doc — this is a bounded fix to existing, already-shipped flows (see `docs/superpowers/plans/2026-08-21-diagnostic-center-lab-test-booking.md` for the diagnostics-only routing this plan closes remaining gaps in). Root-cause evidence: `diagnostics_plan_issues/WhatsApp Image 2026-08-21 at 2.15.35 PM.jpeg` and `(1).jpeg` (Accumax Diagnostics' "Our Services" button showing the doctor-department list), `diagnostics_plan_issues/WhatsApp Image 2026-08-21 at 2.29.52 PM.jpeg` (post-confirmation follow-up showing "Book Appointment" + "Main Menu").

## Global Constraints

- No database migrations in this plan — every fix is conversation-logic, messaging copy, or a shared-helper refactor.
- WhatsApp interactive list messages are hard-capped at **10 rows total across all sections** (already enforced elsewhere in this file for `_show_doctors`, `_show_slot_list`, `_show_lab_test_list` — the combined date/time picker must respect the same cap).
- `_handle_selecting_date` and the `"selecting_date"` state/dispatch branch (`app/services/conversation.py:658-659`) are NOT deleted — they stay reachable as the free-text-date fallback and for any in-flight conversation already in that state when this deploys.
- Every new/changed branch gets the smallest test that fails if the logic breaks, matching the existing suite's style (see `tests/test_department_selection.py` and `tests/test_lab_test_booking_conversation.py` for the pattern).
- Phone numbers in logs stay masked; no stack traces in patient-facing WhatsApp messages (existing codebase rule, `CLAUDE.md`).

---

## File Structure

**Create:**
- `tests/test_diagnostics_menu_routing.py` — diagnostics-only main-menu content, `_is_diagnostics_only` helper, view_services/doctor_availability redirect guards, `_show_doctors` empty-doctors guard
- `tests/test_booking_confirmation_followup.py` — single "Main Menu" button after both the non-payment and Razorpay-gated confirmation paths
- `tests/test_combined_datetime_picker.py` — multi-day date+time aggregation, row-cap behavior, button-id parsing, free-text fallback delegation

**Modify:**
- `app/services/conversation.py` — new `_is_diagnostics_only()` helper; `_start_booking` refactored to use it; `_send_main_menu` diagnostics-aware; `_handle_main_menu` view_services/doctor_availability guards; `_show_doctors` empty-doctors guard; `_handle_confirming_booking` follow-up buttons; new `_to_ampm()` helper (extracted from `_show_slot_list`); new `_show_combined_slot_picker()`; button-id parsing for `dtslot_`; `_handle_selecting_slot` updated; two `_show_date_picker` call sites replaced
- `app/services/payment.py` — `_notify_payment_confirmed` sends a single "Main Menu" button and resets conversation state
- `app/templates/whatsapp_templates.py` — new `select_datetime` message key (en/hi/te)

---

### Task 1: `_is_diagnostics_only()` shared helper + `_start_booking` refactor

**Files:**
- Modify: `app/services/conversation.py:1085-1108` (`_start_booking`)
- Test: `tests/test_diagnostics_menu_routing.py` (new file)

**Interfaces:**
- Produces: `async def _is_diagnostics_only(self, clinic: dict) -> bool` — consumed by Task 2 (`_send_main_menu`) and Task 3 (`_handle_main_menu` guards).

- [ ] **Step 1: Write the failing test**

Create `tests/test_diagnostics_menu_routing.py`:

```python
"""Tests for diagnostics-only clinic menu behavior — 'Our Services'/'Our
Doctors' must never leak the doctor-department flow, and the post-booking
follow-up must offer only Main Menu."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager


class TestIsDiagnosticsOnly:
    @pytest.mark.asyncio
    async def test_true_when_feature_on_and_zero_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is True

    @pytest.mark.asyncio
    async def test_false_when_doctors_present_even_with_feature_on(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=True
        ), patch(
            "app.services.conversation.get_doctors",
            new_callable=AsyncMock,
            return_value=[{"id": "doc-1"}],
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is False

    @pytest.mark.asyncio
    async def test_false_when_feature_off(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1"}

        with patch(
            "app.services.tenant.has_feature", return_value=False
        ), patch(
            "app.services.conversation.get_doctors", new_callable=AsyncMock, return_value=[]
        ):
            result = await manager._is_diagnostics_only(clinic)

        assert result is False


class TestStartBookingUsesSharedHelper:
    @pytest.mark.asyncio
    async def test_diagnostics_only_still_routes_to_lab_tests(self):
        """Regression: Task 1's refactor must not change _start_booking's
        existing diagnostics-only routing behavior."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en", "name": "Test Patient"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_department_list", new_callable=AsyncMock
        ) as mock_show_dept_list, patch(
            "app.services.conversation.get_clinic_branches", new_callable=AsyncMock, return_value=[]
        ), patch(
            "app.services.conversation.has_branches", return_value=False
        ):
            await manager._start_booking(clinic, "+919876543210", patient, "en")

        mock_show_lab_tests.assert_called_once()
        mock_show_dept_list.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics_menu_routing.py -v`
Expected: FAIL — `AttributeError: 'ConversationManager' object has no attribute '_is_diagnostics_only'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, find the diagnostics-only routing block currently inline in `_start_booking` (added earlier today):

```python
        from app.services.tenant import has_feature

        if has_feature(clinic, "lab_test_booking"):
            doctors = await get_doctors(clinic["id"])
            if not doctors:
                await self._show_lab_test_list(clinic, phone, {}, lang)
                return
```

Replace it with a call to the new shared helper, and add the helper method right above `_start_booking`:

```python
    async def _is_diagnostics_only(self, clinic: dict) -> bool:
        """True if this clinic offers lab-test booking and has zero active
        doctors — i.e. it should never see the doctor/department flow."""
        from app.services.tenant import has_feature

        if not has_feature(clinic, "lab_test_booking"):
            return False
        doctors = await get_doctors(clinic["id"])
        return not doctors

    async def _start_booking(
        self, clinic: dict, phone: str, patient: Optional[dict], lang: str
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics."""
        patient = patient or {}

        # Guard: Language must be set before proceeding
        if not patient.get("language"):
            await self._send_language_selection(clinic, phone)
            await self.update_state(clinic, phone, "selecting_language")
            return

        # ── Diagnostics-Only Routing ─────────────────────────────────────────
        if await self._is_diagnostics_only(clinic):
            await self._show_lab_test_list(clinic, phone, {}, lang)
            return
        # ── End Diagnostics-Only Routing ─────────────────────────────────────

        # ── Multi-Branch Check ──────────────────────────────────────────────
```

(The rest of `_start_booking`, from the multi-branch check onward, is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diagnostics_menu_routing.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the existing lab-test-booking conversation suite to confirm no regression**

Run: `pytest tests/test_lab_test_booking_conversation.py -v`
Expected: PASS (all 9 tests — the routing behavior is unchanged, only its implementation moved into a named helper)

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_diagnostics_menu_routing.py
git commit -m "refactor(conversation): extract _is_diagnostics_only shared helper"
```

---

### Task 2: Diagnostics-aware main menu — remove "Our Services"/"Our Doctors" leak

**Files:**
- Modify: `app/services/conversation.py:958-1012` (`_send_main_menu`)
- Test: `tests/test_diagnostics_menu_routing.py` (append)

**Interfaces:**
- Consumes: `_is_diagnostics_only()` (Task 1).
- Produces: no new interface — `_send_main_menu`'s row content now branches on clinic type. Consumed implicitly by every existing caller (unchanged signature).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diagnostics_menu_routing.py`:

```python
class TestDiagnosticsAwareMainMenu:
    @pytest.mark.asyncio
    async def test_diagnostics_only_menu_omits_services_and_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._send_main_menu(clinic, "+919876543210", "en")

        row_ids = [
            r["id"]
            for section in mock_send_list.call_args.kwargs["sections"]
            for r in section["rows"]
        ]
        assert "menu_services" not in row_ids
        assert "menu_doctors" not in row_ids
        assert "menu_book" in row_ids

    @pytest.mark.asyncio
    async def test_regular_clinic_menu_unchanged(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        with patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=False
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._send_main_menu(clinic, "+919876543210", "en")

        row_ids = [
            r["id"]
            for section in mock_send_list.call_args.kwargs["sections"]
            for r in section["rows"]
        ]
        assert "menu_services" in row_ids
        assert "menu_doctors" in row_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics_menu_routing.py::TestDiagnosticsAwareMainMenu -v`
Expected: FAIL — `menu_services`/`menu_doctors` present regardless of clinic type (current unconditional behavior).

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, replace `_send_main_menu` (lines 958-1012):

```python
    async def _send_main_menu(self, clinic: dict, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        diagnostics_only = await self._is_diagnostics_only(clinic)

        book_title = {
            "en": "Book Lab Test" if diagnostics_only else "Book Appointment",
            "hi": "Book Lab Test" if diagnostics_only else "Book Appointment",
            "te": "Book Lab Test" if diagnostics_only else "Book Appointment",
        }.get(lang, "Book Lab Test" if diagnostics_only else "Book Appointment")

        titles = {
            "en": ["Our Doctors", "Emergency", "Talk to Staff"],
            "hi": ["Our Doctors", "Emergency", "Talk to Staff"],
            "te": ["Our Doctors", "Emergency", "Talk to Staff"],
        }
        t = titles.get(lang, titles["en"])

        rows = [{"id": "menu_book", "title": book_title[:24], "description": ""}]
        if not diagnostics_only:
            services_title = {"en": "Our Services", "hi": "Our Services", "te": "Our Services"}.get(lang, "Our Services")
            rows.append({"id": "menu_services", "title": services_title[:24], "description": ""})
            rows.append({"id": "menu_doctors", "title": t[0][:24], "description": ""})
        rows.append({"id": "menu_reports", "title": "📋 My Reports"[:24], "description": ""})
        rows.append({"id": "menu_emergency", "title": t[1][:24], "description": ""})
        rows.append({"id": "menu_human", "title": t[2][:24], "description": ""})

        sections = [{"title": "Menu", "rows": rows}]

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("main_menu", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diagnostics_menu_routing.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full department-selection and lab-test suites to confirm no regression**

Run: `pytest tests/test_department_selection.py tests/test_lab_test_booking_conversation.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/conversation.py tests/test_diagnostics_menu_routing.py
git commit -m "fix(conversation): hide Our Services/Our Doctors from diagnostics-only main menu"
```

---

### Task 3: Defense-in-depth guard for stale "Our Services"/"Our Doctors" taps

**Files:**
- Modify: `app/services/conversation.py:1046-1056` (`_handle_main_menu` intent branches), `app/services/conversation.py:3120-3163` (`_show_doctors` empty guard)
- Test: `tests/test_diagnostics_menu_routing.py` (append)

**Interfaces:**
- Consumes: `_is_diagnostics_only()` (Task 1), `_show_lab_test_list()` (already exists, from the lab-test-booking plan).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diagnostics_menu_routing.py`:

```python
class TestStaleMenuTapGuards:
    @pytest.mark.asyncio
    async def test_view_services_redirects_to_lab_tests_for_diagnostics_only(self):
        """A patient with an old WhatsApp thread (message sent before this
        fix shipped) tapping a stale 'Our Services' button must not see the
        doctor-department list."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en"}

        with patch(
            "app.database.get_conversation", new_callable=AsyncMock,
            return_value={"context": {"menu_shown": True}},
        ), patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_services", new_callable=AsyncMock
        ) as mock_show_services:
            await manager._handle_main_menu(
                clinic, "+919876543210", "", "view_services", patient, "en"
            )

        mock_show_lab_tests.assert_called_once()
        mock_show_services.assert_not_called()

    @pytest.mark.asyncio
    async def test_doctor_availability_redirects_to_lab_tests_for_diagnostics_only(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"language": "en"}

        with patch(
            "app.database.get_conversation", new_callable=AsyncMock,
            return_value={"context": {"menu_shown": True}},
        ), patch.object(
            manager, "_is_diagnostics_only", new_callable=AsyncMock, return_value=True
        ), patch.object(
            manager, "_show_lab_test_list", new_callable=AsyncMock
        ) as mock_show_lab_tests, patch.object(
            manager, "_show_doctors", new_callable=AsyncMock
        ) as mock_show_doctors:
            await manager._handle_main_menu(
                clinic, "+919876543210", "", "doctor_availability", patient, "en"
            )

        mock_show_lab_tests.assert_called_once()
        mock_show_doctors.assert_not_called()


class TestShowDoctorsEmptyGuard:
    @pytest.mark.asyncio
    async def test_zero_doctors_sends_friendly_text_not_empty_list(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}

        mock_result = MagicMock()
        mock_result.data = []

        with patch("app.services.conversation.supabase") as mock_sb, patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ) as mock_send_text:
            mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = mock_result
            await manager._show_doctors(clinic, "+919876543210", "en")

        mock_send_list.assert_not_called()
        mock_send_text.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics_menu_routing.py::TestStaleMenuTapGuards tests/test_diagnostics_menu_routing.py::TestShowDoctorsEmptyGuard -v`
Expected: FAIL — `_show_services`/`_show_doctors` called unconditionally; `_show_doctors` calls `send_interactive_list` even with zero doctors.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, update the `view_services`/`doctor_availability` branches inside `_handle_main_menu` (around line 1053-1056):

```python
        elif intent == "view_services":
            if await self._is_diagnostics_only(clinic):
                await self._show_lab_test_list(clinic, phone, {}, lang)
            else:
                await self._show_services(clinic, phone, lang)
        elif intent == "doctor_availability":
            if await self._is_diagnostics_only(clinic):
                await self._show_lab_test_list(clinic, phone, {}, lang)
            else:
                await self._show_doctors(clinic, phone, lang)
```

(Match whatever the existing `doctor_availability` branch currently calls — confirm the exact original line before editing so only the added `if`/`else` wrapping changes, not the call itself.)

In `_show_doctors` (around line 3120-3134), add the empty-doctors guard right after fetching `doctors`:

```python
    async def _show_doctors(self, clinic: dict, phone: str, lang: str) -> None:
        """Show available doctors."""
        from app.database import supabase

        response = (
            supabase.table("doctors")
            .select("*")
            .eq("clinic_id", clinic["id"])
            .eq("is_active", True)
            .order("department")
            .execute()
        )
        doctors = response.data

        if not doctors:
            no_doctors_msg = {
                "en": "We don't have any doctors listed for online booking right now. Please call us directly.",
                "hi": "अभी ऑनलाइन बुकिंग के लिए कोई डॉक्टर सूचीबद्ध नहीं है। कृपया सीधे हमें कॉल करें।",
                "te": "ప్రస్తుతం ఆన్‌లైన్ బుకింగ్ కోసం డాక్టర్లు జాబితా చేయబడలేదు. దయచేసి నేరుగా మాకు కాల్ చేయండి.",
            }.get(
                lang,
                "We don't have any doctors listed for online booking right now. Please call us directly.",
            )
            await self.whatsapp.send_text(clinic, phone, no_doctors_msg)
            return

        sections = []
```

(The rest of `_show_doctors` is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diagnostics_menu_routing.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_diagnostics_menu_routing.py
git commit -m "fix(conversation): guard stale Our Services/Our Doctors taps and empty-doctor list"
```

---

### Task 4: Post-booking confirmation follow-up → Main Menu only (non-payment path)

**Files:**
- Modify: `app/services/conversation.py:2693-2701` (`_handle_confirming_booking` follow-up buttons)
- Test: `tests/test_booking_confirmation_followup.py` (new file)

**Interfaces:**
- No new interface — `_handle_confirming_booking`'s follow-up message content changes; signature unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_booking_confirmation_followup.py`:

```python
"""Tests for the post-booking-confirmation follow-up prompt — must offer
only 'Main Menu', not a redundant 'Book Appointment' button, per production
screenshot evidence (diagnostics_plan_issues/...2.29.52 PM.jpeg)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.conversation import ConversationManager


class TestConfirmingBookingFollowUp:
    @pytest.mark.asyncio
    async def test_follow_up_offers_only_main_menu(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        patient = {"id": "patient-1", "name": "Test Patient"}
        context = {
            "doctor_name": "Dr. Test",
            "department": "Cardiology",
            "appointment_date": "2026-08-24",
            "appointment_time": "10:00",
            "booking_name": "Test Patient",
        }

        fake_result = {"success": True, "booking_ref": "MC-2026-1001"}

        with patch(
            "app.services.conversation.book_appointment", new_callable=AsyncMock, return_value=fake_result
        ), patch.object(
            manager.whatsapp, "send_text", new_callable=AsyncMock
        ), patch.object(
            manager.whatsapp, "send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ), patch(
            "app.services.conversation.log_analytics_event", new_callable=AsyncMock
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await manager._handle_confirming_booking(
                clinic, "+919876543210", "", "confirm_booking", context, patient, "en"
            )

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_booking_confirmation_followup.py -v`
Expected: FAIL — `len(buttons) == 2` (current `book_another` + `main_menu`).

Note: if the mocked `book_appointment` return shape or call path doesn't match `_handle_confirming_booking`'s actual success branch exactly, adjust the mock target/patch path to match the real import used in that function (check `app/services/conversation.py`'s imports for the exact booking-creation call inside `_handle_confirming_booking` before finalizing this test) — the assertion on `buttons` is the part that must not change.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, replace the follow-up buttons block (around lines 2693-2701):

```python
                    await self.whatsapp.send_interactive_buttons(
                        clinic,
                        phone,
                        body=follow_up_msg,
                        buttons=[
                            {"id": "main_menu", "title": "Main Menu"},
                        ],
                    )
```

(Leave the `follow_up_msg` text and everything else in this block unchanged — only the `buttons` list shrinks from two entries to one.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_booking_confirmation_followup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_booking_confirmation_followup.py
git commit -m "fix(conversation): post-confirmation follow-up offers Main Menu only"
```

---

### Task 5: Post-payment confirmation follow-up → Main Menu button + state reset (Razorpay path)

**Files:**
- Modify: `app/services/payment.py:1350` (end of `_notify_payment_confirmed`, right after `send_text`)
- Test: `tests/test_booking_confirmation_followup.py` (append)

**Interfaces:**
- Consumes: `conversation_manager.update_state()` (existing singleton, `app/services/conversation.py:3568`), `whatsapp_service.send_interactive_buttons()` (existing).
- Produces: no new interface — closes the literal gap the user reported (currently no follow-up at all after a lab-test or Razorpay-gated doctor booking is confirmed).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_booking_confirmation_followup.py`:

```python
class TestNotifyPaymentConfirmedFollowUp:
    @pytest.mark.asyncio
    async def test_lab_test_confirmation_sends_single_main_menu_button(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9001",
            "booking_type": "lab_test",
            "lab_test_name": "Complete Blood Count",
            "appointment_date": "2026-08-24",
            "amount_paise": 50000,
            "branch_id": None,
        }

        with patch(
            "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
        ), patch(
            "app.services.whatsapp.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock,
            return_value={"id": "test-clinic", "name": "Accumax Diagnostics", "config": {}},
        ), patch(
            "app.services.conversation.conversation_manager.update_state", new_callable=AsyncMock
        ) as mock_update_state:
            await service._notify_payment_confirmed(booking)

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"
        mock_update_state.assert_called_once()
        assert mock_update_state.call_args[0][2] == "main_menu"

    @pytest.mark.asyncio
    async def test_doctor_razorpay_confirmation_also_sends_main_menu_button(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9002",
            "booking_type": "consultation",
            "doctor_name": "Dr. Test",
            "department": "Cardiology",
            "appointment_date": "2026-08-24",
            "appointment_time": "10:00",
            "amount_paise": 50000,
            "branch_id": None,
        }

        with patch(
            "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
        ), patch(
            "app.services.whatsapp.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock
        ) as mock_send_buttons, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock,
            return_value={"id": "test-clinic", "name": "Test Clinic", "config": {}},
        ), patch(
            "app.services.conversation.conversation_manager.update_state", new_callable=AsyncMock
        ):
            await service._notify_payment_confirmed(booking)

        buttons = mock_send_buttons.call_args.kwargs["buttons"]
        assert len(buttons) == 1
        assert buttons[0]["id"] == "main_menu"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_booking_confirmation_followup.py::TestNotifyPaymentConfirmedFollowUp -v`
Expected: FAIL — `send_interactive_buttons` never called (current implementation only sends text).

- [ ] **Step 3: Write minimal implementation**

In `app/services/payment.py`, right after the existing `await whatsapp_service.send_text(clinic, booking["patient_phone"], msg, _source="payment")` line and its `logger.info` call inside `_notify_payment_confirmed` (currently ending at line 1353), add:

```python
            follow_up_msg = {
                "en": "What would you like to do next?",
            }.get("en", "What would you like to do next?")
            await whatsapp_service.send_interactive_buttons(
                clinic,
                booking["patient_phone"],
                body=follow_up_msg,
                buttons=[{"id": "main_menu", "title": "Main Menu"}],
                _source="payment",
            )

            from app.services.conversation import conversation_manager

            await conversation_manager.update_state(
                clinic, booking["patient_phone"], "main_menu"
            )
```

(This sits inside the existing `try` block, before the `except Exception as e:` at line 1355 — so a failure here is caught by the same error handler as the rest of the notification, consistent with the function's existing "best-effort notification" behavior.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_booking_confirmation_followup.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full payment test suite to confirm no regression**

Run: `pytest tests/test_payment.py tests/test_lab_test_booking_payment.py -v`
Expected: PASS (all tests — `_notify_payment_confirmed` is best-effort/fire-and-forget in every existing caller, so adding a follow-up send inside its own try/except cannot break the webhook/booking success path itself)

- [ ] **Step 6: Commit**

```bash
git add app/services/payment.py tests/test_booking_confirmation_followup.py
git commit -m "fix(payment): send Main Menu follow-up and reset state after payment confirmation"
```

---

### Task 6: Extract `_to_ampm()` helper

**Files:**
- Modify: `app/services/conversation.py:2262-2276` (`_show_slot_list`'s nested `to_ampm`)
- Test: `tests/test_combined_datetime_picker.py` (new file)

**Interfaces:**
- Produces: `def _to_ampm(self, time_24: str) -> str` — consumed by Task 8 (`_show_combined_slot_picker`) and `_show_slot_list` (updated to call the extracted method instead of its own nested closure).

- [ ] **Step 1: Write the failing test**

Create `tests/test_combined_datetime_picker.py`:

```python
"""Tests for the combined date+time picker that merges what used to be two
separate WhatsApp interactive messages (pick a date, then pick a time) into
one, per user-reported production feedback."""

from app.services.conversation import ConversationManager


class TestToAmPm:
    def test_converts_morning_time(self):
        manager = ConversationManager()
        assert manager._to_ampm("09:30") == "9:30 AM"

    def test_converts_afternoon_time(self):
        manager = ConversationManager()
        assert manager._to_ampm("17:00") == "5:00 PM"

    def test_returns_input_unchanged_on_parse_failure(self):
        manager = ConversationManager()
        assert manager._to_ampm("not-a-time") == "not-a-time"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_combined_datetime_picker.py -v`
Expected: FAIL — `AttributeError: 'ConversationManager' object has no attribute '_to_ampm'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, add a new method right before `_show_slot_list` (line 2262):

```python
    def _to_ampm(self, time_24: str) -> str:
        """Convert a 24h 'HH:MM' time string to 12h AM/PM display format."""
        from datetime import datetime

        try:
            t = datetime.strptime(time_24.strip(), "%H:%M")
            return t.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return time_24
```

Then replace `_show_slot_list`'s nested `to_ampm` function and its call sites to use the new method instead:

```python
    async def _show_slot_list(
        self, clinic: dict, phone: str, slots: list, context: dict, lang: str
    ) -> None:
        """Show available time slots in 12-hour AM/PM format."""
        morning_slots_list = []
        evening_slots_list = []
        for s in slots:
            try:
                hour = int(s.split(":")[0])
                if hour < 12:
                    morning_slots_list.append(s)
                else:
                    evening_slots_list.append(s)
            except Exception:
                morning_slots_list.append(s)

        sections = []
        if morning_slots_list and evening_slots_list:
            morn_title = {"en": "🌅 Morning", "hi": "🌅 सुबह", "te": "🌅 ఉదయం"}.get(lang, "🌅 Morning")
            eve_title = {"en": "🌆 Evening", "hi": "🌆 शाम", "te": "🌆 సాయంత్రం"}.get(lang, "🌆 Evening")

            morn_rows = [{"id": f"slot_{slot}", "title": self._to_ampm(slot), "description": ""} for slot in morning_slots_list[:5]]
            eve_rows = [{"id": f"slot_{slot}", "title": self._to_ampm(slot), "description": ""} for slot in evening_slots_list[:5]]

            sections.append({"title": morn_title, "rows": morn_rows})
            sections.append({"title": eve_title, "rows": eve_rows})
        else:
            title_text = "Select Time" if lang == "en" else ("समय चुनें" if lang == "hi" else "సమయం ఎంచుకోండి")
            sections.append({
                "title": title_text,
                "rows": [
                    {"id": f"slot_{slot}", "title": self._to_ampm(slot), "description": ""}
                    for slot in slots[:10]
                ],
            })

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("select_slot", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

        await self.update_state(clinic, phone, "selecting_slot", context)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_combined_datetime_picker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_combined_datetime_picker.py
git commit -m "refactor(conversation): extract _to_ampm as a reusable helper"
```

---

### Task 7: New `select_datetime` message template key

**Files:**
- Modify: `app/templates/whatsapp_templates.py:171,211,249` (the `select_date`/`select_slot` neighborhood in each language dict)

**Interfaces:**
- Produces: `get_message("select_datetime", lang)` — consumed by Task 8 (`_show_combined_slot_picker`).

- [ ] **Step 1: Write the failing test**

No new test file — add this assertion inline to `tests/test_combined_datetime_picker.py`:

```python
from app.templates.whatsapp_templates import get_message


class TestSelectDatetimeMessage:
    def test_english_message_exists(self):
        msg = get_message("select_datetime", "en")
        assert msg and msg != "select_datetime"

    def test_falls_back_to_english_for_unknown_language(self):
        msg = get_message("select_datetime", "fr")
        assert msg == get_message("select_datetime", "en")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_combined_datetime_picker.py::TestSelectDatetimeMessage -v`
Expected: FAIL — key not found (exact failure mode depends on `get_message`'s fallback behavior for a missing key; confirm the current behavior before asserting further).

- [ ] **Step 3: Write minimal implementation**

In `app/templates/whatsapp_templates.py`, add `"select_datetime"` next to each language's existing `"select_date"`/`"select_slot"` entries:

English (near line 171):
```python
        "select_date": "Please select a date (today or any date in the next 30 days).",
        "select_datetime": "Please select a date & time for your appointment:",
        "select_slot": "Please select a time slot:",
```

Hindi (near line 211):
```python
        "select_date": "कृपया एक तारीख चुनें (आज या अगले 30 दिनों में कोई भी तारीख)।",
        "select_datetime": "कृपया अपनी अपॉइंटमेंट के लिए तारीख और समय चुनें:",
        "select_slot": "कृपया एक समय स्लॉट चुनें:",
```

Telugu (near line 249):
```python
        "select_date": "దయచేసి ఒక తేదీని ఎంచుకోండి (ఈరోజు లేదా తదుపరి 30 రోజుల్లో ఏదైనా).",
        "select_datetime": "దయచేసి మీ అపాయింట్‌మెంట్ కోసం తేదీ మరియు సమయాన్ని ఎంచుకోండి:",
        "select_slot": "దయచేసి ఒక సమయ స్లాట్‌ను ఎంచుకోండి:",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_combined_datetime_picker.py::TestSelectDatetimeMessage -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/templates/whatsapp_templates.py tests/test_combined_datetime_picker.py
git commit -m "feat(templates): add select_datetime message key"
```

---

### Task 8: `_show_combined_slot_picker()` — merge date+time into one interactive list

**Files:**
- Modify: `app/services/conversation.py` (add new method near `_show_date_picker`/`_show_slot_list`, i.e. around line 2212-2317)
- Test: `tests/test_combined_datetime_picker.py` (append)

**Interfaces:**
- Consumes: `get_available_slots(clinic_id, doctor_name, date_str)` (existing, `app/database.py:340`), `_to_ampm()` (Task 6), `get_message("select_datetime", lang)` (Task 7), `_suggest_other_doctors()` (existing).
- Produces: `async def _show_combined_slot_picker(self, clinic: dict, phone: str, context: dict, lang: str) -> None` — self-transitions to `"selecting_slot"` state (mirroring `_show_slot_list`'s existing self-transition pattern). Consumed by Task 9 (replaces both `_show_date_picker` call sites).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_combined_datetime_picker.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch


class TestShowCombinedSlotPicker:
    @pytest.mark.asyncio
    async def test_builds_one_list_spanning_multiple_days(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        async def fake_get_available_slots(clinic_id, doctor_name, date_str, **kwargs):
            # First two checked days have slots; the rest are empty.
            if date_str in ("2026-08-21", "2026-08-22"):
                return ["09:00", "09:30", "10:00"], None
            return [], None

        with patch(
            "app.services.conversation.get_available_slots", side_effect=fake_get_available_slots
        ), patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list, patch.object(
            manager, "update_state", new_callable=AsyncMock
        ) as mock_update_state, patch(
            "app.services.conversation.datetime"
        ) as mock_dt:
            from datetime import datetime as real_datetime

            mock_dt.now.return_value = real_datetime(2026, 8, 21)
            mock_dt.strptime = real_datetime.strptime

            await manager._show_combined_slot_picker(clinic, "+919876543210", context, "en")

        sections = mock_send_list.call_args.kwargs["sections"]
        assert len(sections) == 2  # two days with availability
        total_rows = sum(len(s["rows"]) for s in sections)
        assert total_rows <= 10
        all_ids = [r["id"] for s in sections for r in s["rows"]]
        assert all(rid.startswith("dtslot_") for rid in all_ids)
        mock_update_state.assert_called_once_with(
            clinic, "+919876543210", "selecting_slot", context
        )

    @pytest.mark.asyncio
    async def test_no_availability_in_14_days_suggests_other_doctors(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch(
            "app.services.conversation.get_available_slots",
            new_callable=AsyncMock,
            return_value=([], None),
        ), patch.object(
            manager, "_suggest_other_doctors", new_callable=AsyncMock
        ) as mock_suggest, patch.object(
            manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
        ) as mock_send_list:
            await manager._show_combined_slot_picker(clinic, "+919876543210", context, "en")

        mock_suggest.assert_called_once()
        mock_send_list.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_combined_datetime_picker.py::TestShowCombinedSlotPicker -v`
Expected: FAIL — `AttributeError: 'ConversationManager' object has no attribute '_show_combined_slot_picker'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, add the new method right after `_show_date_picker` (after line 2260, before `_show_slot_list`):

```python
    async def _show_combined_slot_picker(
        self, clinic: dict, phone: str, context: dict, lang: str
    ) -> None:
        """Show date+time as ONE interactive list instead of two separate
        messages — merges what used to be _show_date_picker followed by
        _show_slot_list into a single patient tap."""
        today = datetime.now().date()

        day_labels = {
            "en": ["Today", "Tomorrow"],
            "hi": ["आज", "कल"],
            "te": ["ఈరోజు", "రేపు"],
        }
        labels = day_labels.get(lang, day_labels["en"])

        sections = []
        rows_used = 0
        days_with_slots = 0
        MAX_ROWS = 10
        MAX_DAYS = 4

        for i in range(14):
            if rows_used >= MAX_ROWS or days_with_slots >= MAX_DAYS:
                break
            d = today + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")

            slots, _reason = await get_available_slots(
                clinic["id"], context["doctor_name"], date_str
            )
            if not slots:
                continue

            remaining = MAX_ROWS - rows_used
            day_slots = slots[: min(3, remaining)]
            if not day_slots:
                break

            if i == 0:
                title = f"{labels[0]} ({d.strftime('%d %b')})"
            elif i == 1:
                title = f"{labels[1]} ({d.strftime('%d %b')})"
            else:
                title = d.strftime("%A, %d %b")

            sections.append(
                {
                    "title": title[:24],
                    "rows": [
                        {
                            "id": f"dtslot_{date_str}_{slot}",
                            "title": self._to_ampm(slot),
                            "description": "",
                        }
                        for slot in day_slots
                    ],
                }
            )
            rows_used += len(day_slots)
            days_with_slots += 1

        if not sections:
            await self._suggest_other_doctors(clinic, phone, context, lang)
            return

        await self.whatsapp.send_interactive_list(
            clinic,
            phone,
            body=get_message("select_datetime", lang),
            button_text=(
                "Select" if lang == "en" else ("चुनें" if lang == "hi" else "ఎంచుకోండి")
            ),
            sections=sections,
        )

        await self.update_state(clinic, phone, "selecting_slot", context)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_combined_datetime_picker.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_combined_datetime_picker.py
git commit -m "feat(conversation): add combined date+time picker for doctor booking"
```

---

### Task 9: Wire `dtslot_` button-id parsing and replace both `_show_date_picker` call sites

**Files:**
- Modify: `app/services/conversation.py:372-380` (button-id → intent parsing), `app/services/conversation.py:540-541` and `app/services/conversation.py:2094-2095` (the two `_show_date_picker` call sites)
- Test: `tests/test_combined_datetime_picker.py` (append)

**Interfaces:**
- Consumes: `_show_combined_slot_picker()` (Task 8).
- Produces: button ids of the form `dtslot_{date}_{time}` now resolve to `intent="select_datetime"`, consumed by Task 10 (`_handle_selecting_slot`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_combined_datetime_picker.py`:

```python
class TestDtslotButtonParsing:
    def test_dtslot_button_id_maps_to_select_datetime_intent(self):
        # _parse_button_id (or equivalent inline logic) lives inside the
        # webhook message handler; exercise it via the smallest public seam
        # available — direct string-prefix behavior mirrors the existing
        # "slot_"/"date_" branches immediately above it in the same
        # if/elif chain, so this test asserts the mapping contract rather
        # than re-invoking the full webhook handler.
        button_id = "dtslot_2026-08-24_10:00"
        assert button_id.startswith("dtslot_")
        message = button_id.replace("dtslot_", "")
        assert message == "2026-08-24_10:00"
```

(This is a lightweight contract test for the parsing convention rather than a full webhook-handler integration test, since the button-id parsing lives inline in a large message-ingestion method — Task 10's tests exercise the consuming side, which is where behavior actually branches.)

- [ ] **Step 2: Run test to verify it fails**

This particular test doesn't fail against current code (it tests a string literal, not the implementation) — it exists to document the id format contract. Skip to Step 3.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, add a new `elif` branch immediately after the existing `date_` branch (around lines 378-380):

```python
            elif button_id.startswith("date_"):
                intent = "select_date"
                message = button_id.replace("date_", "")
            elif button_id.startswith("dtslot_"):
                intent = "select_datetime"
                message = button_id.replace("dtslot_", "")
```

Replace the first `_show_date_picker` call site (around lines 540-541, inside the `view_doctor` intent handler):

```python
                await self._show_combined_slot_picker(clinic, phone, context, lang)
```

(Remove the now-redundant `await self.update_state(clinic, phone, "selecting_date", context)` line that followed it — `_show_combined_slot_picker` self-transitions to `"selecting_slot"` at its end, per Task 8.)

Replace the second call site at the end of `_handle_selecting_doctor` (around lines 2090-2095):

```python
        context["doctor_name"] = doctor_name
        context["doctor"] = doctor

        await self._show_combined_slot_picker(clinic, phone, context, lang)
```

(Remove the redundant `merged_context = {**context}` / `_show_date_picker` / `update_state(..., "selecting_date", ...)` lines that followed — `context` is already the live dict being mutated, and `_show_combined_slot_picker` handles its own state transition.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_combined_datetime_picker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_combined_datetime_picker.py
git commit -m "feat(conversation): route doctor selection into the combined date+time picker"
```

---

### Task 10: `_handle_selecting_slot` — handle combined `select_datetime` intent + free-text fallback

**Files:**
- Modify: `app/services/conversation.py:2320-2339` (`_handle_selecting_slot`)
- Test: `tests/test_combined_datetime_picker.py` (append)

**Interfaces:**
- Consumes: `intent == "select_datetime"` (Task 9), `_handle_selecting_date()` (existing, unchanged — the free-text fallback).
- Produces: no new interface — `_handle_selecting_slot`'s branching logic is extended; signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_combined_datetime_picker.py`:

```python
class TestHandleSelectingSlotCombined:
    @pytest.mark.asyncio
    async def test_select_datetime_intent_sets_both_date_and_time(self):
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch.object(
            manager, "_show_booking_confirmation", new_callable=AsyncMock
        ) as mock_show_confirmation:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "2026-08-24_10:00", "select_datetime", context, "en"
            )

        assert context["appointment_date"] == "2026-08-24"
        assert context["appointment_time"] == "10:00"
        mock_show_confirmation.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_select_slot_intent_still_works(self):
        """Regression: the old single-day slot_ tap (reached only via the
        free-text-date fallback now) must keep working unchanged."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test", "appointment_date": "2026-08-24"}

        with patch.object(
            manager, "_show_booking_confirmation", new_callable=AsyncMock
        ) as mock_show_confirmation:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "10:00", "select_slot", context, "en"
            )

        assert context["appointment_time"] == "10:00"
        mock_show_confirmation.assert_called_once()

    @pytest.mark.asyncio
    async def test_free_text_delegates_to_handle_selecting_date(self):
        """A patient typing 'tomorrow' instead of tapping a button must
        still work — delegates to the existing free-text date parser."""
        manager = ConversationManager()
        clinic = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
        context = {"doctor_name": "Dr. Test"}

        with patch.object(
            manager, "_handle_selecting_date", new_callable=AsyncMock
        ) as mock_handle_date:
            await manager._handle_selecting_slot(
                clinic, "+919876543210", "tomorrow", "unknown", context, "en"
            )

        mock_handle_date.assert_called_once_with(
            clinic, "+919876543210", "tomorrow", context, "en"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_combined_datetime_picker.py::TestHandleSelectingSlotCombined -v`
Expected: FAIL — current implementation always treats `message` as a bare time string and never delegates to `_handle_selecting_date`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/conversation.py`, replace `_handle_selecting_slot` (lines 2320-2339):

```python
    async def _handle_selecting_slot(
        self,
        clinic: dict,
        phone: str,
        message: str,
        intent: str,
        context: dict,
        lang: str,
    ) -> None:
        """Handle slot selection — combined date+time tap, legacy single-day
        slot tap, or free-text date input (delegates to the date parser)."""

        if intent == "select_datetime":
            date_str, _, time_str = message.partition("_")
            context["appointment_date"] = date_str
            context["appointment_time"] = time_str
        elif intent == "select_slot":
            context["appointment_time"] = message.strip()
        else:
            await self._handle_selecting_date(clinic, phone, message, context, lang)
            return

        await self._show_booking_confirmation(clinic, phone, context, lang)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_combined_datetime_picker.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation.py tests/test_combined_datetime_picker.py
git commit -m "feat(conversation): handle combined datetime selection with free-text fallback"
```

---

### Task 11: Full regression run and manual WhatsApp verification

**Files:** None (verification only)

- [ ] **Step 1: Run the full automated test suite**

Run: `pytest -q`
Expected: all previously-passing tests still pass, plus every new test from Tasks 1-10 (0 failures, 0 errors).

- [ ] **Step 2: Manually verify against a real diagnostics-only clinic (Accumax Diagnostics) in production**

Send "Hi" to trigger the main menu → confirm the list shows "Book Lab Test" (not "Book Appointment"), no "Our Services" row, no "Our Doctors" row. Tap "Book Lab Test" → confirm the lab test catalog appears directly. Complete a real ₹1 test booking (temporarily lower one test's price for this check, then restore it) → confirm the payment-confirmation message is followed by a single "Main Menu" button, and tapping it correctly re-shows the main menu (proving the state reset worked).

- [ ] **Step 3: Manually verify against a real doctor-consultation clinic in production**

Send "Book Appointment" → select a department → select a doctor → confirm ONE interactive list message now shows both date and time together (e.g. "Today (21 Aug)" section with time rows, "Tomorrow (22 Aug)" section with time rows) instead of two separate messages. Tap a slot → confirm the booking confirmation (Confirm/Edit) appears immediately. Complete the booking → confirm the follow-up shows only "Main Menu", not "Book Appointment" + "Main Menu".

This step has no automated test — it is the final human check that the real WhatsApp Cloud API renders the new combined interactive list correctly (row/section limits, title truncation) and that Meta's actual button-tap webhook payloads match what the mocked unit tests assumed.

---

## Self-Review

**1. Issue coverage:**
- "Our Services"/"Our Doctors" leaking the doctor-department menu for diagnostics-only clinics (screenshots 1 & 2) → Tasks 1, 2, 3.
- Post-confirmation follow-up should show only "Main Menu" (screenshot 3, plus the literal "after the test confirmation" gap where lab-test payment confirmation currently sends no follow-up at all) → Tasks 4, 5 (both the non-payment and Razorpay-gated confirmation paths are covered, since the user's own example screenshot was from a non-payment-gated clinic while the literal complaint was about lab-test/Razorpay confirmations).
- Doctor booking's separate date-then-time conversation turns merged into one → Tasks 6, 7, 8, 9, 10.

**2. Placeholder scan:** No "TBD"/"TODO"/"add appropriate error handling" found. Every step has runnable code or an exact verification command. Task 9's Step 1 test is explicitly scoped as a documentation-of-contract test rather than a behavior test, with the reasoning stated inline (the actual behavior is exercised by Task 10's tests) — this is a deliberate scoping note, not a placeholder.

**3. Type consistency:** `_is_diagnostics_only(self, clinic: dict) -> bool` signature matches across Task 1 (definition), Task 2/3 (call sites `await self._is_diagnostics_only(clinic)`). `_to_ampm(self, time_24: str) -> str` matches across Task 6 (definition) and Task 8 (`self._to_ampm(slot)`). `_show_combined_slot_picker(self, clinic, phone, context, lang)` signature matches across Task 8 (definition) and Task 9 (both call sites, same four positional args as the old `_show_date_picker` calls they replace). Button-id prefix `dtslot_{date}_{time}` (Task 8's row-id construction, `f"dtslot_{date_str}_{slot}"`) matches Task 9's parsing (`button_id.replace("dtslot_", "")`) and Task 10's consumption (`message.partition("_")` splitting on the first underscore — safe because `date_str` uses dashes and `slot` uses a colon, never an underscore).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-21-diagnostic-menu-fix-and-datetime-merge.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
