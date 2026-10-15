"""D-31 (KK-P4-08 hardening audit): lab-test edit/delete branch scope, price ceiling,
collection-window audit row.

- PUT / DELETE /admin/lab-tests/{id} filter on clinic + id only, so a branch-pinned
  LAB_TESTS_MANAGE user could edit or delete another branch's own test (bulk-delete
  already refuses that). Fix: the same own-branch + branch-less filter as D-21.
- price_paise is INTEGER: a price above Rs 21,474,836 overflowed in the DB -> 500.
- PUT /admin/lab-collection-window wrote no audit row.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import admin
from app.routers.admin import AdminUser, LabCollectionWindowUpdate, LabTestCreate, LabTestUpdate
from tests.config_writes import captured_config_writes  # D-13: saves go through config_store

CLINIC = "11111111-1111-1111-1111-111111111111"
B1 = "22222222-2222-2222-2222-222222222222"
PINNED = AdminUser("lab_b1", role="staff", clinic_id=CLINIC, permissions=["LAB_TESTS_MANAGE"], branch_id=B1)
ADMIN = AdminUser("owner", role="clinic_admin", clinic_id=CLINIC)


def _chain(data):
    q = MagicMock()
    for m in ("update", "delete", "select", "eq", "or_", "table"):
        getattr(q, m).return_value = q
    return q, AsyncMock(return_value=MagicMock(data=data))


def _run(coro_fn, user, data):
    q, sb = _chain(data)
    with patch.object(admin, "supabase", q), patch.object(admin, "sb", sb), \
         patch.object(admin, "log_admin_action", AsyncMock()):
        try:
            out = asyncio.run(coro_fn(user))
        except HTTPException as e:
            out = e
    return q, out


def _put(user):
    return admin.update_lab_test("t1", LabTestUpdate(price_rupees=500), request=None, clinic_id=CLINIC, user=user)


def _delete(user):
    return admin.delete_lab_test("t1", request=None, clinic_id=CLINIC, user=user)


@pytest.mark.parametrize("call", [_put, _delete])
def test_pinned_staff_write_is_limited_to_own_branch_and_shared_rows(call):
    q, out = _run(call, PINNED, [])  # another branch's row: the filter matches nothing
    q.or_.assert_called_once_with(f"branch_id.eq.{B1},branch_id.is.null")
    assert isinstance(out, HTTPException) and out.status_code == 404


@pytest.mark.parametrize("call", [_put, _delete])
def test_admin_write_is_not_branch_filtered(call):
    q, out = _run(call, ADMIN, [{"id": "t1"}])
    q.or_.assert_not_called()
    assert not isinstance(out, HTTPException)


@pytest.mark.parametrize("model", [LabTestCreate, LabTestUpdate])
def test_price_above_integer_paise_ceiling_is_422(model):
    with pytest.raises(ValidationError):
        model(name="CBC", price_rupees=21_474_837)
    assert model(name="CBC", price_rupees=21_474_836).price_rupees == 21_474_836


def test_collection_window_update_writes_an_audit_row():
    q, sb = _chain([{"config": {}}])
    audit = AsyncMock()
    with captured_config_writes(), patch.object(admin, "supabase", q), patch.object(admin, "sb", sb), \
         patch.object(admin, "log_admin_action", audit):
        asyncio.run(admin.update_lab_collection_window(
            LabCollectionWindowUpdate(start="07:00", end="11:00"), clinic_id=CLINIC, branch_id=None, user=ADMIN))
    kw = audit.await_args.kwargs
    assert kw["action"] == "update_lab_collection_window"
    assert kw["details"]["lab_collection"] == {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"}
    assert kw["details"]["branch_id"] is None


# ── D-32: import failure_reason never holds raw exception text ──────────────

def _import_failure(exc):
    q, sb = _chain([{}])
    with patch.object(admin, "supabase", q), patch.object(admin, "sb", sb), \
         patch("app.services.price_list_parser.parse_catalogue_file", AsyncMock(side_effect=exc)):
        asyncio.run(admin._process_catalogue_import_job(
            preview_id="p1", clinic_id=CLINIC, branch_id=None, default_category=None, raw_bytes=b"x", filename="a.csv"))
    return q.update.call_args[0][0]


def test_import_failure_hides_raw_exception_text():
    written = _import_failure(RuntimeError('connection to db.internal:5432 failed: password "s3cret"'))
    assert written["status"] == "failed"
    assert "s3cret" not in written["failure_reason"] and "5432" not in written["failure_reason"]
    assert written["failure_reason"] == admin.IMPORT_FAILED_TEXT


def test_import_failure_keeps_the_parsers_own_message():
    written = _import_failure(HTTPException(400, "Corrupted or invalid XLSX file."))
    assert written["failure_reason"] == "Corrupted or invalid XLSX file."


# ── D-33: cleanup-apply checks every row's branch before the first write ────

def test_cleanup_apply_refuses_another_branch_before_writing_anything():
    a, b = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    rows = [{"id": a, "name": "Mine", "branch_id": B1}, {"id": b, "name": "Theirs", "branch_id": "b2"}]
    q, sb = _chain(rows)
    q.in_.return_value = q
    body = admin.LabCleanupApplyRequest(prep_fixes=[
        {"test_id": a, "fasting_required": True, "preparation": "Fast"},
        {"test_id": b, "fasting_required": True, "preparation": "Fast"}])
    with patch.object(admin, "supabase", q), patch.object(admin, "sb", sb), \
         patch.object(admin, "log_admin_action", AsyncMock()):
        with pytest.raises(HTTPException) as e:
            asyncio.run(admin.apply_lab_tests_cleanup(body, request=None, clinic_id=CLINIC, user=PINNED))
    assert e.value.status_code == 403
    q.update.assert_not_called()
    q.delete.assert_not_called()
