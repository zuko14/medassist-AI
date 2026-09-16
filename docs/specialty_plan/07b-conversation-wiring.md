# Task 7 (Part B) — Wire `specialty_flow` into `app/services/conversation.py`

Every edit below either:
- **adds** a branch that runs only when a treatment is involved (`context.get("treatment_id")`, `specialty_enabled(clinic)`, or an id this feature owns), or
- threads a new optional parameter whose default reproduces today's behaviour exactly.

**Nothing else in `conversation.py` may change.** Use the exact `old` → `new` snippets. If an `old` snippet is not found exactly once in the named function, stop and report it; do not improvise.

**Files:**
- Modify: `app/services/conversation.py`
- Test: `tests/test_specialty_conversation_wiring.py`

**Interfaces:**
- Consumes: everything produced by Part A.
- Produces:
  - `ConversationManager._start_booking(clinic, phone, patient, lang, seed_context: Optional[dict] = None)`
  - `ConversationState.BROWSING_TREATMENTS = "browsing_treatments"`
  - `ConversationState.SEARCHING_TREATMENTS = "searching_treatments"`

---

- [x] **Step 1: Write the failing test** — create `tests/test_specialty_conversation_wiring.py`:

```python
"""conversation.py wiring: existing tenants see identical behaviour; specialty
clinics get the treatment flow; an abandoned treatment never tags a booking."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow
from app.services.conversation import ConversationManager, ConversationState

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "app" / "services" / "conversation.py").read_text(encoding="utf-8")
PHONE = "+919000000001"
T1 = "11111111-1111-1111-1111-111111111111"


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    return m


def _menu_ids(m):
    call = m.whatsapp.send_interactive_list.await_args
    return [r["id"] for s in call.kwargs["sections"] for r in s["rows"]]


def test_states_exist():
    assert ConversationState.BROWSING_TREATMENTS == "browsing_treatments"
    assert ConversationState.SEARCHING_TREATMENTS == "searching_treatments"


# ── main menu ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["soloclinic", "essential", "polyclinic", "enterprise"])
async def test_existing_plans_keep_the_exact_same_menu(plan):
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)) as active:
        await m._send_main_menu({"id": "c1", "plan": plan}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human"]
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_plan_with_lab_booking_keeps_its_lab_row():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", side_effect=lambda c, f: f == "lab_test_booking"):
        await m._send_main_menu({"id": "c1", "plan": "polyclinic"}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_lab_tests",
                            "menu_emergency", "menu_human"]


@pytest.mark.asyncio
async def test_specialty_clinic_with_treatments_gets_treatment_rows_and_no_services_row():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": "dental", "features": {}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_treatments", "menu_concern", "menu_book", "menu_doctors",
                            "menu_emergency", "menu_human"]


@pytest.mark.asyncio
async def test_specialty_clinic_without_published_treatments_keeps_the_normal_menu():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=False)):
        await m._send_main_menu({"id": "c1", "plan": "eye", "features": {}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_book", "menu_services", "menu_doctors", "menu_emergency", "menu_human"]


@pytest.mark.asyncio
async def test_override_enabled_general_clinic_keeps_services_alongside_treatments():
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.has_feature", return_value=False), \
         patch.object(specialty_flow, "has_active_treatments", AsyncMock(return_value=True)):
        await m._send_main_menu({"id": "c1", "plan": "polyclinic", "features": {"specialty_treatments": True}}, PHONE, "en")
    assert _menu_ids(m) == ["menu_treatments", "menu_concern", "menu_book", "menu_services", "menu_doctors",
                            "menu_emergency", "menu_human"]


# ── context hygiene ──────────────────────────────────────────────────────────

async def _run_update_state(new_state):
    m = _manager()
    existing = MagicMock(data=[{"context": {"treatment_id": T1, "treatment_name": "IVF", "branch_id": "b1"},
                                "state": "selecting_doctor"}])
    fake_supabase = MagicMock()
    with patch("app.database.supabase", fake_supabase), \
         patch("app.services.conversation.sb", AsyncMock(side_effect=[existing, MagicMock(data=[])])):
        await m.update_state({"id": "c1"}, PHONE, new_state, {"x": 1})
    return fake_supabase.table.return_value.update.call_args.args[0]["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["main_menu", "idle", "selecting_department", "suggesting_department",
                                   "collecting_symptoms"])
async def test_leaving_the_treatment_flow_drops_the_tag(state):
    ctx = await _run_update_state(state)
    assert "treatment_id" not in ctx and "treatment_name" not in ctx
    assert ctx["branch_id"] == "b1" and ctx["x"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["selecting_doctor", "selecting_date", "selecting_slot", "confirming_booking",
                                   "selecting_branch", "collecting_name", "awaiting_payment"])
async def test_booking_steps_keep_the_tag(state):
    ctx = await _run_update_state(state)
    assert ctx["treatment_id"] == T1


# ── booking seed and the symptom step ────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("seed, expected", [(None, {}), ({"treatment_id": T1, "treatment_name": "IVF"},
                                                          {"treatment_id": T1, "treatment_name": "IVF"})])
async def test_start_booking_seed_is_carried_or_empty(seed, expected):
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
         patch("app.services.tenant.get_clinic_branches", AsyncMock(return_value=[])), \
         patch("app.services.tenant.has_branches", return_value=False), \
         patch.object(m, "update_state", AsyncMock()) as upd, \
         patch.object(m, "_continue_booking_after_branch", AsyncMock()) as cont:
        await m._start_booking({"id": "c1"}, PHONE, {"language": "en", "name": "Asha Rao"}, "en", seed_context=seed)
    assert upd.await_args.args[2] == "selecting_family_member"
    assert upd.await_args.args[3] == expected and upd.await_args.kwargs["reset_context"] is True
    assert cont.await_args.args[4] == expected


@pytest.mark.asyncio
async def test_name_step_routes_treatment_bookings_to_specialists():
    m = _manager()
    ctx = {"treatment_id": T1, "treatment_name": "Hair PRP Therapy", "for_self": True}
    with patch("app.services.conversation.update_patient", AsyncMock()), \
         patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_collecting_name({"id": "c1"}, PHONE, "Asha Rao", ctx, {"name": None}, "en")
    show.assert_awaited_once()
    m.whatsapp.send_text.assert_not_awaited()
    assert all(c.args[2] != "collecting_symptoms" for c in upd.await_args_list)


@pytest.mark.asyncio
async def test_name_step_without_treatment_still_asks_for_symptoms():
    m = _manager()
    with patch("app.services.conversation.update_patient", AsyncMock()), \
         patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_collecting_name({"id": "c1"}, PHONE, "Asha Rao", {"for_self": True}, {"name": None}, "en")
    show.assert_not_awaited()
    m.whatsapp.send_text.assert_awaited_once()
    assert upd.await_args.args[2] == "collecting_symptoms"


@pytest.mark.asyncio
async def test_family_member_pick_routes_treatment_bookings():
    m = _manager()
    ctx = {"treatment_id": T1, "family_members": [{"full_name": "Ravi Rao", "relationship": "spouse"}]}
    with patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "update_state", AsyncMock()) as upd:
        await m._handle_selecting_family_member({"id": "c1"}, PHONE, "fam_0", ctx, "en", {"name": "Asha"})
    show.assert_awaited_once()
    upd.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_doctors_in_treatment_flow_pages_the_treatment_list():
    m = _manager()
    ctx = {"treatment_id": T1, "doctor_page": 0}
    with patch.object(specialty_flow, "show_treatment_doctors", AsyncMock()) as show, \
         patch.object(m, "_show_doctor_list", AsyncMock()) as dept_list:
        await m._handle_selecting_doctor({"id": "c1"}, PHONE, "More options", "button_click", ctx, "en",
                                         {"id": "doc_more"})
    assert show.await_args.kwargs["page"] == 1
    dept_list.assert_not_awaited()


# ── hooks present exactly where required ─────────────────────────────────────

def test_routing_hooks_are_wired():
    assert "from app.services import specialty_flow" in SRC
    assert "specialty_flow.handle_treatment_button(" in SRC
    assert "specialty_flow.offer_treatment_browse(" in SRC
    assert "specialty_flow.handle_treatment_search_text(" in SRC
    assert "specialty_flow.handle_treatment_state(" in SRC
    assert "specialty_flow.TREATMENT_RESET_STATES" in SRC
    assert SRC.count("specialty_flow.route_to_treatment_doctors(") == 7
    view_doctor_block = SRC.split('if intent == "view_doctor":')[1][:3500]
    assert "specialty_flow.clear_treatment_context(context)" in view_doctor_block


def test_no_new_booking_type_is_introduced():
    assert "treatment_procedure" not in SRC
```

- [x] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_conversation_wiring.py -q
```
Expected: FAIL (missing states, unexpected `seed_context` keyword, and so on).

- [x] **Step 3: Import.** Replace exactly:
```python
from app.services.clinical_firewall import screen_message
```
with:
```python
from app.services.clinical_firewall import screen_message
from app.services import specialty_flow
```

- [x] **Step 4: States.** Replace exactly:
```python
    CONFIRMING_COLLECTION_DATE = "confirming_collection_date"
```
with:
```python
    CONFIRMING_COLLECTION_DATE = "confirming_collection_date"
    BROWSING_TREATMENTS = "browsing_treatments"
    SEARCHING_TREATMENTS = "searching_treatments"
```

- [x] **Step 5: Booking context keys.** Inside `BOOKING_CONTEXT_KEYS = frozenset({ … })`, replace exactly:
```python
    "department_page",
```
with:
```python
    "department_page",
    "treatment_id",
    "treatment_name",
```

- [x] **Step 6: `update_state` context hygiene.** Replace exactly:
```python
        if reset_context:
            merged = new_context
        else:
            merged = {**existing, **new_context}
```
with:
```python
        if reset_context:
            merged = new_context
        else:
            merged = {**existing, **new_context}

        # A treatment tag must never outlive the treatment flow that set it. A
        # patient who abandons it and books through departments or Our Doctors
        # would otherwise get that booking labelled with the old treatment.
        # No-op for every clinic that never sets these keys.
        if new_state in specialty_flow.TREATMENT_RESET_STATES:
            specialty_flow.clear_treatment_context(merged)
```

- [x] **Step 7: Firewall offer.** Replace exactly:
```python
            if firewall_blocked and firewall_response:
                await self.whatsapp.send_text(clinic, phone, firewall_response)
```
with:
```python
            if firewall_blocked and firewall_response:
                await self.whatsapp.send_text(clinic, phone, firewall_response)
                # Specialty clinics only: the firewall stays exactly as strict,
                # but the patient also gets a way into the clinic's own catalogue.
                await specialty_flow.offer_treatment_browse(self, clinic, phone, lang_for_firewall)
```

- [x] **Step 8: Button routing.** In the interactive-button `if/elif` chain in `_handle_message_locked`, replace exactly:
```python
            elif button_id == "continue_booking":
                intent = "continue_booking"
```
with:
```python
            elif button_id in specialty_flow.TREATMENT_BUTTON_IDS or button_id.startswith(
                specialty_flow.TREATMENT_BUTTON_PREFIXES
            ):
                # Specialty treatments: menu rows, category/treatment lists and
                # card buttons. Returns to the main menu when the clinic no
                # longer has the feature (a stale list tapped after a plan change).
                lang = await get_lang(clinic, phone)
                await specialty_flow.handle_treatment_button(self, clinic, phone, button_id, session, lang)
                return
            elif button_id == "continue_booking":
                intent = "continue_booking"
```
Check that none of `TREATMENT_BUTTON_PREFIXES` (`trtcat_`, `trtbook_`, `trtcall_`, `trt_`) collides with an existing id prefix:
```bash
grep -n '"trt' app/services/*.py app/templates/*.py
```
Expected: matches only in `specialty_flow.py` and this new branch.

- [x] **Step 9: Our Doctors path.** Inside `if intent == "view_doctor":`, replace exactly:
```python
            context = session.get("context", {})
            context["doctor"] = doc
```
with:
```python
            context = session.get("context", {})
            # Our Doctors is not the treatment flow; drop any abandoned tag.
            specialty_flow.clear_treatment_context(context)
            context["doctor"] = doc
```

- [x] **Step 10: Typed text while browsing or searching treatments.** Insert directly **before** the line:
```python
        # Global handlers for top-level menu intents (escape hatches from selection states)
```
the block:
```python
        # A patient typing while browsing or searching treatments is describing a
        # concern. Placed ahead of the global menu intents for the same reason as
        # the lab-test search above: a free-text concern ("hair fall") can be
        # classified as view_services / doctor_availability and would otherwise
        # be hijacked. Emergency, opt-out, escalation and language change are
        # handled above this point, so they still win.
        if (
            state in ("browsing_treatments", "searching_treatments")
            and not interactive_data
            and (message or "").strip()
            and message.strip().lower() not in NAV_KEYWORDS
            and intent not in {"greeting", "book_appointment", "cancel_appointment", "reschedule_appointment"}
        ):
            await specialty_flow.handle_treatment_search_text(self, clinic, phone, message, lang)
            return

```

- [x] **Step 11: State machine.** Insert directly **before** the line:
```python
        # "viewing_reports" is no longer entered — the report archive is gone.
```
the block:
```python
        elif state in ("browsing_treatments", "searching_treatments"):
            await specialty_flow.handle_treatment_state(self, clinic, phone, lang)
```
This `elif` must sit in the same `if state == "idle": … elif …` chain, directly after the `elif state == "confirming_collection_date":` branch and its call.

- [x] **Step 12: Main menu.** Replace the entire `_send_main_menu` method. Old (exact):
```python
    async def _send_main_menu(self, clinic: dict, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        from app.services.tenant import has_feature

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
```
New:
```python
    async def _send_main_menu(self, clinic: dict, phone: str, lang: str) -> None:
        """Send main menu with buttons."""
        from app.services.tenant import has_feature

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

        # Specialty clinics with at least one published treatment lead with
        # their treatments. treatment_menu_active() is False — without a
        # database call — for every plan that existed before migration 077.
        treatment_menu = (not diagnostics_only) and await specialty_flow.treatment_menu_active(clinic)

        rows = specialty_flow.treatment_menu_rows(lang) if treatment_menu else []
        rows.append({"id": "menu_book", "title": book_title[:24], "description": ""})
        if not diagnostics_only:
            # On a single-specialty plan "Our Services" would list one department;
            # Our Treatments replaces it. Override-enabled general clinics keep both.
            if not (treatment_menu and specialty_flow.is_specialty_plan(clinic)):
                services_title = {"en": "Our Services", "hi": "Our Services", "te": "Our Services"}.get(lang, "Our Services")
                rows.append({"id": "menu_services", "title": services_title[:24], "description": ""})
            rows.append({"id": "menu_doctors", "title": t[0][:24], "description": ""})
```
Everything after that point in the method (the lab row, the "No My Reports" comment, emergency, human, `sections`, `send_interactive_list`) stays **unchanged**.

- [x] **Step 13: `_start_booking` seed.** Replace the method signature and body. Old (exact):
```python
    async def _start_booking(
        self, clinic: dict, phone: str, patient: Optional[dict], lang: str
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics."""
        patient = patient or {}
```
New:
```python
    async def _start_booking(
        self,
        clinic: dict,
        phone: str,
        patient: Optional[dict],
        lang: str,
        seed_context: Optional[dict] = None,
    ) -> None:
        """Start the booking flow — with optional branch selection for multi-branch clinics.

        seed_context: keys carried into the fresh booking context. Only the
        specialty treatment flow passes it ({"treatment_id", "treatment_name"});
        every existing caller passes nothing and gets the empty context it
        always had.
        """
        patient = patient or {}
        seed = dict(seed_context or {})
```
Then, **inside `_start_booking` only**, make these four replacements (each occurs once in this method; `_start_lab_booking` has similar lines that must NOT change):

| Old (in `_start_booking`) | New |
|---|---|
| `await self.update_state(clinic, phone, "selecting_branch", {}, reset_context=True)` | `await self.update_state(clinic, phone, "selecting_branch", dict(seed), reset_context=True)` |
| `context = self._set_branch_context({}, branch)` | `context = self._set_branch_context(dict(seed), branch)` |
| `await self.update_state(clinic, phone, "selecting_family_member", {}, reset_context=True)` | `await self.update_state(clinic, phone, "selecting_family_member", dict(seed), reset_context=True)` |
| `await self._continue_booking_after_branch(clinic, phone, patient, lang, {})` | `await self._continue_booking_after_branch(clinic, phone, patient, lang, dict(seed))` |

- [x] **Step 14: The seven places the flow asks for symptoms.** At each, add the two-line treatment route **before** the existing state update or message. The existing lines stay byte-identical.

**S1** — `_handle_message_locked`, "For Me" button. Replace exactly:
```python
                ctx["for_self"] = True
                ctx["booking_name"] = patient_name
                await update_conversation(
```
with:
```python
                ctx["for_self"] = True
                ctx["booking_name"] = patient_name
                if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, ctx, lang):
                    return
                await update_conversation(
```

**S2** — `_handle_selecting_family_member`, branch 1 ("For Me"). Replace exactly:
```python
                "for_self": True,
                "is_family": False,
            }
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```
with:
```python
                "for_self": True,
                "is_family": False,
            }
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                return
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```

**S3** — `_handle_selecting_family_member`, branch 3 (saved member by index). Replace exactly:
```python
                "relationship": member.get("relationship"),
                "is_family": True,
                "for_self": False,
            }
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```
with:
```python
                "relationship": member.get("relationship"),
                "is_family": True,
                "for_self": False,
            }
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                return
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```

**S4** — `_handle_selecting_family_member`, branch 4 (typed exact name). Replace exactly:
```python
                    "relationship": m.get("relationship"),
                    "is_family": True,
                    "for_self": False,
                }
                await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```
with:
```python
                    "relationship": m.get("relationship"),
                    "is_family": True,
                    "for_self": False,
                }
                if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                    return
                await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```

**S5** — `_handle_selecting_family_member`, fallback (typed new name). Replace exactly:
```python
                "booking_name": message.strip(),
                "is_family": True,
                "for_self": False,
            }
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```
with:
```python
                "booking_name": message.strip(),
                "is_family": True,
                "for_self": False,
            }
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, new_ctx, lang):
                return
            await self.update_state(clinic, phone, "collecting_symptoms", new_ctx)
```

**S6** — `_handle_collecting_name`, typed "self". Replace exactly:
```python
            context["for_self"] = True
            context["booking_name"] = patient.get("name")
            await self.whatsapp.send_text(
```
with:
```python
            context["for_self"] = True
            context["booking_name"] = patient.get("name")
            if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, context, lang):
                return
            await self.whatsapp.send_text(
```

**S7** — `_handle_collecting_name`, valid name. Replace exactly:
```python
        # Move to symptoms
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
```
with:
```python
        # Treatment bookings skip symptoms: the patient already chose the treatment.
        if await specialty_flow.route_to_treatment_doctors(self, clinic, phone, context, lang):
            return

        # Move to symptoms
        await self.whatsapp.send_text(clinic, phone, get_message("ask_symptoms", lang))
```

Verify:
```bash
grep -c "specialty_flow.route_to_treatment_doctors(" app/services/conversation.py
grep -c 'get_message("ask_symptoms", lang)' app/services/conversation.py
```
Expected: `7`, and the second count unchanged from before this task (7).

- [x] **Step 15: `_handle_selecting_doctor`.** Three edits, all inside this method.

**H1** — more doctors. Replace exactly:
```python
        if button_id == "doc_more":
            await self._show_doctor_list(
```
with:
```python
        if button_id == "doc_more":
            if context.get("treatment_id"):
                await specialty_flow.show_treatment_doctors(
                    self, clinic, phone, context, lang,
                    page=int(context.get("doctor_page") or 0) + 1,
                )
                return
            await self._show_doctor_list(
```

**H2** — department from the chosen doctor. Replace exactly:
```python
        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
```
with:
```python
        context["doctor_name"] = doctor_name
        context["doctor"] = doctor
        if context.get("treatment_id") and isinstance(doctor, dict) and doctor.get("department"):
            # The treatment flow skipped department selection; the booking's
            # department (analytics, confirmations) is the specialist's own.
            context["department"] = doctor["department"]
```

**H3** — invalid input re-shows the treatment's specialists. In the `if not doctor:` block of this method, replace exactly:
```python
            await self.whatsapp.send_text(clinic, phone, fallback_msg)
            if context.get("department"):
```
with:
```python
            await self.whatsapp.send_text(clinic, phone, fallback_msg)
            if context.get("treatment_id"):
                await specialty_flow.show_treatment_doctors(self, clinic, phone, context, lang)
                return
            if context.get("department"):
```

- [x] **Step 16: Run the new tests and the full conversation regression set**

```bash
pytest tests/test_specialty_conversation_wiring.py tests/test_specialty_whatsapp_flow.py tests/test_conversation_navigation_and_timeout.py tests/test_conversation_session_timeout.py tests/test_conversation_payment_mode.py tests/test_conversation_unreadable_messages.py tests/test_conversation_admin_sync_and_csv.py tests/test_lab_test_booking_conversation.py tests/test_lab_test_search.py tests/test_lab_booking_production_fixes.py tests/test_webhook.py tests/test_lint_unscoped_queries.py -q
```
Expected: all PASS. **Any failure in a pre-existing test is a regression.** Fix the new code; never edit a pre-existing test in this step.

Run the orphan check.

- [x] **Step 17: Commit**

```bash
git add app/services/conversation.py tests/test_specialty_conversation_wiring.py
git commit -m "feat(whatsapp): wire specialty treatment flow into the conversation state machine

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
