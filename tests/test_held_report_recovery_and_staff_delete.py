"""Regressions for the 2026-09-02 stuck-review-queue and staff-lifecycle fixes.

Production symptom: 52 Accumx lab reports sat in the needs-review triage queue
for three days and could not be cleared by any means.

Root cause was a pair of defects that only bite together:

  1. app/routers/integrations.py — the cross-path idempotency guard treated ANY
     existing lab_reports row for an external_report_id as "already delivered",
     including a needs_review row. A held report had nothing sent and no PDF
     stored (file_path is the "pending_review/<id>" sentinel), so the connector
     re-offered it on every poll and this guard silently swallowed it. Forever.

  2. app/services/patient_match.py — a transient lookup failure (a DNS blip,
     "[Errno 11001] getaddrinfo failed", a dropped PostgREST connection) failed
     closed straight into that permanent hold. Nine reports were parked by
     infrastructure noise, not by any identity conflict.

Plus two lifecycle gaps: staff accounts could only be deactivated (the row
lived on in the panel forever), and the delegated-permission picker offered a
diagnostics tenant eight doctor permissions while omitting all three that
describe the work its staff actually does.
"""

import inspect
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ADMIN_HTML = pathlib.Path("admin/index.html")


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


# ── 1. A held report is not "already delivered" ──────────────────────────────

def test_idempotency_guard_reprocesses_a_held_report():
    """needs_review means the gate held it: nothing sent, no PDF stored."""
    import app.routers.integrations as integrations

    src = inspect.getsource(integrations.receive_lab_report)
    assert 'HELD_NEVER_DELIVERED = {"needs_review"}' in src
    assert "existing_lr.data = []" in src, (
        "A held row must not short-circuit intake as already_processed"
    )


def test_a_delivered_report_still_blocks_reprocessing():
    """The guard must keep doing its real job — no double-sends."""
    import app.routers.integrations as integrations

    src = inspect.getsource(integrations.receive_lab_report)
    m = re.search(r"HELD_NEVER_DELIVERED = \{([^}]*)\}", src)
    assert m, "exempt set not found"
    exempt = {v.strip().strip("\"'") for v in m.group(1).split(",") if v.strip()}
    assert exempt == {"needs_review"}, (
        f"Only a never-delivered hold may be reprocessed; got {exempt}"
    )


def test_claim_takes_over_a_held_row_under_cas():
    """The unique index would otherwise make every re-offer collide."""
    from app.services.lab_reports import LabReportService

    src = inspect.getsource(LabReportService.upload_and_send)
    assert '.eq("status", "needs_review")' in src, (
        "Takeover must be a compare-and-set so exactly one worker wins"
    )
    assert '"status": "processing"' in src


# ── 2. A transient lookup failure must not become a permanent hold ───────────

@pytest.mark.asyncio
async def test_patient_lookup_retries_a_transient_failure():
    """A DNS blip stranded real reports. The lookup is an idempotent read."""
    from app.services.patient_match import patient_match_service

    calls = {"n": 0}

    async def flaky(_query):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("[Errno 11001] getaddrinfo failed")
        return MagicMock(data=[])

    with patch("app.services.patient_match.sb", flaky), patch(
        "app.services.patient_match.supabase", MagicMock()
    ), patch.object(
        type(patient_match_service), "_hold_unverified", AsyncMock(return_value=False)
    ), patch("asyncio.sleep", AsyncMock()):
        result = await patient_match_service.match(
            clinic_id="c-1", scraped_name="Mr X", scraped_phone="+919440545808"
        )

    assert calls["n"] == 3, "must retry, not fail closed on the first blip"
    assert result.is_safe_to_send is True
    assert result.match_source != "database_error"


@pytest.mark.asyncio
async def test_patient_lookup_still_fails_closed_when_the_db_is_really_down():
    """Retrying must not turn a genuine outage into a silent delivery."""
    from app.services.patient_match import patient_match_service

    async def always_down(_query):
        raise OSError("Server disconnected")

    with patch("app.services.patient_match.sb", always_down), patch(
        "app.services.patient_match.supabase", MagicMock()
    ), patch("asyncio.sleep", AsyncMock()):
        result = await patient_match_service.match(
            clinic_id="c-1", scraped_name="Mr X", scraped_phone="+919440545808"
        )

    assert result.is_safe_to_send is False
    assert result.match_source == "database_error"


# ── 3. Staff accounts are really deletable ───────────────────────────────────

def test_delete_staff_endpoint_revokes_sessions_before_removing_the_row():
    from app.routers.admin import delete_staff

    src = inspect.getsource(delete_staff)
    assert "revoke_sessions_for_user" in src, (
        "A deleted account holding a live cookie would keep working"
    )
    assert src.index("revoke_sessions_for_user") < src.index(".delete()")
    assert '.eq("role", "staff")' in src, "Only staff rows may be deleted here"


@pytest.mark.asyncio
async def test_delete_staff_refuses_a_non_staff_row():
    from fastapi import HTTPException

    from app.routers.admin import AdminUser, delete_staff

    admin = AdminUser("boss", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    res = MagicMock(data=[{"id": "s-1", "role": "clinic_admin", "clinic_id": "c-1"}])

    with patch("app.routers.admin.sb", AsyncMock(return_value=res)):
        with pytest.raises(HTTPException) as exc:
            await delete_staff(staff_id="s-1", request=None, user=admin)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_staff_refuses_another_tenants_account():
    from fastapi import HTTPException

    from app.routers.admin import AdminUser, delete_staff

    admin = AdminUser("boss", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    res = MagicMock(data=[{"id": "s-9", "role": "staff", "clinic_id": "c-OTHER",
                           "username": "victim", "branch_id": None}])

    with patch("app.routers.admin.sb", AsyncMock(return_value=res)):
        with pytest.raises(HTTPException) as exc:
            await delete_staff(staff_id="s-9", request=None, user=admin)
    assert exc.value.status_code == 403


def test_panel_offers_delete_not_just_deactivate():
    html = _html()
    assert "async function deleteStaff(" in html
    assert "apiDel('/admin/staff/'" in html
    assert 'onclick="deleteStaff(' in html


# ── 4. Delegated permissions match what the plan can actually do ─────────────

def test_every_registered_permission_is_offered():
    from app.services.permissions import PERMISSIONS

    html = _html()
    for cls in ("staff-perm-cb", "edit-staff-perm-cb"):
        ui = set(re.findall(r'class="' + cls + r'" value="([A-Z_]+)"', html))
        missing = PERMISSIONS - ui
        assert not missing, f"{cls} omits {sorted(missing)}"


def test_diagnostic_permissions_are_present_and_gated():
    """A lab delegates report work, not doctor rosters."""
    html = _html()
    for perm in ("REPORTS_VIEW", "REPORTS_RESOLVE", "CONNECTOR_MANAGE"):
        assert f'value="{perm}"' in html, f"{perm} missing from the picker"
    assert 'data-perm-feature="diagnostic_reports"' in html


@pytest.mark.parametrize(
    "perm", ["DOCTORS_CREATE", "DOCTORS_UPDATE", "DOCTORS_DELETE", "DOCTOR_BRANCH_ASSIGN"]
)
def test_doctor_permissions_are_hidden_without_the_booking_feature(perm):
    m = re.search(
        r'<label([^>]*)><input type="checkbox" class="staff-perm-cb" value="' + perm + r'"',
        _html(),
    )
    assert m and 'data-perm-feature="booking"' in m.group(1), (
        f"{perm} must be gated on the booking feature"
    )


def test_hidden_permissions_are_unticked_not_just_invisible():
    """A hidden but ticked box would submit a grant the plan cannot exercise."""
    m = re.search(r"function applyPermissionVisibility\(\) \{(.*?)\n\}", _html(), re.S)
    assert m, "applyPermissionVisibility not found"
    assert "cb.checked = false" in m.group(1)


def test_role_presets_cannot_retick_a_hidden_permission():
    assert _html().count("    applyPermissionVisibility();\n}") >= 2, (
        "Both role-change handlers must re-apply plan gating"
    )


# ── 5. Cross-tab identity mismatch surfaces instead of 403-looping ───────────

def test_tenant_boundary_403_checks_who_we_actually_are():
    m = re.search(r"async function apiFail\(r\) \{(.*?)\n\}", _html(), re.S)
    assert m, "apiFail not found"
    body = m.group(1)
    assert "Access to clinic" in body
    assert "who.username !== myUsername" in body
    assert "forceRelogin(" in body
