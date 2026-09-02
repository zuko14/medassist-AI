"""Delegated branch management, the Talk-to-Staff phone, and splitting the
holiday calendar out of doctor-roster management.

Four production gaps this covers:
  1. Staff holding DOCTOR_BRANCH_ASSIGN could not reach the branch list, so the
     doctor-assignment UI they had been granted never rendered.
  2. There was no permission that let an admin delegate branch add/edit at all.
  3. "Talk to Staff" quoted the WhatsApp number, with nothing in the panel to
     change it.
  4. A diagnostics-only plan showed Doctor Leaves, which has no meaning there,
     while Holidays — which a lab does need — was gated behind the same flag.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    ClinicProfileUpdate,
    create_branch,
    delete_branch,
    get_branches,
    get_current_admin,
    update_branch,
)
from app.services.permissions import PERMISSIONS, ROLE_PRESETS, resolve_permissions
from app.services.tenant import ALL_FEATURES, FEATURE_LABELS, PLAN_FEATURES, has_feature

ADMIN_HTML = "admin/index.html"


def _html() -> str:
    with open(ADMIN_HTML, encoding="utf-8") as f:
        return f.read()


# ── 2. BRANCHES_MANAGE exists and is opt-in ──────────────────────────────────

def test_branches_manage_permission_is_registered():
    assert "BRANCHES_MANAGE" in PERMISSIONS
    assert "BRANCHES_MANAGE" in resolve_permissions("CUSTOM_ROLE", ["BRANCHES_MANAGE"])


def test_branches_manage_is_never_granted_implicitly_by_a_role():
    """It must appear only when an admin ticks it. A role preset would hand it
    to every existing account of that role the next time one is saved."""
    for role, grants in ROLE_PRESETS.items():
        assert "BRANCHES_MANAGE" not in grants, f"{role} grants it implicitly"


def test_branch_reads_open_up_and_writes_tighten():
    """The list was 403ing for staff, which hid the assignment UI. Writes go
    the other way: they used to be role-gated, now they need the grant.

    Read the declarations rather than the signatures — FastAPI erases the
    Depends() factory down to an opaque `_dep` closure.
    """
    assert "Depends(verify_credentials)" in inspect.getsource(get_branches)
    for fn in (create_branch, update_branch, delete_branch):
        src = inspect.getsource(fn)
        assert 'require_permission("BRANCHES_MANAGE")' in src, fn.__name__
        assert "Depends(require_admin)" not in src, fn.__name__


@pytest.mark.asyncio
async def test_branch_pinned_staff_cannot_create_a_new_branch():
    staff = AdminUser(
        "gayatri", role="staff", clinic_id="clinic-1", user_id="u-1",
        permissions=["BRANCHES_MANAGE"], branch_id="branch-1",
        staff_role="BRANCH_MANAGER",
    )
    with pytest.raises(HTTPException) as exc:
        await create_branch(
            branch=MagicMock(), clinic_id="clinic-1", request=None, user=staff
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_branch_pinned_staff_cannot_edit_a_sibling_branch():
    staff = AdminUser(
        "gayatri", role="staff", clinic_id="clinic-1", user_id="u-1",
        permissions=["BRANCHES_MANAGE"], branch_id="branch-MINE",
        staff_role="BRANCH_MANAGER",
    )
    mock_sb = MagicMock()
    with patch("app.routers.admin.supabase", mock_sb), patch("app.database.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await update_branch(
                branch_id="branch-OTHER", branch=MagicMock(),
                clinic_id="clinic-1", request=None, user=staff,
            )
    assert exc.value.status_code == 403
    assert not mock_sb.table.return_value.update.called


# ── 1. The doctor-assignment UI is reachable for staff who hold the grant ────

def test_branch_nav_opens_for_either_branch_permission():
    assert (
        "hasPermission('DOCTOR_BRANCH_ASSIGN') || hasPermission('BRANCHES_MANAGE')"
        in _html()
    ), "Branches nav must open for assign-only staff as well as branch managers"


def test_branch_add_edit_ui_is_permission_gated_not_role_gated():
    html = _html()
    assert "hasPermission('BRANCHES_MANAGE') ? '' : 'none'" in html, \
        "The add/edit branch form must follow the grant"
    assert "hasPermission('BRANCHES_MANAGE') ? `<button" in html, \
        "Row edit/delete buttons must follow the grant, not the role"


def test_both_staff_permission_pickers_offer_branch_management():
    assert _html().count('value="BRANCHES_MANAGE"') == 2, \
        "Create-staff and edit-staff dialogs must both offer the grant"


# ── 3. Talk-to-Staff phone is configurable ───────────────────────────────────

def test_escalation_prefers_the_configured_staff_phone():
    from app.services.conversation import ConversationManager

    src = inspect.getsource(ConversationManager._handle_human_escalation)
    assert "get_clinic_contact(" in src and '"staff_phone"' in src
    # The old chain stays as the fallback, so clinics that never set one are
    # unaffected.
    assert 'clinic.get("whatsapp_number")' in src


def test_profile_round_trips_the_staff_phone():
    import app.routers.admin as admin_module

    assert "hospital_staff_phone" in ClinicProfileUpdate.model_fields
    src = inspect.getsource(admin_module)
    assert 'cfg["staff_phone"] = staff_phone' in src
    assert 'cfg.pop("staff_phone", None)' in src, "Clearing must restore the fallback"

    html = _html()
    assert "f-profileStaffPhone" in html
    assert "hospital_staff_phone" in html


# ── 4. Holiday calendar is not a doctor-roster feature ───────────────────────

def test_diagnostics_gets_holidays_without_doctor_leaves():
    diag = PLAN_FEATURES["diagstream"]
    assert "holiday_calendar" in diag, "A lab closes on public holidays too"
    assert "roster_management" not in diag, "Doctor leave has no meaning without doctors"


@pytest.mark.parametrize("plan", ["soloclinic", "diagstream", "essential", "polyclinic"])
def test_every_plan_has_a_holiday_calendar(plan):
    assert "holiday_calendar" in PLAN_FEATURES[plan]


def test_enterprise_wildcard_still_covers_the_new_feature():
    assert has_feature({"plan": "enterprise"}, "holiday_calendar")


def test_new_feature_has_a_display_label():
    assert set(ALL_FEATURES) <= set(FEATURE_LABELS)
    assert FEATURE_LABELS["holiday_calendar"]


def test_nav_gates_leaves_and_holidays_on_different_features():
    html = _html()
    assert 'data-page="leaves" data-feature="roster_management"' in html
    assert 'data-page="holidays" data-feature="holiday_calendar"' in html


# ── 4b. A super-admin acting on a tenant sees that tenant's plan ─────────────

@pytest.mark.asyncio
async def test_me_resolves_the_selected_tenants_plan_for_a_super_admin():
    owner = AdminUser("owner", role="super_admin", clinic_id=None, user_id="super_admin_env")
    clinic = {"id": "c-diag", "plan": "diagstream", "features": {}}

    with patch("app.routers.admin.get_clinic_by_id", AsyncMock(return_value=clinic)):
        scoped = await get_current_admin(clinic_id="c-diag", user=owner)
        unscoped = await get_current_admin(user=owner)

    assert scoped["plan"] == "diagstream"
    assert "holiday_calendar" in scoped["features"]
    assert "roster_management" not in scoped["features"], \
        "Doctor Leaves must not show while acting on a diagnostics-only tenant"
    # With no tenant named, the owner keeps the see-everything default.
    assert unscoped["features"] is None


@pytest.mark.asyncio
async def test_me_ignores_clinic_id_for_a_clinic_admin():
    """A clinic_admin must never be able to read another tenant's plan."""
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="u-1")
    seen = {}

    async def _fake_get(cid):
        seen["cid"] = cid
        return {"id": cid, "plan": "polyclinic", "features": {}}

    with patch("app.routers.admin.get_clinic_by_id", _fake_get):
        out = await get_current_admin(clinic_id="some-other-clinic", user=admin)

    assert seen["cid"] == "clinic-1"
    assert out["plan"] == "polyclinic"


def test_panel_adopts_the_picked_tenants_plan():
    html = _html()
    assert "async function adoptClinicPlan()" in html
    assert html.count("await adoptClinicPlan();") == 2, \
        "Must run on the first pick and on every subsequent switch"


def test_a_granted_permission_cannot_reshow_a_tab_the_plan_excludes():
    """The role loop runs after the feature loop and assigns display outright.
    Without re-checking the plan, staff holding DOCTOR_LEAVES_CREATE would see
    Doctor Leaves on a diagnostics-only tenant — the exact tab being removed."""
    html = _html()
    assert "function planAllowsFeature(feature)" in html
    assert "if (!planAllowsFeature(el.dataset.feature)) {" in html


def test_switching_off_a_diagnostics_tenant_restores_the_clinic_dashboard():
    """myPlan now actually changes on switch, so the diagnostics panel needs an
    inverse or it stays on screen over a booking tenant's dashboard."""
    html = _html()
    assert "function applyDashboardForPlan()" in html
    assert html.count("applyDashboardForPlan();") == 2
    assert "if (myPlan === 'diagstream') loadDiagnosticDashboard();" not in html
