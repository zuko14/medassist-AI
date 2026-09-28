"""Dental sitting note photos + per-patient history export (migration 094)."""

import csv
import io
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, build_dental_history_csv, verify_credentials
from app.services import dental_plans as dp

CLINIC = "11111111-1111-1111-1111-111111111111"
APPT = "22222222-2222-2222-2222-222222222222"
PLAN_ID = "33333333-3333-3333-3333-333333333333"
PHOTO = "55555555-5555-5555-5555-555555555555"
DENTAL = {"id": CLINIC, "name": "Smile Dental", "plan": "dental", "config": {}, "features": {}}
ADMIN = AdminUser(username="dental_admin", role="clinic_admin", clinic_id=CLINIC, user_id="u1")
STAFF = AdminUser(username="desk", role="staff", clinic_id=CLINIC, user_id="u2")

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 200
client = TestClient(app)


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


def _sitting(status="completed", **over):
    s = {"id": APPT, "clinic_id": CLINIC, "treatment_plan_id": PLAN_ID, "status": status, "branch_id": None}
    s.update(over)
    return s


# ═══════ type sniffing ═══════


@pytest.mark.parametrize("data, expected", [
    (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"),
    (b"%PDF-1.7 ...", None), (b"GIF89a....", None), (b"<svg onload=alert(1)>", None),
    (b"<html>", None), (b"", None), (b"RIFF\x00\x00\x00\x00WAVE", None),
])
def test_sniff_uses_the_bytes_not_the_claim(data, expected):
    assert dp.sniff_image_type(data) == expected


# ═══════ add_sitting_photo ═══════


def _storage():
    bucket = MagicMock()
    storage = MagicMock()
    storage.from_.return_value = bucket
    return storage, bucket


async def _add(data, appt=None, existing=0):
    storage, bucket = _storage()
    sb_mock = AsyncMock(side_effect=[
        MagicMock(count=existing),                                            # photo count
        MagicMock(data=[{"id": PHOTO, "size_bytes": len(data)}]),             # insert
    ])
    fake = MagicMock(storage=storage)
    with patch("app.services.dental_plans.supabase", fake), patch("app.services.dental_plans.sb", sb_mock):
        out = await dp.add_sitting_photo(CLINIC, appt or _sitting(), data, "desk")
    return out, bucket, fake


@pytest.mark.asyncio
async def test_photo_is_stored_under_an_id_only_path_with_the_sniffed_type():
    out, bucket, fake = await _add(PNG)
    path, data, opts = bucket.upload.call_args.args
    assert re.fullmatch(rf"{CLINIC}/dental-sittings/{APPT}/[0-9a-f]{{32}}\.png", path)
    assert data == PNG and opts == {"content-type": "image/png"}
    fake.storage.from_.assert_called_with("lab-reports")
    row = fake.table.return_value.insert.call_args.args[0]
    assert row == {"clinic_id": CLINIC, "appointment_id": APPT, "storage_path": path,
                   "content_type": "image/png", "size_bytes": len(PNG), "uploaded_by": "desk"}
    assert out["id"] == PHOTO


@pytest.mark.asyncio
@pytest.mark.parametrize("data, appt, existing, message", [
    (JPEG, _sitting("cancelled"), 0, "booked or completed"),
    (b"", None, 0, "empty"),
    (b"\xff\xd8\xff" + b"0" * dp.PHOTO_MAX_BYTES, None, 0, "3 MB"),
    (b"%PDF-1.4", None, 0, "JPG, PNG or WebP"),
    (JPEG, None, dp.PHOTO_MAX_PER_SITTING, "at most 6"),
], ids=["cancelled", "empty", "too-big", "pdf", "limit"])
async def test_photo_refusals(data, appt, existing, message):
    with pytest.raises(dp.DentalError, match=message):
        await _add(data, appt, existing)


@pytest.mark.asyncio
async def test_failed_index_row_removes_the_uploaded_file():
    storage, bucket = _storage()
    sb_mock = AsyncMock(side_effect=[MagicMock(count=0), RuntimeError("db down")])
    with patch("app.services.dental_plans.supabase", MagicMock(storage=storage)), \
         patch("app.services.dental_plans.sb", sb_mock):
        with pytest.raises(RuntimeError):
            await dp.add_sitting_photo(CLINIC, _sitting(), JPEG, "desk")
    uploaded = bucket.upload.call_args.args[0]
    bucket.remove.assert_called_once_with([uploaded])


@pytest.mark.asyncio
async def test_invalid_clinic_scope_is_refused():
    with pytest.raises(ValueError):
        await dp.add_sitting_photo("default", _sitting(), JPEG, "desk")


# ═══════ list + delete ═══════


@pytest.mark.asyncio
async def test_sitting_photos_returns_signed_urls_per_sitting():
    storage, bucket = _storage()
    bucket.create_signed_urls.return_value = [
        {"path": "p/1.jpg", "signedURL": "https://s/1"}, {"path": "p/2.jpg", "signedURL": "https://s/2"}]
    rows = [{"id": "a", "appointment_id": APPT, "storage_path": "p/1.jpg", "created_at": "t1"},
            {"id": "b", "appointment_id": APPT, "storage_path": "p/2.jpg", "created_at": "t2"}]
    with patch("app.services.dental_plans.supabase", MagicMock(storage=storage)), \
         patch("app.services.dental_plans.sb", AsyncMock(return_value=MagicMock(data=rows))):
        out = await dp.sitting_photos(CLINIC, [APPT])
    assert [p["url"] for p in out[APPT]] == ["https://s/1", "https://s/2"]
    assert bucket.create_signed_urls.call_args.args == (["p/1.jpg", "p/2.jpg"], dp.PHOTO_URL_SECONDS)


@pytest.mark.asyncio
async def test_sitting_photos_never_breaks_the_plan_view():
    with patch("app.services.dental_plans.supabase", MagicMock()), \
         patch("app.services.dental_plans.sb", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await dp.sitting_photos(CLINIC, [APPT]) == {}
    assert await dp.sitting_photos(CLINIC, []) == {}


@pytest.mark.asyncio
async def test_delete_removes_row_then_file_and_survives_storage_errors():
    storage, bucket = _storage()
    bucket.remove.side_effect = RuntimeError("storage down")
    with patch("app.services.dental_plans.supabase", MagicMock(storage=storage)), \
         patch("app.services.dental_plans.sb", AsyncMock(return_value=MagicMock(data=[{"storage_path": "p/1.jpg"}]))):
        assert await dp.delete_sitting_photo(CLINIC, APPT, PHOTO) is True
    bucket.remove.assert_called_once_with(["p/1.jpg"])
    with patch("app.services.dental_plans.supabase", MagicMock(storage=storage)), \
         patch("app.services.dental_plans.sb", AsyncMock(return_value=MagicMock(data=[]))):
        assert await dp.delete_sitting_photo(CLINIC, APPT, PHOTO) is False


# ═══════ routes ═══════


def _route_patches(sitting=None):
    return [
        patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=DENTAL)),
        patch("app.services.dental_plans.get_sitting", new=AsyncMock(return_value=sitting or _sitting())),
        patch("app.routers.admin.log_admin_action", new=AsyncMock()),
    ]


def _run(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


def test_staff_without_dental_permission_cannot_upload(as_user):
    as_user(STAFF)
    res = client.post(f"/admin/dental/sittings/{APPT}/photos", files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert res.status_code == 403


def test_upload_route_stores_and_audits(as_user):
    as_user(ADMIN)
    add = AsyncMock(return_value={"id": PHOTO, "size_bytes": len(JPEG)})
    res = _run(_route_patches() + [patch("app.services.dental_plans.add_sitting_photo", add)],
               lambda: client.post(f"/admin/dental/sittings/{APPT}/photos?clinic_id={CLINIC}",
                                   files={"file": ("notes.jpg", JPEG, "image/jpeg")}))
    assert res.status_code == 200 and res.json()["photo_id"] == PHOTO
    clinic_id, appt, data, by = add.await_args.args
    assert clinic_id == CLINIC and appt["id"] == APPT and data == JPEG and by == "dental_admin"


def test_upload_route_reads_at_most_one_byte_past_the_cap(as_user):
    as_user(ADMIN)
    add = AsyncMock(side_effect=dp.DentalError("The photo is larger than 3 MB."))
    big = b"\xff\xd8\xff" + b"0" * (dp.PHOTO_MAX_BYTES + 50_000)
    res = _run(_route_patches() + [patch("app.services.dental_plans.add_sitting_photo", add)],
               lambda: client.post(f"/admin/dental/sittings/{APPT}/photos",
                                   files={"file": ("big.jpg", big, "image/jpeg")}))
    assert res.status_code == 400 and "3 MB" in res.json()["detail"]
    assert len(add.await_args.args[2]) == dp.PHOTO_MAX_BYTES + 1


def test_upload_route_hides_storage_errors(as_user):
    as_user(ADMIN)
    add = AsyncMock(side_effect=RuntimeError("secret bucket key"))
    res = _run(_route_patches() + [patch("app.services.dental_plans.add_sitting_photo", add)],
               lambda: client.post(f"/admin/dental/sittings/{APPT}/photos",
                                   files={"file": ("a.jpg", JPEG, "image/jpeg")}))
    assert res.status_code == 502 and "secret" not in res.text


def test_upload_to_another_branchs_sitting_is_refused(as_user):
    pinned = AdminUser(username="b1", role="staff", clinic_id=CLINIC, user_id="u9",
                       permissions=["DENTAL_PLANS_MANAGE"], branch_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    as_user(pinned)
    add = AsyncMock()
    other = _sitting(branch_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    res = _run(_route_patches(other) + [patch("app.services.dental_plans.add_sitting_photo", add)],
               lambda: client.post(f"/admin/dental/sittings/{APPT}/photos",
                                   files={"file": ("a.jpg", JPEG, "image/jpeg")}))
    assert res.status_code in (403, 404)
    add.assert_not_awaited()


def test_delete_route(as_user):
    as_user(ADMIN)
    delete = AsyncMock(return_value=True)
    res = _run(_route_patches() + [patch("app.services.dental_plans.delete_sitting_photo", delete)],
               lambda: client.delete(f"/admin/dental/sittings/{APPT}/photos/{PHOTO}"))
    assert res.status_code == 200
    delete.assert_awaited_once_with(CLINIC, APPT, PHOTO)
    res = _run(_route_patches(), lambda: client.delete(f"/admin/dental/sittings/{APPT}/photos/not-a-uuid"))
    assert res.status_code == 400


# ═══════ export ═══════


PLAN = {"id": PLAN_ID, "patient_name": "Ravi Kumar", "patient_phone": "+919876543210",
        "treatment_name": "Root Canal Treatment", "tooth_numbers": "36", "status": "active",
        "planned_sittings": 3, "created_at": "2026-09-20T10:00:00+00:00", "quoted_amount_paise": 600000,
        "notes": None}
EMPTY_PLAN = {**PLAN, "id": "66666666-6666-6666-6666-666666666666", "treatment_name": "Scaling",
              "quoted_amount_paise": None}
SITTINGS = [
    {"id": APPT, "treatment_plan_id": PLAN_ID, "sitting_number": 1, "status": "completed",
     "appointment_date": "2026-09-22", "appointment_time": "10:30:00", "doctor_name": "Dr. Priya",
     "amount_collected_paise": 100000, "sitting_notes": "=HYPERLINK(\"http://evil\")", "review_rating": 3},
    {"id": "77777777-7777-7777-7777-777777777777", "treatment_plan_id": PLAN_ID, "sitting_number": 2,
     "status": "confirmed", "appointment_date": "2026-10-01", "appointment_time": "11:00:00",
     "doctor_name": "Dr. Priya", "amount_collected_paise": None, "sitting_notes": None, "review_rating": None},
    {"id": "88888888-8888-8888-8888-888888888888", "treatment_plan_id": None, "booking_type": "consultation"},
]


def _parse(body):
    assert body.startswith("﻿")
    return list(csv.reader(io.StringIO(body[1:])))


def test_export_csv_one_row_per_sitting_and_formula_safe():
    rows = _parse(build_dental_history_csv([PLAN, EMPTY_PLAN], SITTINGS, {APPT: 2}))
    header, data = rows[0], rows[1:]
    assert len(data) == 3                                      # 2 sittings + 1 plan with none; unlinked skipped
    first = dict(zip(header, data[0]))
    assert first["Treatment"] == "Root Canal Treatment" and first["Sitting"] == "1"
    assert first["Collected this sitting (Rs)"] == "1000.00"
    assert first["Collected in plan (Rs)"] == "1000.00" and first["Balance (Rs)"] == "5000.00"
    assert first["Work done / notes"].startswith("'=")        # never a live formula in Excel
    assert first["Note photos"] == "2" and first["Patient review"] == "Excellent"
    assert first["Time"] == "10:30" and first["Phone"] == "+919876543210"
    empty = dict(zip(header, data[2]))
    assert empty["Treatment"] == "Scaling" and empty["Sitting"] == "" and empty["Estimate (Rs)"] == ""


def _export_patches(plans, appts, photo_rows=()):
    return _route_patches() + [
        patch("app.routers.admin._dental_history", new=AsyncMock(return_value=(plans, appts))),
        patch("app.routers.admin.sb", new=AsyncMock(return_value=MagicMock(data=list(photo_rows)))),
    ]


def test_export_route_downloads_audits_and_masks(as_user):
    as_user(ADMIN)
    patches = _export_patches([PLAN], SITTINGS, [{"appointment_id": APPT}])
    for p in patches:
        p.start()
    try:
        res = client.get("/admin/dental/patient-history/export?phone=9876543210")
        from app.routers import admin as admin_mod
        audit = admin_mod.log_admin_action.await_args
    finally:
        for p in patches:
            p.stop()
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "treatment_history_3210_" in res.headers["content-disposition"]
    assert res.headers["cache-control"] == "no-store"
    assert audit.kwargs["action"] == "DENTAL_PATIENT_EXPORT"
    assert "9876543210" not in str(audit.kwargs["resource_id"])


def test_export_route_404_without_plans_and_400_for_bad_phone(as_user):
    as_user(ADMIN)
    res = _run(_export_patches([], []), lambda: client.get("/admin/dental/patient-history/export?phone=9876543210"))
    assert res.status_code == 404
    res = _run(_export_patches([PLAN], []), lambda: client.get("/admin/dental/patient-history/export?phone=abc"))
    assert res.status_code == 400


def test_export_needs_dental_permission(as_user):
    as_user(STAFF)
    assert client.get("/admin/dental/patient-history/export?phone=9876543210").status_code == 403


# ═══════ migration 094 on real PostgreSQL ═══════


def test_094_constraints_cascade_and_rls(real_pg_conn):
    import uuid as _uuid
    import psycopg2
    cur = real_pg_conn.cursor()
    cur.execute("INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Photos 094', %s, 'dental', true) "
                "RETURNING id", ("+9196" + _uuid.uuid4().hex[:8],))
    cid = str(cur.fetchone()[0])
    try:
        cur.execute("INSERT INTO appointments (clinic_id, patient_phone, department, appointment_date, "
                    "appointment_time, status) VALUES (%s, '+919876543210', 'Dental', current_date, '10:00', "
                    "'completed') RETURNING id", (cid,))
        appt = str(cur.fetchone()[0])
        ins = ("INSERT INTO dental_sitting_photos (clinic_id, appointment_id, storage_path, content_type, size_bytes) "
               "VALUES (%s, %s, %s, %s, %s)")
        cur.execute(ins, (cid, appt, f"{cid}/dental-sittings/{appt}/a.jpg", "image/jpeg", 1000))
        for bad in [("x/b.svg", "image/svg+xml", 10), ("x/c.jpg", "image/jpeg", 0),
                    ("x/d.jpg", "image/jpeg", 3 * 1024 * 1024 + 1)]:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(ins, (cid, appt, *bad))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(ins, (cid, appt, f"{cid}/dental-sittings/{appt}/a.jpg", "image/jpeg", 5))
        cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'dental_sitting_photos'")
        assert cur.fetchone() == (True, True)
        cur.execute("DELETE FROM appointments WHERE id = %s", (appt,))
        cur.execute("SELECT count(*) FROM dental_sitting_photos WHERE clinic_id = %s", (cid,))
        assert cur.fetchone()[0] == 0
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))
