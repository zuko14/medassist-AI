"""Per-branch diagnostic catalogues.

A diagnostic chain with three collection centres does not run one menu. Some
tests are offered everywhere, some only at the centre with the analyser, and a
shared test can carry a different price per centre. The rule the whole stack
has to agree on:

    branch_id NULL          -> offered at every branch
    branch_id = B           -> offered at B only, and OVERRIDES an
                               all-branches row of the same name at B

The admin panel writes it, the WhatsApp catalogue reads it, and the booking
handler re-checks it. These tests pin all three ends, because a disagreement
between them books a patient into a test their centre does not run.
"""

import io
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

KUKATPALLY = "11111111-1111-1111-1111-111111111111"
MADHAPUR = "22222222-2222-2222-2222-222222222222"


def _test(tid, name, branch_id=None, price=50000):
    return {
        "id": tid,
        "name": name,
        "branch_id": branch_id,
        "price_paise": price,
        "is_active": True,
    }


def _paged_catalogue(rows):
    """A supabase select stub whose single short page ends the paging loop."""
    page = MagicMock()
    page.data = rows
    q = MagicMock()
    q.eq.return_value = q
    q.is_.return_value = q
    q.or_.return_value = q
    q.order.return_value = q
    q.range.return_value = q
    q.execute.return_value = page
    return q


def _admin_user(permissions=None, branch_id=None):
    from app.routers.admin import AdminUser

    user = AdminUser("staff-user")
    user.username = "labstaff"
    user.role = "staff"
    user.clinic_id = "clinic-1"
    user.user_id = "user-1"
    user.permissions = permissions or ["LAB_TESTS_MANAGE"]
    user.branch_id = branch_id
    return user


def _admin_app(user):
    from app.routers.admin import router, verify_credentials

    app = FastAPI()
    app.include_router(router)

    async def fake_user():
        return user

    app.dependency_overrides[verify_credentials] = fake_user
    return app


# -- The catalogue the patient sees -------------------------------------------


class TestPatientCatalogue:
    @pytest.mark.asyncio
    async def test_other_branches_tests_are_hidden(self):
        from app.database import get_lab_tests, supabase

        rows = [
            _test("t1", "CBC"),
            _test("t2", "MRI Brain", branch_id=KUKATPALLY),
            _test("t3", "Karyotyping", branch_id=MADHAPUR),
        ]
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = _paged_catalogue(rows)
            got = await get_lab_tests("clinic-1", branch_id=KUKATPALLY)

        assert [t["name"] for t in got] == ["CBC", "MRI Brain"]

    @pytest.mark.asyncio
    async def test_branch_row_overrides_all_branches_row_of_same_name(self):
        """The same name listed twice at two prices is what this prevents."""
        from app.database import get_lab_tests, supabase

        rows = [
            _test("shared", "Lipid Profile", price=60000),
            _test("kpl", "Lipid Profile", branch_id=KUKATPALLY, price=45000),
        ]
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = _paged_catalogue(rows)
            got = await get_lab_tests("clinic-1", branch_id=KUKATPALLY)

        assert len(got) == 1
        assert got[0]["id"] == "kpl"
        assert got[0]["price_paise"] == 45000

    @pytest.mark.asyncio
    async def test_override_match_ignores_case_and_padding(self):
        from app.database import get_lab_tests, supabase

        rows = [
            _test("shared", " Lipid Profile "),
            _test("kpl", "LIPID PROFILE", branch_id=KUKATPALLY),
        ]
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = _paged_catalogue(rows)
            got = await get_lab_tests("clinic-1", branch_id=KUKATPALLY)

        assert [t["id"] for t in got] == ["kpl"]

    @pytest.mark.asyncio
    async def test_other_branch_name_does_not_shadow_anything(self):
        """A sibling centre's override must not remove the shared row here."""
        from app.database import get_lab_tests, supabase

        rows = [
            _test("shared", "Lipid Profile"),
            _test("mdp", "Lipid Profile", branch_id=MADHAPUR),
        ]
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = _paged_catalogue(rows)
            got = await get_lab_tests("clinic-1", branch_id=KUKATPALLY)

        assert [t["id"] for t in got] == ["shared"]

    @pytest.mark.asyncio
    async def test_single_location_clinic_is_untouched(self):
        """No branch in context means no filtering -- the pre-existing path."""
        from app.database import get_lab_tests, supabase

        rows = [_test("t1", "CBC"), _test("t2", "Lipid Profile")]
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = _paged_catalogue(rows)
            got = await get_lab_tests("clinic-1")

        assert got == rows


# -- The booking handler's re-check -------------------------------------------


class TestSelectedTestBranchGuard:
    @pytest.mark.asyncio
    async def test_rejects_a_test_belonging_to_another_branch(self):
        from app.database import get_lab_test_by_id, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(
            data=[_test("t2", "MRI Brain", branch_id=KUKATPALLY)]
        )
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_test_by_id("clinic-1", "t2", branch_id=MADHAPUR)

        assert got is None

    @pytest.mark.asyncio
    async def test_allows_an_all_branches_test_anywhere(self):
        from app.database import get_lab_test_by_id, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=[_test("t1", "CBC")])
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_test_by_id("clinic-1", "t1", branch_id=MADHAPUR)

        assert got is not None and got["id"] == "t1"

    @pytest.mark.asyncio
    async def test_allows_the_branch_own_test(self):
        from app.database import get_lab_test_by_id, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(
            data=[_test("t2", "MRI Brain", branch_id=KUKATPALLY)]
        )
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_test_by_id("clinic-1", "t2", branch_id=KUKATPALLY)

        assert got is not None and got["id"] == "t2"

    @pytest.mark.asyncio
    async def test_single_location_clinic_is_not_blocked(self):
        """No branch in context must never reject a branch-tagged test."""
        from app.database import get_lab_test_by_id, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(
            data=[_test("t2", "MRI Brain", branch_id=KUKATPALLY)]
        )
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_test_by_id("clinic-1", "t2")

        assert got is not None

    @pytest.mark.asyncio
    async def test_conversation_passes_the_patient_branch(self):
        """Without this wiring the guard above is never reached in production."""
        from app.services.conversation import conversation_manager as cm

        clinic = {"id": "clinic-1", "name": "Vijaya Diagnostics"}
        context = {"branch_id": MADHAPUR}

        with patch(
            "app.database.get_lab_test_by_id", new_callable=AsyncMock, return_value=None
        ) as by_id, patch.object(
            cm, "_show_lab_test_list", new_callable=AsyncMock
        ), patch.object(cm, "whatsapp") as wa:
            wa.send_text = AsyncMock()
            await cm._handle_browsing_lab_tests(
                clinic,
                "+919876543210",
                "",
                "unknown",
                context,
                "en",
                interactive_data={"id": "labtest_t2"},
            )

        by_id.assert_awaited_once()
        assert by_id.await_args is not None
        assert by_id.await_args.kwargs["branch_id"] == MADHAPUR


# -- The admin catalogue view -------------------------------------------------


class TestAdminCatalogueView:
    def test_branch_view_includes_all_branches_rows(self):
        """The admin must see what the bot offers there, not only tagged rows."""
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            return_value={"id": KUKATPALLY, "clinic_id": "clinic-1"},
        ):
            q = _paged_catalogue([_test("t1", "CBC"), _test("t2", "MRI", KUKATPALLY)])
            sb.table.return_value.select.return_value = q
            resp = TestClient(app).get("/admin/lab-tests?branch_id=" + KUKATPALLY)

        assert resp.status_code == 200
        assert len(resp.json()) == 2
        or_filter = q.or_.call_args[0][0]
        assert "branch_id.eq." + KUKATPALLY in or_filter
        assert "branch_id.is.null" in or_filter

    def test_unscoped_view_applies_no_branch_filter(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            q = _paged_catalogue([_test("t1", "CBC")])
            sb.table.return_value.select.return_value = q
            resp = TestClient(app).get("/admin/lab-tests")

        assert resp.status_code == 200
        q.or_.assert_not_called()

    def test_branch_of_another_tenant_is_refused(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Branch not found"),
        ):
            sb.table.return_value.select.return_value = _paged_catalogue([])
            resp = TestClient(app).get("/admin/lab-tests?branch_id=" + MADHAPUR)

        assert resp.status_code == 404

    def test_catalogue_is_paged_past_the_1000_row_cap(self):
        """A 1,392-test catalogue used to stop at 1,000 with nothing logged."""
        from app.routers import admin as admin_module

        first = [_test("t%d" % i, "Test %d" % i) for i in range(1000)]
        second = [_test("t%d" % i, "Test %d" % i) for i in range(1000, 1392)]

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            q = MagicMock()
            q.eq.return_value = q
            q.order.return_value = q
            q.range.return_value = q
            q.execute.side_effect = [MagicMock(data=first), MagicMock(data=second)]
            sb.table.return_value.select.return_value = q
            resp = TestClient(app).get("/admin/lab-tests")

        assert resp.status_code == 200
        assert len(resp.json()) == 1392


# -- Per-branch sample collection hours ---------------------------------------
#
# A chain's centres keep different hours: the main lab runs 07:00-21:00, the
# satellite 08:00-14:00. The booking flow has always read the CHOSEN branch's
# hours, and the `days` in that window decide which dates the patient is even
# offered -- but the panel could only write the clinic-wide record, so the
# branch rows were unreachable from the product.


def _clinic_config_query(config):
    q = MagicMock()
    q.eq.return_value = q
    q.execute.return_value = MagicMock(data=[{"config": config}])
    return q


class TestCollectionWindowRead:
    def test_branch_hours_win_over_the_shared_hours(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        branch_hours = {"start": "08:00", "end": "14:00", "days": "Mon,Tue,Wed"}
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            return_value={"id": KUKATPALLY, "config": {"lab_collection": branch_hours}},
        ):
            sb.table.return_value.select.return_value = _clinic_config_query({})
            resp = TestClient(app).get(
                "/admin/lab-collection-window?branch_id=" + KUKATPALLY
            )

        assert resp.status_code == 200
        assert resp.json()["lab_collection"] == branch_hours
        assert resp.json()["source"] == "branch"

    def test_branch_without_hours_shows_the_inherited_clinic_hours(self):
        """The panel must report inheritance, not show placeholders as fact."""
        from app.routers import admin as admin_module

        clinic_hours = {"start": "07:00", "end": "21:00", "days": "Mon,Tue,Wed,Thu,Fri"}
        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            return_value={"id": KUKATPALLY, "config": {}},
        ):
            sb.table.return_value.select.return_value = _clinic_config_query(
                {"lab_collection": clinic_hours}
            )
            resp = TestClient(app).get(
                "/admin/lab-collection-window?branch_id=" + KUKATPALLY
            )

        assert resp.status_code == 200
        assert resp.json()["lab_collection"] == clinic_hours
        assert resp.json()["source"] == "clinic"

    def test_unconfigured_clinic_reports_the_shared_default(self):
        from app.database import DEFAULT_LAB_COLLECTION_WINDOW
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            sb.table.return_value.select.return_value = _clinic_config_query({})
            resp = TestClient(app).get("/admin/lab-collection-window")

        assert resp.status_code == 200
        assert resp.json()["lab_collection"] == DEFAULT_LAB_COLLECTION_WINDOW
        assert resp.json()["source"] == "default"

    def test_another_tenants_branch_is_refused(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Branch not found"),
        ):
            sb.table.return_value.select.return_value = _clinic_config_query({})
            resp = TestClient(app).get(
                "/admin/lab-collection-window?branch_id=" + MADHAPUR
            )

        assert resp.status_code == 404


class TestCollectionWindowWrite:
    def test_saving_with_a_branch_writes_that_branch_config(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            sb.table.return_value.select.return_value = _clinic_config_query(
                {"other": "kept"}
            )
            sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": KUKATPALLY}]
            )
            resp = TestClient(app).put(
                "/admin/lab-collection-window?branch_id=" + KUKATPALLY,
                json={"start": "08:00", "end": "14:00", "days": "Mon,Tue"},
            )

        assert resp.status_code == 200
        written = sb.table.return_value.update.call_args[0][0]["config"]
        assert written["lab_collection"] == {
            "start": "08:00",
            "end": "14:00",
            "days": "Mon,Tue",
        }
        # Unrelated branch settings must survive an hours-only save.
        assert written["other"] == "kept"
        assert sb.table.call_args_list[-1][0][0] == "branches"


class TestCollectionWindowResolution:
    @pytest.mark.asyncio
    async def test_branch_config_is_read_scoped_to_the_clinic(self):
        """A primary-key read alone would resolve another tenant's config."""
        from app.database import get_lab_collection_window, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(
            data=[
                {
                    "config": {
                        "lab_collection": {
                            "start": "08:00",
                            "end": "14:00",
                            "days": "Mon",
                        }
                    }
                }
            ]
        )
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_collection_window(
                {"id": "clinic-1", "config": {}}, branch_id=KUKATPALLY
            )

        assert got["start"] == "08:00"
        eq_args = [c[0] for c in q.eq.call_args_list]
        assert ("id", KUKATPALLY) in eq_args
        assert ("clinic_id", "clinic-1") in eq_args

    @pytest.mark.asyncio
    async def test_branch_without_hours_inherits_the_clinic(self):
        from app.database import get_lab_collection_window, supabase

        q = MagicMock()
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=[{"config": {}}])
        clinic_hours = {"start": "06:00", "end": "10:00", "days": "Sat"}
        with patch.object(supabase, "table") as table:
            table.return_value.select.return_value = q
            got = await get_lab_collection_window(
                {"id": "clinic-1", "config": {"lab_collection": clinic_hours}},
                branch_id=KUKATPALLY,
            )

        assert got == clinic_hours


class TestCollectionWindowClear:
    """Setting branch hours was a one-way door until this endpoint existed."""

    def test_clearing_a_branch_removes_only_its_hours(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            return_value={
                "id": KUKATPALLY,
                "config": {
                    "lab_collection": {"start": "08:00", "end": "14:00", "days": "Mon"},
                    "other": "kept",
                },
            },
        ):
            sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": KUKATPALLY}]
            )
            resp = TestClient(app).delete(
                "/admin/lab-collection-window?branch_id=" + KUKATPALLY
            )

        assert resp.status_code == 200
        assert resp.json() == {"success": True, "cleared": True}
        written = sb.table.return_value.update.call_args[0][0]["config"]
        assert "lab_collection" not in written
        # Unrelated branch settings must survive.
        assert written["other"] == "kept"

    def test_clearing_a_branch_that_already_inherits_writes_nothing(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            return_value={"id": KUKATPALLY, "config": {"other": "kept"}},
        ):
            resp = TestClient(app).delete(
                "/admin/lab-collection-window?branch_id=" + KUKATPALLY
            )

        assert resp.status_code == 200
        assert resp.json() == {"success": True, "cleared": False}
        sb.table.return_value.update.assert_not_called()

    def test_the_shared_hours_cannot_be_cleared(self):
        """Nothing to inherit from, so this would silently swap in the
        built-in default rather than restore anything."""
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ):
            resp = TestClient(app).delete("/admin/lab-collection-window")

        assert resp.status_code == 400
        assert "Choose a branch" in resp.json()["detail"]
        sb.table.return_value.update.assert_not_called()

    def test_another_tenants_branch_cannot_be_cleared(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Branch not found"),
        ):
            resp = TestClient(app).delete(
                "/admin/lab-collection-window?branch_id=" + MADHAPUR
            )

        assert resp.status_code == 404
        sb.table.return_value.update.assert_not_called()


class TestDuplicateNameHandling:
    """Migration 076 makes the database refuse duplicates; the API has to
    explain that rather than return an opaque 500 about a doctor."""

    def test_duplicate_create_is_a_409_naming_the_catalogue(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module,
            "resolve_clinic_id_for_write",
            new_callable=AsyncMock,
            return_value="clinic-1",
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            sb.table.return_value.insert.return_value.execute.side_effect = Exception(
                "duplicate key value violates unique constraint "
                '"idx_unique_lab_test_name_per_branch"'
            )
            resp = TestClient(app).post(
                "/admin/lab-tests", json={"name": "CBC", "price_rupees": 500}
            )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "test with this name already exists" in detail
        assert "doctor" not in detail.lower()

    def test_a_non_duplicate_failure_is_still_a_500(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module,
            "resolve_clinic_id_for_write",
            new_callable=AsyncMock,
            return_value="clinic-1",
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            sb.table.return_value.insert.return_value.execute.side_effect = Exception(
                "connection reset by peer"
            )
            resp = TestClient(app).post(
                "/admin/lab-tests", json={"name": "CBC", "price_rupees": 500}
            )

        assert resp.status_code == 500

    def test_names_are_stored_stripped(self):
        """The unique index keys on lower(btrim(name)); a padded row would be
        unreachable by the importer and by the branch-override rule."""
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module,
            "resolve_clinic_id_for_write",
            new_callable=AsyncMock,
            return_value="clinic-1",
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "new-id", "name": "CBC"}]
            )
            resp = TestClient(app).post(
                "/admin/lab-tests", json={"name": "   CBC   ", "price_rupees": 500}
            )

        assert resp.status_code == 200
        assert sb.table.return_value.insert.call_args[0][0]["name"] == "CBC"

    def test_a_whitespace_only_name_is_rejected(self):
        app = _admin_app(_admin_user())
        resp = TestClient(app).post(
            "/admin/lab-tests", json={"name": "   ", "price_rupees": 500}
        )
        assert resp.status_code == 422


class TestBookingUsesBranchHours:
    @pytest.mark.asyncio
    async def test_offered_dates_and_quoted_hours_follow_the_branch(self):
        """The days in the branch window gate which dates exist at all."""
        from datetime import datetime as _dt

        from app.services.conversation import conversation_manager as cm

        clinic = {"id": "clinic-1", "name": "Vijaya Diagnostics"}
        context = {"branch_id": KUKATPALLY}
        branch_hours = {"start": "08:00", "end": "14:00", "days": "Mon"}

        with patch(
            "app.database.get_lab_test_by_id",
            new_callable=AsyncMock,
            return_value=_test("t9", "CBC", branch_id=KUKATPALLY),
        ), patch(
            "app.database.get_lab_collection_window",
            new_callable=AsyncMock,
            return_value=branch_hours,
        ) as window, patch.object(cm, "whatsapp") as wa:
            wa.send_text = AsyncMock()
            wa.send_interactive_buttons = AsyncMock()
            await cm._handle_browsing_lab_tests(
                clinic,
                "+919876543210",
                "",
                "unknown",
                context,
                "en",
                interactive_data={"id": "labtest_t9"},
            )

        assert window.await_args is not None
        assert window.await_args.kwargs["branch_id"] == KUKATPALLY
        sent = wa.send_interactive_buttons.await_args.kwargs
        assert "08:00 - 14:00" in sent["body"]
        dates = [b["id"].removeprefix("labdate_") for b in sent["buttons"]]
        assert dates, "no collection dates offered"
        assert all(_dt.strptime(d, "%Y-%m-%d").weekday() == 0 for d in dates), dates


# -- CSV import scope ---------------------------------------------------------


CSV = "name,price_rupees\nCBC,500\nLipid Profile,600\n"


def _import_csv(user, csv_text, branch_id=None, existing=None):
    """Run the import endpoint; returns (response, inserts, updates, lookup)."""
    from app.routers import admin as admin_module

    app = _admin_app(user)
    lookup = _paged_catalogue(existing or [])

    with patch.object(admin_module, "supabase") as sb, patch.object(
        admin_module,
        "resolve_clinic_id_for_write",
        new_callable=AsyncMock,
        return_value="clinic-1",
    ), patch.object(
        admin_module, "log_admin_action", new_callable=AsyncMock
    ), patch.object(
        admin_module,
        "resolve_owned_branch",
        new_callable=AsyncMock,
        return_value={"id": branch_id, "clinic_id": "clinic-1"},
    ):
        sb.table.return_value.select.return_value = lookup
        sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "new-id"}]
        )
        sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "updated-id"}]
        )
        data = {"branch_id": branch_id} if branch_id else {}
        resp = TestClient(app).post(
            "/admin/lab-tests/import-csv",
            files={"file": ("tests.csv", io.BytesIO(csv_text.encode()), "text/csv")},
            data=data,
        )
        inserts = [c[0][0] for c in sb.table.return_value.insert.call_args_list]
        updates = [c[0][0] for c in sb.table.return_value.update.call_args_list]
    return resp, inserts, updates, lookup


class TestEditReassignment:
    def test_reassigning_to_another_tenant_branch_is_refused(self):
        """An edit could previously park a test on a foreign branch UUID."""
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )
            resp = TestClient(app).put(
                "/admin/lab-tests/t1",
                json={"branch_id": MADHAPUR, "price_rupees": 500},
            )

        assert resp.status_code == 400
        assert "does not belong to your clinic" in resp.json()["detail"]
        sb.table.return_value.update.assert_not_called()

    def test_reassigning_within_the_clinic_is_allowed(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module, "enforce_clinic_access", return_value="clinic-1"
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"id": KUKATPALLY}]
            )
            sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[_test("t1", "CBC", branch_id=KUKATPALLY)]
            )
            resp = TestClient(app).put(
                "/admin/lab-tests/t1", json={"branch_id": KUKATPALLY}
            )

        assert resp.status_code == 200
        assert sb.table.return_value.update.call_args[0][0]["branch_id"] == KUKATPALLY


class TestCsvImportScope:
    def test_import_without_branch_stays_all_branches(self):
        """The live single-branch clients must see no change at all."""
        resp, inserts, _, lookup = _import_csv(_admin_user(), CSV)

        assert resp.status_code == 200
        assert resp.json()["scope"] == "all_branches"
        assert resp.json()["branch_id"] is None
        assert [r["branch_id"] for r in inserts] == [None, None]
        lookup.is_.assert_called_once_with("branch_id", "null")

    def test_import_with_branch_tags_every_row(self):
        resp, inserts, _, lookup = _import_csv(_admin_user(), CSV, branch_id=KUKATPALLY)

        assert resp.status_code == 200
        assert resp.json()["scope"] == "branch"
        assert resp.json()["branch_id"] == KUKATPALLY
        assert [r["branch_id"] for r in inserts] == [KUKATPALLY, KUKATPALLY]
        lookup.is_.assert_not_called()

    def test_same_name_at_another_branch_is_created_not_overwritten(self):
        """Keying the upsert on name alone rewrote a sibling branch's row."""
        resp, inserts, updates, _ = _import_csv(
            _admin_user(),
            "name,price_rupees\nCBC,500\n",
            branch_id=MADHAPUR,
            # The lookup is branch-scoped, so Kukatpally's CBC never appears.
            existing=[],
        )

        assert resp.status_code == 200
        assert resp.json()["created"] == 1
        assert resp.json()["updated"] == 0
        assert inserts[0]["branch_id"] == MADHAPUR
        assert updates == []

    def test_reimport_updates_rows_in_the_same_scope(self):
        resp, inserts, updates, _ = _import_csv(
            _admin_user(),
            "name,price_rupees\nCBC,700\n",
            branch_id=MADHAPUR,
            existing=[{"id": "mdp-cbc", "name": "CBC"}],
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "created": 0,
            "updated": 1,
            "total_imported": 1,
            "branch_id": MADHAPUR,
            "scope": "branch",
            "errors": [],
        }
        assert updates[0]["price_paise"] == 70000
        assert inserts == []
        # An import must never MOVE a test between branches: the lookup only
        # ever matches rows already in the import's scope, so the branch it
        # writes back is the branch it matched.
        assert updates[0]["branch_id"] == MADHAPUR

    def test_staff_pinned_elsewhere_cannot_import_to_another_branch(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user(branch_id=MADHAPUR))
        with patch.object(admin_module, "supabase"), patch.object(
            admin_module,
            "resolve_clinic_id_for_write",
            new_callable=AsyncMock,
            return_value="clinic-1",
        ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock):
            resp = TestClient(app).post(
                "/admin/lab-tests/import-csv",
                files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
                data={"branch_id": KUKATPALLY},
            )

        assert resp.status_code == 403

    def test_bad_branch_rejects_before_any_row_is_written(self):
        from app.routers import admin as admin_module

        app = _admin_app(_admin_user())
        with patch.object(admin_module, "supabase") as sb, patch.object(
            admin_module,
            "resolve_clinic_id_for_write",
            new_callable=AsyncMock,
            return_value="clinic-1",
        ), patch.object(
            admin_module, "log_admin_action", new_callable=AsyncMock
        ), patch.object(
            admin_module,
            "resolve_owned_branch",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Branch not found"),
        ):
            resp = TestClient(app).post(
                "/admin/lab-tests/import-csv",
                files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
                data={"branch_id": MADHAPUR},
            )

        assert resp.status_code == 404
        sb.table.return_value.insert.assert_not_called()
