"""Bulk delete for the lab test catalogue (POST /admin/lab-tests/bulk-delete).

A diagnostics centre carries ~1,400 tests; deleting them one trash-can click
at a time is not a workflow. These tests drive the real route against an
in-memory lab_tests table, so the clinic filter, the branch rule and the
chunking are exercised, not just asserted on a mock's call list.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("APP_ENV", "testing")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from fastapi import FastAPI
from fastapi.testclient import TestClient

CLINIC = "clinic-1"
OTHER = "clinic-2"
KUKATPALLY = "11111111-1111-1111-1111-111111111111"
MADHAPUR = "22222222-2222-2222-2222-222222222222"


def _uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Just enough PostgREST: select/delete with eq and in_ filters."""

    def __init__(self, store, calls):
        self.store, self.calls = store, calls
        self.op, self.filters = None, []

    def select(self, *_):
        self.op = "select"
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self.calls.append((self.op, len(vals)))
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def execute(self):
        hit = [r for r in self.store if all(f(r) for f in self.filters)]
        if self.op == "delete":
            for r in hit:
                self.store.remove(r)
        return _Result([dict(r) for r in hit])


class _Supabase:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def table(self, name):
        assert name == "lab_tests"
        return _Query(self.rows, self.calls)


def _user(role="clinic_admin", branch_id=None, permissions=None):
    from app.routers.admin import AdminUser

    u = AdminUser("u")
    u.username, u.role, u.clinic_id, u.user_id = "labadmin", role, CLINIC, "user-1"
    u.permissions = permissions if permissions is not None else ["LAB_TESTS_MANAGE"]
    u.branch_id = branch_id
    return u


def _post(user, ids, rows):
    from app.routers import admin as admin_module
    from app.routers.admin import router, verify_credentials

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_credentials] = lambda: user
    fake = _Supabase(rows)
    with patch.object(admin_module, "supabase", fake), patch.object(
        admin_module, "enforce_clinic_access", return_value=CLINIC
    ), patch.object(admin_module, "log_admin_action", new_callable=AsyncMock) as audit:
        resp = TestClient(app).post("/admin/lab-tests/bulk-delete", json={"ids": ids})
    return resp, fake, audit


def _row(n, clinic=CLINIC, branch_id=None):
    return {"id": _uid(n), "name": f"Test {n}", "clinic_id": clinic, "branch_id": branch_id}


def test_deletes_exactly_the_selected_tests():
    rows = [_row(i) for i in range(1, 6)]
    resp, fake, audit = _post(_user(), [_uid(1), _uid(3)], rows)

    assert resp.status_code == 200
    assert resp.json() == {"requested": 2, "deleted": 2, "not_found": 0}
    assert [r["id"] for r in fake.rows] == [_uid(2), _uid(4), _uid(5)]
    details = audit.await_args.kwargs["details"]
    assert details["deleted"] == 2 and details["names"] == ["Test 1", "Test 3"]


def test_another_clinics_test_is_never_touched():
    rows = [_row(1), _row(2, clinic=OTHER)]
    resp, fake, _ = _post(_user(), [_uid(1), _uid(2)], rows)

    assert resp.json() == {"requested": 2, "deleted": 1, "not_found": 1}
    assert [r["id"] for r in fake.rows] == [_uid(2)]


def test_a_whole_catalogue_is_deleted_in_url_safe_chunks():
    rows = [_row(i) for i in range(1, 451)]
    resp, fake, _ = _post(_user(), [_uid(i) for i in range(1, 451)], rows)

    assert resp.json()["deleted"] == 450
    assert fake.rows == []
    assert max(n for _, n in fake.calls) <= 200


def test_branch_pinned_staff_cannot_delete_another_branchs_test():
    """All or nothing: one foreign-branch test refuses the whole request."""
    rows = [_row(1, branch_id=KUKATPALLY), _row(2, branch_id=MADHAPUR)]
    resp, fake, audit = _post(_user("staff", branch_id=KUKATPALLY), [_uid(1), _uid(2)], rows)

    assert resp.status_code == 403
    assert len(fake.rows) == 2
    assert all(op == "select" for op, _ in fake.calls)
    audit.assert_not_awaited()


def test_branch_pinned_staff_can_delete_own_branch_tests():
    rows = [_row(1, branch_id=KUKATPALLY), _row(2)]
    resp, fake, _ = _post(_user("staff", branch_id=KUKATPALLY), [_uid(1), _uid(2)], rows)

    assert resp.json()["deleted"] == 2 and fake.rows == []


def test_without_lab_permission_nothing_is_deleted():
    resp, fake, _ = _post(_user("staff", permissions=["APPOINTMENTS_VIEW"]), [_uid(1)], [_row(1)])

    assert resp.status_code == 403
    assert len(fake.rows) == 1


def test_non_uuid_ids_are_refused_before_any_query():
    """An id lands inside in.(...): a comma or parenthesis would rewrite the filter."""
    for bad in (["1,2"], ["x)"], [_uid(1), "not-a-uuid"], []):
        resp, fake, _ = _post(_user(), bad, [_row(1)])
        assert resp.status_code == 422, bad
        assert fake.calls == [] and len(fake.rows) == 1


def test_duplicate_ids_count_once_and_request_size_is_capped():
    from app.routers.admin import LAB_BULK_DELETE_MAX

    resp, _, _ = _post(_user(), [_uid(1), _uid(1)], [_row(1)])
    assert resp.json() == {"requested": 1, "deleted": 1, "not_found": 0}

    resp, fake, _ = _post(_user(), [_uid(i) for i in range(LAB_BULK_DELETE_MAX + 1)], [_row(1)])
    assert resp.status_code == 422 and fake.calls == []


def test_nothing_found_writes_no_audit_entry():
    resp, _, audit = _post(_user(), [_uid(9)], [_row(1)])

    assert resp.json() == {"requested": 1, "deleted": 0, "not_found": 1}
    audit.assert_not_awaited()


def test_deleting_refreshes_the_whatsapp_service_type_menu():
    from app.services.conversation import ConversationManager

    ConversationManager._lab_heading_cache[CLINIC] = ("stale", 0)
    try:
        _post(_user(), [_uid(1)], [_row(1)])
        assert CLINIC not in ConversationManager._lab_heading_cache
    finally:
        ConversationManager._lab_heading_cache.pop(CLINIC, None)


def test_past_bookings_survive_a_deleted_test():
    """The delete leans on appointments.lab_test_id being ON DELETE SET NULL."""
    sql = Path(__file__).resolve().parents[1].joinpath(
        "migrations", "039_appointments_lab_test_booking.sql"
    ).read_text(encoding="utf-8")
    assert re.search(r"lab_test_id\s+UUID\s+REFERENCES\s+lab_tests\(id\)\s+ON DELETE SET NULL", sql)


def test_panel_wires_selection_to_the_route():
    html = Path(__file__).resolve().parents[1].joinpath("admin", "index.html").read_text(encoding="utf-8")
    assert "/admin/lab-tests/bulk-delete" in html
    assert 'id="labBulkBar"' in html and 'id="labSelectAll"' in html
    # Selection must be dropped when the clinic changes and pruned on reload.
    reset = html[html.index("function resetLabTestBranchState"):]
    assert "labSelected.clear()" in reset[:reset.index("\n}")]
    assert "labSelected.delete(id)" in html
    # Large deletes need the typed word; the panel never sends more than one request's cap.
    assert "Type DELETE to confirm" in html
    assert "i += 1000" in html
