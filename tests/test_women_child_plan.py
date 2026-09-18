"""The Women & Child hospital plan (migration 082) and treatment service lines.

A Rainbow / BirthRight style hospital runs Child Care, Women Care and
Fertility Care under one roof. Two load-bearing properties:

* ZERO REGRESSION -- a catalogue that files nothing under a service line (every
  row that existed before 082), or files everything under ONE line, browses
  exactly as before. TestUnsplitCatalogueIsUnchanged pins that.
* NOTHING UNREACHABLE -- with three lines and ~45 treatments, every treatment
  must still be reachable by tapping, inside Meta's list limits, in all three
  languages. TestFullCatalogueWalk drives the real flow to prove it.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow as sf
from app.services.ai_engine import EMERGENCY_KEYWORDS, keyword_intent_fallback
from app.services.clinical_firewall import is_sex_determination_request, screen_message
from app.services.conversation import ConversationManager
from app.services.specialty_catalog import (
    CONCERN_EXAMPLES_BY_PLAN,
    SERVICE_LINES,
    STARTER_LISTS_BY_PLAN,
    STARTER_SERVICE_LINE,
    STARTER_TREATMENTS,
    _PATHWAY_BY_NAME,
    seed_starter_treatments,
)
from app.services.tenant import (
    HYBRID_SPECIALTY_PLANS,
    PLAN_FEATURES,
    SPECIALTY_BY_PLAN,
    has_feature,
    specialty_enabled,
)

REPO = Path(__file__).resolve().parent.parent
PLAN = "womenchild"
PHONE = "+919000000082"
CLINIC = {"id": "clinic-wc", "plan": PLAN, "features": {}}


# ── plan registry ────────────────────────────────────────────────────────────


class TestPlanRegistry:
    def test_features_are_the_multispecialty_hybrid(self):
        assert PLAN_FEATURES[PLAN] == PLAN_FEATURES["multispecialty"]
        assert "*" not in PLAN_FEATURES[PLAN]

    @pytest.mark.parametrize("feature", [
        "booking", "multi_department", "multi_branch", "lab_test_booking", "lab_reports",
        "payments_razorpay", "reminders", "specialty_treatments", "staff_training", "feedback",
    ])
    def test_departments_lab_and_treatments_are_all_on(self, feature):
        assert has_feature({"plan": PLAN}, feature)

    def test_it_is_a_hybrid_so_the_departments_row_stays(self):
        assert PLAN in HYBRID_SPECIALTY_PLANS
        assert PLAN not in SPECIALTY_BY_PLAN
        assert not sf.is_specialty_plan({"plan": PLAN})

    @pytest.mark.parametrize("clinic, expected", [
        ({"plan": PLAN}, True),
        ({"plan": PLAN, "features": None}, True),
        ({"plan": PLAN, "features": {"specialty_treatments": False}}, False),
        ({"plan": "enterprise"}, False),
        ({"plan": "polyclinic"}, False),
    ])
    def test_specialty_enabled(self, clinic, expected):
        assert specialty_enabled(clinic) is expected

    def test_no_other_plan_changed(self):
        assert PLAN_FEATURES["multispecialty"] == set(PLAN_FEATURES["polyclinic"]) | {"specialty_treatments"}
        assert SPECIALTY_BY_PLAN == {
            "derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "fertility",
        }


class TestRegistrySweep:
    def test_onboarding_and_update_validators_accept_the_plan(self):
        from app.routers.clinics import CreateClinicRequest, UpdateClinicRequest

        req = CreateClinicRequest(
            name="Rainbow Children's", whatsapp_number="+919876543210", plan=PLAN,
            meta_phone_number_id="000000000000", meta_access_token="EAAG_test",
        )
        assert req.plan == PLAN
        assert UpdateClinicRequest(plan=PLAN).plan == PLAN

    def test_platform_router_lists_the_plan(self):
        src = (REPO / "app" / "routers" / "platform.py").read_text(encoding="utf-8")
        assert f'"{PLAN}"' in src.split("valid_plans = {")[1].split("}")[0]
        assert f'"{PLAN}": 0' in src.split("clinics_by_plan = {")[1].split("}")[0]

    def test_message_accounting_fallback_lists_the_plan(self):
        src = (REPO / "app" / "services" / "message_accounting.py").read_text(encoding="utf-8")
        assert re.search(rf'"{PLAN}": \{{"included_messages_month": 5000', src)

    def test_platform_console_offers_styles_and_filters_the_plan(self):
        html = (REPO / "admin" / "platform.html").read_text(encoding="utf-8")
        assert f".badge-{PLAN} " in html
        assert f'value="{PLAN}"' in html.split('id="ccPlan"')[1].split("</select>")[0]
        assert f'value="{PLAN}"' in html.split('id="planFilter"')[1].split("</select>")[0]
        assert f"'{PLAN}'" in re.search(r"const PLANS_WITH_LAB_REPORTS = \[([^\]]+)\]", html).group(1)

    def test_the_panel_url_serves_the_admin_panel(self):
        src = (REPO / "app" / "main.py").read_text(encoding="utf-8")
        block = src.split('@app.get("/derma-panel")')[1].split("async def")[0]
        assert '@app.get("/women-child-panel")' in block

    def test_migration_082_widens_both_plan_constraints_and_adds_the_column(self):
        sql = (REPO / "migrations" / "082_women_child_plan.sql").read_text(encoding="utf-8")
        assert sql.count(f"'{PLAN}'") >= 4
        assert "ON CONFLICT (plan_name) DO NOTHING" in sql
        assert "ADD COLUMN IF NOT EXISTS service_line TEXT" in sql
        assert "UPDATE specialty_treatments" not in sql  # no backfill: every row stays NULL
        assert (REPO / "migrations" / "rollback" / "082_down.sql").exists()

    def test_the_sql_check_and_the_python_registry_cannot_drift(self):
        sql = (REPO / "migrations" / "082_women_child_plan.sql").read_text(encoding="utf-8")
        block = sql.split("service_line IS NULL OR service_line IN (", 1)[1].split(")", 1)[0]
        assert set(re.findall(r"'([a-z_]+)'", block)) == set(SERVICE_LINES)


# ── catalogue ────────────────────────────────────────────────────────────────


class TestStarterCatalogue:
    def test_the_plan_seeds_child_women_and_fertility(self):
        assert STARTER_LISTS_BY_PLAN[PLAN] == ("pediatrics", "womens_health", "fertility")
        assert [STARTER_SERVICE_LINE[s] for s in STARTER_LISTS_BY_PLAN[PLAN]] == [
            "child_care", "women_care", "fertility_care",
        ]

    def test_every_starter_list_maps_to_a_known_line(self):
        assert set(STARTER_SERVICE_LINE) == set(STARTER_TREATMENTS)
        assert set(STARTER_SERVICE_LINE.values()) <= set(SERVICE_LINES)

    def test_names_are_unique_across_the_three_lists(self):
        """The seeder skips a name the clinic already has, so a duplicate would
        silently drop a row from the second list."""
        names = [t["name"].strip().lower() for s in STARTER_LISTS_BY_PLAN[PLAN] for t in STARTER_TREATMENTS[s]]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("specialty", ["pediatrics", "womens_health"])
    def test_each_new_list_opens_with_one_first_visit_consultation(self, specialty):
        rows = STARTER_TREATMENTS[specialty]
        assert rows[0]["care_pathway"] == "entry"
        assert sum(r["care_pathway"] == "entry" for r in rows) == 1

    def test_the_obstetrician_decides_how_a_baby_is_delivered(self):
        for name in ("Childbirth & Delivery Care", "Labour Pain Relief (Epidural)",
                     "Birth After Caesarean (VBAC)", "Newborn Intensive Care (NICU)"):
            assert _PATHWAY_BY_NAME[name] == "assessment_first"

    def test_the_scan_row_states_the_pcpndt_rule(self):
        scan = next(t for t in STARTER_TREATMENTS["womens_health"] if t["name"] == "Fetal Medicine & Scans")
        assert "sex of the baby is not disclosed" in scan["prep_instructions"]

    def test_line_labels_fit_a_whatsapp_row_in_every_language(self):
        for slug in list(SERVICE_LINES) + [sf.LINE_OTHER]:
            for lang in ("en", "hi", "te"):
                assert len(sf.line_label(slug, lang)) <= 24, (slug, lang)

    def test_concern_examples_speak_to_this_hospital(self):
        assert "child" in CONCERN_EXAMPLES_BY_PLAN[PLAN] and "pregnancy" in CONCERN_EXAMPLES_BY_PLAN[PLAN]


class TestSeeding:
    @staticmethod
    async def _seed(existing, **kwargs):
        fake = MagicMock()
        with patch("app.services.specialty_catalog.supabase", fake), \
             patch("app.services.specialty_catalog.sb",
                   AsyncMock(side_effect=[MagicMock(data=existing), MagicMock(data=[])])):
            await seed_starter_treatments("c1", "pediatrics", **kwargs)
        return fake.table.return_value.insert.call_args.args[0]

    @pytest.mark.asyncio
    async def test_the_line_is_written_only_when_asked(self):
        rows = await self._seed([], service_line="child_care")
        assert {r["service_line"] for r in rows} == {"child_care"}
        # A single-specialty clinic's insert carries no new key at all.
        rows = await self._seed([])
        assert all("service_line" not in r for r in rows)

    @pytest.mark.asyncio
    async def test_an_unknown_line_is_never_written(self):
        rows = await self._seed([], service_line="cardiology")
        assert all("service_line" not in r for r in rows)

    @pytest.mark.asyncio
    async def test_a_second_list_is_ordered_after_the_first(self):
        rows = await self._seed([{"name": "Gynaecology Consultation", "display_order": 200}])
        assert min(r["display_order"] for r in rows) > 200

    @pytest.mark.asyncio
    async def test_onboarding_seeds_all_three_lists_each_under_its_line(self):
        from app.routers import clinics

        seed = AsyncMock(return_value={"added": 5, "skipped": 1})
        with patch.object(clinics, "seed_starter_treatments", seed):
            total = await clinics.seed_plan_starter_lists("c1", PLAN)
        assert [c.args[1] for c in seed.await_args_list] == ["pediatrics", "womens_health", "fertility"]
        assert [c.kwargs["service_line"] for c in seed.await_args_list] == [
            "child_care", "women_care", "fertility_care",
        ]
        assert total == {"added": 15, "skipped": 3}

    @pytest.mark.asyncio
    async def test_one_failing_list_does_not_stop_the_others_or_onboarding(self):
        from app.routers import clinics

        seed = AsyncMock(side_effect=[RuntimeError("db"), {"added": 2, "skipped": 0}, {"added": 3, "skipped": 0}])
        with patch.object(clinics, "seed_starter_treatments", seed):
            total = await clinics.seed_plan_starter_lists("c1", PLAN)
        assert total == {"added": 5, "skipped": 0}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan", ["derma", "multispecialty", "polyclinic", "soloclinic"])
    async def test_no_other_plan_seeds_anything_new(self, plan):
        from app.routers import clinics

        seed = AsyncMock()
        with patch.object(clinics, "seed_starter_treatments", seed):
            assert await clinics.seed_plan_starter_lists("c1", plan) is None
        seed.assert_not_awaited()


# ── admin API ────────────────────────────────────────────────────────────────


class TestAdminApi:
    def test_service_line_is_validated_and_normalised(self):
        from pydantic import ValidationError

        from app.routers.admin import TreatmentCreate, TreatmentUpdate

        assert TreatmentCreate(name="X", category="C").service_line is None
        assert TreatmentCreate(name="X", category="C", service_line=" Child_Care ").service_line == "child_care"
        assert TreatmentUpdate(service_line="").service_line is None
        with pytest.raises(ValidationError):
            TreatmentCreate(name="X", category="C", service_line="cardiology")

    def test_a_new_row_without_a_line_is_sent_exactly_as_before(self):
        from app.routers.admin import TreatmentCreate, TreatmentUpdate, _treatment_row

        assert "service_line" not in _treatment_row(TreatmentCreate(name="X", category="C"), partial=False)
        assert _treatment_row(TreatmentCreate(name="X", category="C", service_line="women_care"),
                              partial=False)["service_line"] == "women_care"
        # An update that omits it leaves it alone; an explicit blank clears it.
        assert "service_line" not in _treatment_row(TreatmentUpdate(name="Y"), partial=True)
        assert _treatment_row(TreatmentUpdate(service_line=""), partial=True) == {"service_line": None}

    @pytest.mark.parametrize("plan, specialty, expected", [
        (PLAN, "pediatrics", "child_care"),
        (PLAN, "womens_health", "women_care"),
        ("multispecialty", "dermatology", "skin_hair"),
        ("derma", "dermatology", None),
        ("ivf", "fertility", None),
    ])
    def test_starter_lists_are_filed_only_for_hybrid_plans(self, plan, specialty, expected):
        from app.routers.admin import _starter_service_line

        assert _starter_service_line({"plan": plan}, specialty) == expected

    def test_starter_request_accepts_the_new_lists_and_a_hybrid_must_choose(self):
        from fastapi import HTTPException

        from app.routers.admin import TreatmentStarterRequest, _starter_specialty

        assert TreatmentStarterRequest(specialty="pediatrics").specialty == "pediatrics"
        assert _starter_specialty({"plan": PLAN}, "womens_health") == "womens_health"
        with pytest.raises(HTTPException) as missing:
            _starter_specialty({"plan": PLAN}, None)
        assert missing.value.status_code == 400

    @pytest.mark.asyncio
    async def test_admin_me_describes_the_sections(self):
        from app.routers.admin import AdminUser, get_current_admin

        user = AdminUser("wc", role="clinic_admin", clinic_id="clinic-wc", user_id="u1")
        with patch("app.routers.admin.get_clinic_by_id", AsyncMock(return_value={"id": "clinic-wc", "plan": PLAN})):
            me = await get_current_admin(user=user)
        assert me["specialty"] is None and me["specialty_enabled"] is True
        assert me["plan_service_lines"] == ["child_care", "women_care", "fertility_care"]
        assert {line["slug"] for line in me["service_lines"]} == set(SERVICE_LINES)

    @pytest.mark.asyncio
    async def test_admin_me_for_a_single_specialty_clinic_has_no_plan_sections(self):
        from app.routers.admin import AdminUser, get_current_admin

        user = AdminUser("d", role="clinic_admin", clinic_id="c-d", user_id="u2")
        with patch("app.routers.admin.get_clinic_by_id", AsyncMock(return_value={"id": "c-d", "plan": "derma"})):
            me = await get_current_admin(user=user)
        assert me["plan_service_lines"] == []

    def test_admin_panel_offers_sections_and_the_new_starter_lists(self):
        html = (REPO / "admin" / "index.html").read_text(encoding="utf-8")
        picker = html.split('id="trtStarterSpecialty"')[1].split("</select>")[0]
        for specialty in STARTER_TREATMENTS:
            assert f'value="{specialty}"' in picker
        assert 'id="trtLineTabs"' in html and 'id="f-trtLine"' in html
        # The field is only ever sent by a clinic that sees it.
        assert "if (treatmentSectionsOn()) payload.service_line" in html


# ── WhatsApp: the unsplit catalogue is untouched ─────────────────────────────


def _row(i, name, category, line=None, pathway="direct", **extra):
    row = {
        "id": f"{i:08d}-0000-0000-0000-000000000000",
        "name": name, "short_name": None, "category": category,
        "display_order": i, "is_active": True,
        "description": f"What {name} is.\nWhat to expect.",
        "price_from_paise": 0, "care_pathway": pathway,
    }
    if line is not None:
        row["service_line"] = line
    row.update(extra)
    return row


def _stateful_manager():
    """A manager whose conversation context persists across taps, as it does
    in production (update_state merges into the stored context)."""
    m = MagicMock()
    m.ctx = {}

    async def update_state(_clinic, _phone, _state, new_context=None, reset_context=False):
        m.ctx = dict(new_context or {}) if reset_context else {**m.ctx, **(new_context or {})}

    m.update_state = AsyncMock(side_effect=update_state)
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    m._send_main_menu = AsyncMock()
    m._start_booking = AsyncMock()
    m._handle_emergency = AsyncMock()
    m._page_rows = ConversationManager.__new__(ConversationManager)._page_rows
    return m


async def _tap(m, button_id, lang="en"):
    await sf.handle_treatment_button(m, CLINIC, PHONE, button_id, {"context": dict(m.ctx)}, lang)
    return m.whatsapp.send_interactive_list.await_args.kwargs


def _ids(sent):
    return [r["id"] for s in sent["sections"] for r in s["rows"]]


class TestUnsplitCatalogueIsUnchanged:
    @pytest.mark.parametrize("rows", [
        # every row read before migration 082: no key at all
        [_row(1, "Acne", "Acne"), _row(2, "Peel", "Pigment")],
        # everything under ONE line (a single-specialty clinic's new seed)
        [_row(1, "Acne", "Acne", "skin_hair"), _row(2, "Peel", "Pigment", "skin_hair")],
        # one line plus legacy rows
        [_row(1, "Acne", "Acne", "skin_hair"), _row(2, "Peel", "Pigment")],
        # garbage in the column
        [_row(1, "Acne", "Acne", "cardio"), _row(2, "Peel", "Pigment", 7)],
    ])
    def test_no_line_picker(self, rows):
        assert sf.service_lines_in(rows) == []
        assert sf.in_line(rows, "child_care") == rows

    @pytest.mark.asyncio
    async def test_browsing_opens_on_categories_and_context_keeps_its_shape(self):
        rows = [_row(1, "Acne", "Acne"), _row(2, "Peel", "Pigment")]
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=rows)):
            sent = await _tap(m, "menu_treatments")
        assert sent["header"] == "Our Treatments"
        assert _ids(sent) == ["trtcat_0", "trtcat_1"]
        assert "treatment_line" not in m.ctx


# ── WhatsApp: the split catalogue ────────────────────────────────────────────


SPLIT = [
    _row(1, "Paediatric Consultation", "Child Consultations", "child_care", "entry"),
    _row(2, "Vaccination", "Child Consultations", "child_care"),
    _row(3, "Child Surgery", "Surgery", "child_care"),
    _row(10, "Gynaecology Consultation", "Gynaecology", "women_care", "entry"),
    _row(11, "Gynae Laparoscopy", "Surgery", "women_care", "assessment_first"),
    _row(20, "IVF", "Surgery", "fertility_care", "assessment_first"),
    _row(30, "Legacy Row", "Misc"),
]


class TestSplitCatalogue:
    def test_lines_follow_the_admin_order_and_legacy_rows_get_other(self):
        assert sf.service_lines_in(SPLIT) == ["child_care", "women_care", "fertility_care", sf.LINE_OTHER]
        assert [t["name"] for t in sf.in_line(SPLIT, sf.LINE_OTHER)] == ["Legacy Row"]

    @pytest.mark.asyncio
    async def test_first_tap_is_the_line_picker(self):
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=SPLIT)):
            sent = await _tap(m, "menu_treatments")
        assert _ids(sent) == ["trtline_child_care", "trtline_women_care", "trtline_fertility_care", "trtline_other"]
        assert sent["sections"][0]["rows"][0]["title"] == "👶 Child Care"

    @pytest.mark.asyncio
    async def test_a_line_shows_only_its_own_categories(self):
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=SPLIT)):
            await _tap(m, "menu_treatments")
            sent = await _tap(m, "trtline_child_care")
        assert sent["header"] == "👶 Child Care"
        assert [r["title"] for r in sent["sections"][0]["rows"]] == ["Child Consultations", "Surgery"]
        assert m.ctx["treatment_line"] == "child_care"

    @pytest.mark.asyncio
    async def test_the_same_category_in_two_lines_never_mixes(self):
        """"Surgery" exists in Child, Women and Fertility care."""
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=SPLIT)):
            await _tap(m, "menu_treatments")
            await _tap(m, "trtline_women_care")
            idx = m.ctx["treatment_categories"].index("Surgery")
            sent = await _tap(m, f"trtcat_{idx}")
        assert [r["title"] for r in sent["sections"][0]["rows"]] == ["Gynae Laparoscopy"]

    @pytest.mark.asyncio
    async def test_a_single_category_line_goes_straight_to_its_treatments(self):
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=SPLIT)):
            await _tap(m, "menu_treatments")
            sent = await _tap(m, "trtline_fertility_care")
        assert [r["title"] for r in sent["sections"][0]["rows"]] == ["IVF"]
        assert m.ctx["treatment_line"] == "fertility_care"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale", ["trtline_eye_care", "trtline_", "trtline_DROP TABLE", "trtline_other"])
    async def test_a_stale_or_hostile_line_tap_reshows_the_picker(self, stale):
        rows = [r for r in SPLIT if r["name"] != "Legacy Row"]  # no Other line now
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=rows)):
            sent = await _tap(m, stale)
        assert _ids(sent) == ["trtline_child_care", "trtline_women_care", "trtline_fertility_care"]

    @pytest.mark.asyncio
    async def test_all_treatments_from_a_card_returns_to_the_picker(self):
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=SPLIT)):
            await _tap(m, "trtline_child_care")
            sent = await _tap(m, "menu_treatments")
        assert _ids(sent)[0] == "trtline_child_care"
        assert m.ctx["treatment_line"] is None

    def test_the_line_button_is_routed_and_its_context_is_cleaned_up(self):
        assert "trtline_child_care".startswith(sf.TREATMENT_BUTTON_PREFIXES)
        assert "treatment_line" in sf.TREATMENT_CONTEXT_KEYS


# ── WhatsApp: the real catalogue, every tap, every language ─────────────────


def _full_catalogue():
    rows, i = [], 0
    for specialty in STARTER_LISTS_BY_PLAN[PLAN]:
        for item in STARTER_TREATMENTS[specialty]:
            i += 1
            rows.append(_row(i * 10, item["name"], item["category"], STARTER_SERVICE_LINE[specialty],
                             item["care_pathway"], short_name=item["short_name"],
                             description=item["description"], duration_minutes=item["duration_minutes"],
                             prep_instructions=item["prep_instructions"]))
    return rows


def _meta_violations(kwargs):
    bad = []
    rows = [r for s in kwargs.get("sections", []) for r in s["rows"]]
    ids = [r["id"] for r in rows]
    if len(rows) > 10:
        bad.append(f"{len(rows)} rows")
    if len(ids) != len(set(ids)) or not all(ids):
        bad.append("row ids not unique/non-empty")
    for r in rows:
        if not r["title"] or len(r["title"]) > 24:
            bad.append(f"title {r['title']!r}")
        if len(r.get("description") or "") > 72:
            bad.append(f"description of {r['title']!r}")
    if len(kwargs.get("body") or "") > 1024:
        bad.append("body > 1024")
    if len(kwargs.get("header") or "") > 60:
        bad.append("header > 60")
    return bad


class TestFullCatalogueWalk:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("lang", ["en", "hi", "te"])
    async def test_every_treatment_is_reachable_within_meta_limits(self, lang):
        catalogue = _full_catalogue()
        assert len(catalogue) > 40
        m = _stateful_manager()
        reached, violations = set(), []
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=catalogue)):
            picker = await _tap(m, "menu_treatments", lang)
            violations += _meta_violations(picker)
            for line_id in _ids(picker):
                first = await _tap(m, line_id, lang)
                violations += _meta_violations(first)
                categories = list(m.ctx.get("treatment_categories") or [])
                pages = []
                for idx in range(len(categories)):
                    await _tap(m, line_id, lang)
                    pages.append(await _tap(m, f"trtcat_{idx}", lang))
                for page in pages:
                    for _ in range(10):
                        violations += _meta_violations(page)
                        reached |= {r["id"][4:] for r in page["sections"][0]["rows"] if r["id"].startswith("trt_")}
                        if "trt_more" not in _ids(page):
                            break
                        page = await _tap(m, "trt_more", lang)
        assert not violations, violations
        assert reached == {t["id"] for t in catalogue}

    @pytest.mark.asyncio
    async def test_three_first_visit_rows_are_offered_not_guessed(self):
        catalogue = _full_catalogue()
        m = _stateful_manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=catalogue)):
            sent = await _tap(m, "menu_entry_consult")
        titles = [r["title"] for s in sent["sections"] for r in s["rows"]]
        assert titles == ["Child Consultation", "Gynae Consultation", "Fertility Consultation"]
        assert not _meta_violations(sent)


class TestMainMenu:
    @pytest.mark.asyncio
    async def test_menu_keeps_departments_and_lab_and_fits_ten_rows(self):
        m = ConversationManager()
        m.whatsapp = MagicMock()
        m.whatsapp.send_interactive_list = AsyncMock()
        with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
             patch.object(sf, "has_active_treatments", AsyncMock(return_value=True)), \
             patch.object(sf, "has_entry_treatment", AsyncMock(return_value=True)):
            await m._send_main_menu(CLINIC, PHONE, "en")
        rows = [r for s in m.whatsapp.send_interactive_list.await_args.kwargs["sections"] for r in s["rows"]]
        ids = [r["id"] for r in rows]
        assert ids[:3] == ["menu_entry_consult", "menu_concern", "menu_treatments"]
        assert {"menu_services", "menu_doctors", "menu_lab_tests", "menu_emergency"} <= set(ids)
        assert len(rows) <= 10

    @pytest.mark.asyncio
    async def test_concern_prompt_uses_women_and_child_examples(self):
        m = _stateful_manager()
        with patch.object(sf, "has_entry_treatment", AsyncMock(return_value=True)):
            await sf.prompt_concern(m, CLINIC, PHONE, "en")
        assert CONCERN_EXAMPLES_BY_PLAN[PLAN] in m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]


# ── safety: obstetric / newborn emergencies, PCPNDT ─────────────────────────


class TestEmergencies:
    @pytest.mark.parametrize("message", [
        "my water broke", "Waters broke 10 mins ago", "labour has started", "labor pains started",
        "baby is not moving since morning", "the baby turned blue", "child having convulsions",
        "पानी की थैली फट गई", "बच्चा हिल नहीं रहा",
    ])
    def test_red_flags_are_emergencies(self, message):
        assert keyword_intent_fallback(message) == "emergency"

    @pytest.mark.parametrize("message", [
        "do you offer painless delivery?", "labour pain relief cost", "epidural charges",
        "book pregnancy scan", "vaccination for my baby", "normal delivery package price",
    ])
    def test_ordinary_questions_are_not(self, message):
        assert not any(kw in message.lower() for kw in EMERGENCY_KEYWORDS)


class TestPcpndt:
    @pytest.mark.parametrize("message", [
        "Can you tell the gender of the baby?", "baby gender scan", "what is the sex of my baby",
        "is it a boy or girl in the scan", "I want to know the gender, I am pregnant",
        "sex determination", "gender test", "babys gender pls",
        "बच्चा लड़का है या लड़की", "లింగ నిర్ధారణ చేస్తారా",
    ])
    def test_sex_determination_is_refused_with_the_law(self, message):
        assert is_sex_determination_request(message)
        for lang in ("en", "hi", "te"):
            blocked, reply = screen_message(message, lang)
            assert blocked and "PCPNDT" in reply

    @pytest.mark.parametrize("message", [
        "Gender: Female, need pregnancy scan", "book pregnancy scan", "my baby has fever",
        "female doctor for my pregnancy check", "sexual health consultation",
        "I am a girl, 22, irregular periods", "boy or girl ward", "Name Ravi, sex male, fever",
    ])
    def test_a_patient_giving_her_own_details_is_not_refused(self, message):
        assert not is_sex_determination_request(message)
