# Session 16 — Lab Booking Patient Name, Guide Commands, Family Members

**Date:** 2026-09-19
**Branch:** main (uncommitted at time of writing)
**Migration:** none — no schema change. Uses existing `conversations.context`, `patients.name`, `family_members`.
**Trigger:** live test on Accumx Diagnostics (Direct Stream plan) from the owner's phone, with screenshots of the WhatsApp chat and the admin Appointments page.

---

## 1. Reported symptoms → root causes

| # | Symptom (production) | Root cause | Where |
|---|---|---|---|
| 1 | Lab test booked without asking who it is for; admin panel showed patient **"Patient"** | Tapping a collection date wrote the booking immediately; name fell back to `patient.name` or the literal `"Patient"` | `_handle_confirming_collection_date` |
| 2 | "Cancel" / "Cancel booking" → **no reply** | Lab bookings store `doctor_name = NULL`; `appt.get('doctor_name', 'Doctor')[:20]` returns None → `TypeError` → message died, retried 3× silently | `_handle_cancel_request` |
| 3 | "delete my data" → **no reply** | Data *was* erased, but `delete_patient_data` deletes the `conversations` row, and `send_text` → `_can_send_freeform` reads that row for the 24h window → "session expired", reply dropped | `_handle_data_deletion` + `whatsapp.send_text` |
| 4 | Guide commands ("change language", "cancel booking") unreliable | Dependent on the LLM classifier; keyword fallback misread `cancel booking` → `book_appointment` (substring "book"), `change language` → `reschedule_appointment` (substring "change"). `emergency` typed alone had no fast path | `ai_engine.detect_intent` |
| 5 | (found while verifying payment) Partial-deposit centres charged **full** lab price; paid lab bookings not linked to patient | Lab call to `create_booking_with_payment` omitted `deposit_percent` and `patient_id` | `_finalize_lab_booking` |
| 6 | (found) Confirmation quoted both weekday and Sunday hours for a Sunday booking | `format_collection_window` prints the whole week | lab confirmation |
| 7 | (found) Saved family list always empty | Nothing ever called `add_family_member`; the `confirming_save_family_member` state was never entered (dead code), and when it was it dumped the patient to the main menu mid-booking | doctor flow |

Payment itself was correct for Accumx: no Razorpay keys → `resolve_payment_mode` = `"none"` → confirmed booking "payable at the centre".
TIME column "—" in admin is by design: lab bookings use the collection window, not a slot.

Note: production DB/log reads were blocked by the permission classifier; root causes were established by code tracing and each is pinned by a reproducing test.

---

## 2. Changes

### A. `app/services/conversation.py`

**Lab flow — date → who → name (all inside the existing `confirming_collection_date` state, told apart by `context["lab_step"]`):**
- Date tap (`labdate_*`) stores `lab_collection_date` and calls `_ask_lab_test_patient` (no booking yet).
- `_ask_lab_test_patient`: buttons **For Me / Someone Else**; if the phone has saved family members, an interactive list instead: For Me, up to 8 members (`labfor_fam_{i}`), + Someone Else. Names snapshot in `context["lab_family"]`.
- `labfor_self` with a saved name → book; without → `lab_step="name"`, name saved to `patients.name`.
- `labfor_other` → `lab_step="name"`; typed name validated with `validate_name` (`_send_name_error` shared with doctor flow).
- New (unsaved) someone-else name → `lab_step="save"`, buttons **Save & Book** (`labsave_yes`) / **Just Book** (`labsave_no`); both book, only yes calls `add_family_member`. Works for paid and counter centres.
- Stale `labfor_fam_N` index → asks again, never books the wrong person.
- `_finalize_lab_booking` (extracted): payment decision + booking; passes `patient_id` and `deposit_percent`; pay message shows patient and deposit note; failure path returns to main menu and clears lab keys.
- `_book_lab_test_without_payment`: confirmation adds 👤 patient name and `_collection_hours_on(window, date, lang)` (the chosen day's hours only).
- `_LAB_STEP_CLEARED` constant nulls `lab_step`, `lab_for_self`, `lab_collection_date`, `lab_family`, `lab_pending_name` on leaving (update_state *merges* context, so keys must be overwritten).
- `confirming_collection_date` added to `MID_BOOKING_STATES` (30-min timeout) and Guard-5 `SAFE_STATES`.
- `_process_state`: a typed name at `lab_step == "name"` is routed straight to the lab handler (after emergency/opt-out/deletion/help/language, excluding nav words, "book test", greeting/cancel/reschedule intents) so the classifier can't hijack it.
- Picking a new test clears old lab step keys.

**Cancel:** row title = `doctor_name or lab_test_name or "Booking"`; body/button localized.

**Delete my data:** checks `delete_patient_data` result; sends via `send_text(..., _window_open=True)`; honest failure message; analytics only on success.

**Language:** a language button tapped on an older picker applies directly for a consented patient (`data_consent` truthy); others still go through the picker so consent logic is untouched.

**Doctor flow — family save:**
- `_offer_save_family_member` after a validated someone-else name (`_handle_collecting_name` else-branch, and the typed-name fallback in `_handle_selecting_family_member`, now also validated). Skipped for self and for names already saved (case-insensitive).
- `_handle_confirming_save_family_member` rewritten: Save / Not now both continue booking via `_continue_after_patient_name` (treatment doctors or symptoms); unrelated input re-shows the prompt; missing `pending_family_name` → main menu.
- `_send_save_family_prompt` (buttons `save_family_yes` / `save_family_no`, already routed by the global `save_family_` prefix).

### B. `app/services/ai_engine.py`
- `GUIDE_COMMAND_INTENTS` + `guide_command_intent()`: exact whole-message match (trimmed, case-insensitive) for cancel / cancel booking / cancel appointment / cancel test (+ "my" variants), reschedule, change language / language / भाषा बदलें / భాష మార్చు, talk to staff, emergency.
- `detect_intent` fast path 2b uses it before the LLM.

### C. `app/services/whatsapp.py`
- `send_text(..., _window_open: bool = False)`: skip the 24h-window lookup when the caller is replying to a message just received. Default unchanged for every other caller.

---

## 3. Tests

- **New:** `tests/test_lab_patient_name_and_guide_commands.py`: lab who/name steps, For Me with/without saved name, Someone Else, invalid name, classifier-hijack routing, Sunday hours, cancel list for lab booking, deletion reply + failure, `send_text` window bypass, guide-command mapping and no-LLM path, stale language button, deposit + patient_id, lab family list/pick/stale index/save yes-no/already saved, doctor family save yes/no/self/already saved/re-prompt. Autouse fixture stubs `get_family_members` so no test reaches the real table.
- **Updated (date tap no longer books):** `test_admin_to_patient_sync.py`, `test_lab_booking_production_fixes.py` (2), `test_lab_test_booking_conversation.py` — context now carries `lab_collection_date` and taps `labfor_self`.
- **Updated:** `test_specialty_conversation_wiring.py` — `route_to_treatment_doctors(` call count 7 → 6 (two paths share `_continue_after_patient_name`).
- **Full suite:** `pytest tests --ignore=tests/test_multi_worker_smoke.py` → **2730 passed, 1 skipped, 0 failed**. Multi-worker smoke excluded (boots uvicorn against production `.env` and orphans lock holders — see memory `local-app-runs-steal-prod-locks`); no orphaned processes found afterwards.
- One unrelated flake seen once: `test_alert_verification.py::test_alert_01...` ("Server disconnected" from Supabase), passed on rerun.

---

## 4. Patient-facing flows after this session

**Lab (every plan with `lab_test_booking`):** test → date → *Who is this test for?* (For Me / saved members / + Someone Else) → name if needed → *Save & Book / Just Book* for new names → booked (counter) or payment link (Razorpay, deposit honoured). Confirmation shows test, patient, date, price, that day's collection hours, ref.

**Doctor:** unchanged until a someone-else name is typed → *Save / Not now* → symptoms (or treatment doctors). Saved people appear in the existing "Who is this appointment for?" list. Lab and doctor flows share one family list.

**Guide commands** (menu, book, book test, cancel booking, reschedule, change language, talk to staff, emergency, stop, start, delete my data, help) all answer deterministically regardless of LLM availability.

---

## 5. Open / not done

- Not committed or deployed at time of writing.
- Lab booking has no time-slot selection (collection window by design); add only if clients ask.
- `log_analytics_event(..., "data_deleted")` still writes the phone after erasure (pre-existing).
- Relationship (mother/father…) is not captured for saved family members.
