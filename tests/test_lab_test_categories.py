"""Service types on the diagnostic catalogue (migration 080).

A diagnostic centre does not sell one flat list of blood tests. It sells
pathology, health packages, radiology/imaging and scans, and dropping all of
it into one 1,392-row WhatsApp list hands the patient the centre's filing
problem. `lab_tests.category` is that filing; the headings the bot offers are
whatever the centre's own rows carry, so there is nothing to switch on.

The load-bearing property for a LIVE centre is the first class below: a
catalogue with nothing filed must behave exactly as it did before.
"""

import io
import os
import sys

import pytest
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.services.conversation import ConversationManager  # noqa: E402

CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
PHONE = "+919999999999"

PATHOLOGY = "Lab Tests (Pathology)"
RADIOLOGY = "Radiology & Imaging"
PACKAGES = "Health Packages"
MAIN_MENU_TITLE = "\U0001f3e0 Main Menu"


def _test(name, category=None, tid=None, price=50000):
    return {
        "id": tid or name.lower().replace(" ", "-"),
        "name": name,
        "price_paise": price,
        "sample_type": "Blood",
        "is_active": True,
        "branch_id": None,
        "category": category,
    }


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    m.whatsapp.send_text = AsyncMock(return_value=True)
    m.update_state = AsyncMock()
    m._send_main_menu = AsyncMock()
    return m


def _sent(manager):
    kwargs = manager.whatsapp.send_interactive_list.await_args.kwargs
    section = kwargs["sections"][0]
    return section["rows"], kwargs["body"], section["title"]


# -- The live-centre guarantee ------------------------------------------------


class TestUnfiledCatalogueIsUnchanged:
    """Every catalogue imported before migration 080 has category NULL."""

    @pytest.mark.asyncio
    async def test_no_category_step_when_nothing_is_filed(self):
        m = _manager()
        catalogue = [_test("CBC"), _test("Lipid Profile"), _test("TSH")]
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=catalogue):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en")

        rows, _, _ = _sent(m)
        # Straight to the tests, exactly as before -- no heading list.
        assert [r["id"] for r in rows][:3] == [
            "labtest_cbc", "labtest_lipid-profile", "labtest_tsh",
        ]
        assert not any(r["id"].startswith("labcat_") for r in rows)

    @pytest.mark.asyncio
    async def test_one_heading_is_also_no_step(self):
        """A centre that files everything under one name gains no extra tap."""
        m = _manager()
        catalogue = [_test("CBC", PATHOLOGY), _test("TSH", PATHOLOGY)]
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=catalogue):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en")

        rows, _, _ = _sent(m)
        assert not any(r["id"].startswith("labcat_") for r in rows)


# -- Grouping ----------------------------------------------------------------


class TestGrouping:
    def test_unfiled_rows_land_under_one_bucket(self):
        groups = ConversationManager._group_lab_tests_by_category(
            [_test("CBC"), _test("TSH", "")]
        )
        assert [label for label, _ in groups] == [
            ConversationManager.LAB_UNCATEGORISED_LABEL
        ]

    def test_case_and_padding_do_not_split_a_heading(self):
        """One CSV typed "radiology", another "Radiology" -- still one menu."""
        groups = ConversationManager._group_lab_tests_by_category([
            _test("X-Ray", "Radiology"),
            _test("MRI Brain", "radiology"),
            _test("CT Chest", "  Radiology  "),
        ])
        assert len(groups) == 1
        assert groups[0][0] == "Radiology"
        assert len(groups[0][1]) == 3

    def test_largest_group_leads_and_ties_are_stable(self):
        groups = ConversationManager._group_lab_tests_by_category(
            [_test(f"P{i}", PATHOLOGY) for i in range(5)]
            + [_test("MRI", RADIOLOGY), _test("CT", RADIOLOGY)]
            + [_test("Full Body", PACKAGES), _test("Cardiac Pkg", PACKAGES)]
        )
        assert [label for label, _ in groups] == [PATHOLOGY, PACKAGES, RADIOLOGY]


# -- The category step -------------------------------------------------------


MIXED = (
    [_test(f"Blood {i:02d}", PATHOLOGY) for i in range(20)]
    + [_test("MRI Brain", RADIOLOGY), _test("Chest X-Ray", RADIOLOGY)]
    + [_test("Master Health Checkup", PACKAGES)]
)


class TestCategoryStep:
    @pytest.mark.asyncio
    async def test_headings_are_offered_first_with_counts(self):
        m = _manager()
        ctx = {}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en")

        rows, body, _ = _sent(m)
        # Each heading leads with an icon read off its own wording.
        title = m._lab_heading_title
        assert [r["title"] for r in rows] == [
            title(PATHOLOGY), title(RADIOLOGY), title(PACKAGES), MAIN_MENU_TITLE,
        ]
        assert rows[0]["title"].startswith("\U0001F9EA")  # the lab icon, not a bare heading
        assert rows[0]["description"].startswith("20")
        assert rows[1]["description"].startswith("2")
        assert "What would you like to book" in body
        # Stored so a tap can be resolved back to the heading it names.
        assert ctx["lab_categories"] == [PATHOLOGY, RADIOLOGY, PACKAGES]

    @pytest.mark.asyncio
    async def test_a_heading_with_no_tests_is_simply_absent(self):
        """The admin-side switch: stop offering imaging, stop listing it."""
        m = _manager()
        without_imaging = [t for t in MIXED if t["category"] != RADIOLOGY]
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=without_imaging):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en")

        rows, _, _ = _sent(m)
        assert RADIOLOGY[:24] not in [r["title"] for r in rows]

    @pytest.mark.asyncio
    async def test_tapping_a_heading_shows_only_its_tests(self):
        m = _manager()
        ctx = {"lab_categories": [PATHOLOGY, RADIOLOGY, PACKAGES]}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._handle_browsing_lab_tests(
                CLINIC, PHONE, "", "unknown", ctx, "en", {"id": "labcat_1"}
            )

        rows, _, title = _sent(m)
        assert title == RADIOLOGY[:24]
        # Catalogue order is preserved; get_lab_tests already sorts by name.
        assert [r["title"] for r in rows if r["id"].startswith("labtest_")] == [
            "MRI Brain", "Chest X-Ray",
        ]
        assert ctx["lab_category"] == RADIOLOGY

    @pytest.mark.asyncio
    async def test_a_filtered_list_offers_a_way_back_to_the_headings(self):
        m = _manager()
        ctx = {"lab_category": RADIOLOGY}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en")

        rows, _, _ = _sent(m)
        assert [r["id"] for r in rows[-2:]] == ["labcat_all", "lab_menu"]

    @pytest.mark.asyncio
    async def test_all_services_row_returns_to_the_headings(self):
        m = _manager()
        ctx = {
            "lab_category": RADIOLOGY,
            "lab_categories": [PATHOLOGY, RADIOLOGY, PACKAGES],
        }
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._handle_browsing_lab_tests(
                CLINIC, PHONE, "", "unknown", ctx, "en", {"id": "labcat_all"}
            )

        rows, _, _ = _sent(m)
        assert [r["id"] for r in rows][:3] == ["labcat_0", "labcat_1", "labcat_2"]
        assert "lab_category" not in ctx

    @pytest.mark.asyncio
    async def test_a_deleted_heading_falls_back_instead_of_showing_nothing(self):
        """The list stays tappable forever; the catalogue does not."""
        m = _manager()
        ctx = {"lab_category": "Genetics"}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en")

        rows, _, _ = _sent(m)
        assert [r["id"] for r in rows][:3] == ["labcat_0", "labcat_1", "labcat_2"]

    @pytest.mark.asyncio
    async def test_paging_stays_inside_the_chosen_heading(self):
        m = _manager()
        ctx = {"lab_category": PATHOLOGY, "lab_test_page": 0}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._handle_browsing_lab_tests(
                CLINIC, PHONE, "", "unknown", ctx, "en", {"id": "labtest_more"}
            )

        rows, _, _ = _sent(m)
        tests = [
            r["title"] for r in rows
            if r["id"].startswith("labtest_") and r["id"] != "labtest_more"
        ]
        assert tests and all(t.startswith("Blood") for t in tests)
        assert len(rows) <= 10

    @pytest.mark.asyncio
    async def test_search_inside_a_heading_does_not_escape_it(self):
        m = _manager()
        ctx = {"lab_category": RADIOLOGY}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en", query="brain")

        rows, _, _ = _sent(m)
        assert [r["title"] for r in rows if r["id"].startswith("labtest_")] == ["MRI Brain"]

    @pytest.mark.asyncio
    async def test_typing_before_choosing_searches_the_whole_catalogue(self):
        """Search is the fast path and must not need a heading chosen first."""
        m = _manager()
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en", query="mri")

        rows, _, _ = _sent(m)
        assert [r["title"] for r in rows if r["id"].startswith("labtest_")] == ["MRI Brain"]

    @pytest.mark.asyncio
    async def test_many_headings_page_without_losing_row_ids(self):
        m = _manager()
        many = []
        for n in range(12):
            many += [_test(f"T{n}-{i}", f"Section {n:02d}") for i in range(12 - n)]
        ctx = {}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=many):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en")
        first, _, _ = _sent(m)
        assert len(first) <= 10
        assert first[-2]["id"] == "labcat_more"

        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=many):
            await m._handle_browsing_lab_tests(
                CLINIC, PHONE, "", "unknown", ctx, "en", {"id": "labcat_more"}
            )
        second, _, _ = _sent(m)
        # Ids index the full heading list, not the page, so page 2's first row
        # resolves to the 9th heading and not the 1st.
        assert second[0]["id"] == "labcat_8"
        assert ctx["lab_categories"][8] == "Section 08"


class TestMainMenuRow:
    @pytest.mark.asyncio
    async def test_main_menu_row_leaves_the_catalogue(self):
        m = _manager()
        await m._handle_browsing_lab_tests(
            CLINIC, PHONE, "", "unknown", {}, "en", {"id": "lab_menu"}
        )
        m._send_main_menu.assert_awaited_once()
        assert m.update_state.await_args.args[2] == "main_menu"

    @pytest.mark.asyncio
    async def test_main_menu_row_is_never_parsed_as_a_test_id(self):
        m = _manager()
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=MIXED):
            await m._show_lab_test_list(CLINIC, PHONE, {"lab_category": PACKAGES}, "en")
        rows, _, _ = _sent(m)
        menu_rows = [r for r in rows if r["title"] == MAIN_MENU_TITLE]
        assert len(menu_rows) == 1
        assert not menu_rows[0]["id"].startswith("labtest_")


# -- Admin: CSV import and the per-test form ---------------------------------


def _paged_catalogue(rows):
    page = MagicMock()
    page.data = rows
    q = MagicMock()
    for attr in ("eq", "is_", "or_", "order", "range"):
        getattr(q, attr).return_value = q
    q.execute.return_value = page
    return q


def _admin_user():
    from app.routers.admin import AdminUser

    user = AdminUser("staff-user")
    user.username = "labstaff"
    user.role = "staff"
    user.clinic_id = "clinic-1"
    user.user_id = "user-1"
    user.permissions = ["LAB_TESTS_MANAGE"]
    user.branch_id = None
    return user


def _admin_app():
    from app.routers.admin import router, verify_credentials

    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return _admin_user()

    app.dependency_overrides[verify_credentials] = fake_user
    return app


def _import(csv_text, category=None, existing=None):
    from app.routers import admin as admin_module

    app = _admin_app()
    lookup = _paged_catalogue(existing or [])
    with patch.object(admin_module, "supabase") as sb, patch.object(
        admin_module, "resolve_clinic_id_for_write", new_callable=AsyncMock,
        return_value="clinic-1",
    ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
        sb.table.return_value.select.return_value = lookup
        sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "x"}]
        )
        sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "x"}]
        )
        data = {"category": category} if category is not None else {}
        resp = TestClient(app).post(
            "/admin/lab-tests/import-csv",
            files={"file": ("tests.csv", io.BytesIO(csv_text.encode()), "text/csv")},
            data=data,
        )
        inserts = [c[0][0] for c in sb.table.return_value.insert.call_args_list]
        updates = [c[0][0] for c in sb.table.return_value.update.call_args_list]
    return resp, inserts, updates


class TestCsvCategory:
    def test_file_level_category_files_every_row(self):
        """One export per section is how a centre actually holds a catalogue."""
        resp, inserts, _ = _import(
            "name,price_rupees\nMRI Brain,6500\nCT Chest,4500\n", category=RADIOLOGY
        )
        assert resp.status_code == 200
        assert resp.json()["category"] == RADIOLOGY
        assert [r["category"] for r in inserts] == [RADIOLOGY, RADIOLOGY]

    def test_per_row_column_beats_the_file_level_default(self):
        resp, inserts, _ = _import(
            "name,price_rupees,category\nMRI Brain,6500,\nMaster Checkup,2500,Health Packages\n",
            category=RADIOLOGY,
        )
        assert resp.status_code == 200
        assert [r["category"] for r in inserts] == [RADIOLOGY, "Health Packages"]

    def test_a_lis_export_department_column_is_understood(self):
        resp, inserts, _ = _import("name,price_rupees,Department\nCBC,350,Haematology\n")
        assert resp.status_code == 200
        assert inserts[0]["category"] == "Haematology"

    def test_a_plain_price_list_never_wipes_filed_headings(self):
        """The reason the key is omitted rather than written as None."""
        resp, _, updates = _import(
            "name,price_rupees\nCBC,700\n", existing=[{"id": "t1", "name": "CBC"}]
        )
        assert resp.status_code == 200
        assert updates[0]["price_paise"] == 70000
        assert "category" not in updates[0]

    def test_an_over_long_heading_is_rejected_before_any_write(self):
        resp, inserts, updates = _import(
            "name,price_rupees,category\nCBC,350," + ("x" * 61) + "\n"
        )
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["column"] == "category"
        assert inserts == [] and updates == []

    def test_template_carries_the_column_and_real_examples(self):
        app = _admin_app()
        body = TestClient(app).get("/admin/lab-tests/csv-template").text
        assert body.splitlines()[0].split(",")[2] == "category"
        for heading in ("Health Packages", "Radiology & Imaging", "Scans (CT / MRI)"):
            assert heading in body


class TestModelValidation:
    def test_blank_collapses_to_none_so_the_bucket_is_not_shown_twice(self):
        from app.routers.admin import LabTestCreate

        assert LabTestCreate(name="CBC", price_rupees=100, category="   ").category is None

    def test_padding_is_stripped(self):
        from app.routers.admin import LabTestCreate

        got = LabTestCreate(name="CBC", price_rupees=100, category="  Radiology ")
        assert got.category == "Radiology"

    def test_over_long_heading_is_refused(self):
        from pydantic import ValidationError

        from app.routers.admin import LabTestCreate

        with pytest.raises(ValidationError):
            LabTestCreate(name="CBC", price_rupees=100, category="y" * 61)

    def test_update_can_clear_a_heading(self):
        from app.routers.admin import LabTestUpdate

        assert LabTestUpdate(category="").model_dump(exclude_unset=True) == {"category": None}

    def test_update_that_omits_it_leaves_it_alone(self):
        from app.routers.admin import LabTestUpdate

        assert "category" not in LabTestUpdate(price_rupees=500).model_dump(exclude_unset=True)
