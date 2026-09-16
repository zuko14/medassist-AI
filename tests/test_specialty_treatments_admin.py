"""Treatments admin API: tenant scoped, plan gated, permission gated, and no
path can attach another clinic's doctor to a treatment."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import admin as admin_router
from app.routers.admin import (
    AdminUser,
    TreatmentBulkStatus,
    TreatmentCreate,
    TreatmentDescriptionRequest,
    TreatmentDoctorsUpdate,
    TreatmentUpdate,
    create_treatment,
    delete_treatment,
    generate_treatment_description_admin,
    get_current_admin,
    list_treatments_admin,
    load_starter_treatments,
    set_treatment_doctors,
    set_treatments_status,
    update_treatment,
)

CLINIC = "clinic-1"
T1 = "11111111-1111-1111-1111-111111111111"
D1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
D2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ADMIN = AdminUser("admin1", role="clinic_admin", clinic_id=CLINIC, user_id="u1")


def _clinic(plan="derma", features=None):
    return AsyncMock(return_value={"id": CLINIC, "plan": plan, "features": features or {}})


def _patches(sb_results, plan="derma", features=None):
    fake_supabase = MagicMock()
    return (
        fake_supabase,
        patch.object(admin_router, "get_clinic_by_id", _clinic(plan, features)),
        patch.object(admin_router, "sb", AsyncMock(side_effect=sb_results)),
        patch.object(admin_router, "supabase", fake_supabase),
        patch.object(admin_router, "log_admin_action", AsyncMock()),
        patch.object(admin_router, "resolve_clinic_id_for_write", AsyncMock(return_value=CLINIC)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("plan, features", [("polyclinic", None), ("enterprise", None), ("soloclinic", None),
                                            ("derma", {"specialty_treatments": False})])
async def test_catalogue_is_refused_when_specialty_is_not_enabled(plan, features):
    fake, *ps = _patches([], plan, features)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await list_treatments_admin(clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_attaches_doctor_ids_and_is_clinic_scoped():
    rows = MagicMock(data=[{"id": T1, "name": "Hair PRP Therapy"}])
    links = MagicMock(data=[{"treatment_id": T1, "doctor_id": D1}])
    fake, *ps = _patches([rows, links])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        result = await list_treatments_admin(clinic_id=CLINIC, user=ADMIN)
    assert result == [{"id": T1, "name": "Hair PRP Therapy", "doctor_ids": [D1]}]
    eq_calls = [c.args for c in fake.table.return_value.select.return_value.eq.call_args_list]
    assert ("clinic_id", CLINIC) in eq_calls


@pytest.mark.asyncio
async def test_create_converts_rupees_and_forces_clinic_and_source():
    count = MagicMock(data=[], count=3)
    created = MagicMock(data=[{"id": T1, "name": "Chemical Peel"}])
    fake, *ps = _patches([count, created])
    body = TreatmentCreate(name="  Chemical Peel ", category="Pigmentation", price_from_rupees=1500)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        row = await create_treatment(body=body, request=None, clinic_id=CLINIC, user=ADMIN)
    payload = fake.table.return_value.insert.call_args.args[0]
    assert payload["name"] == "Chemical Peel"
    assert payload["price_from_paise"] == 150000
    assert payload["clinic_id"] == CLINIC
    assert payload["source"] == "custom"
    assert "price_from_rupees" not in payload
    assert row["doctor_ids"] == []


@pytest.mark.asyncio
async def test_create_refuses_past_the_catalogue_limit():
    fake, *ps = _patches([MagicMock(data=[], count=500)])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await create_treatment(body=TreatmentCreate(name="X Treatment", category="General"),
                                   request=None, clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_name_is_a_409_with_a_readable_message():
    dup = Exception('duplicate key value violates unique constraint "idx_unique_treatment_name_per_clinic"')
    fake, *ps = _patches([MagicMock(data=[], count=1), dup])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await create_treatment(body=TreatmentCreate(name="Chemical Peel", category="Pigmentation"),
                                   request=None, clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


def test_model_validation():
    with pytest.raises(ValidationError):
        TreatmentCreate(name="   ", category="General")
    with pytest.raises(ValidationError):
        TreatmentCreate(name="Laser Hair Reduction", category="Laser", short_name="x" * 25)
    with pytest.raises(ValidationError):
        TreatmentCreate(name="IVF", category="Treatments", price_from_rupees=-1)
    with pytest.raises(ValidationError):
        TreatmentCreate(name="IVF", category="Treatments", duration_minutes=2)
    assert TreatmentCreate(name="IVF", category="Treatments", short_name="  ").short_name is None
    with pytest.raises(ValidationError):
        TreatmentUpdate(name="  ")


@pytest.mark.asyncio
async def test_update_rejects_non_uuid_and_missing_rows():
    fake, *ps = _patches([MagicMock(data=[])])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as bad:
            await update_treatment(treatment_id="not-a-uuid", body=TreatmentUpdate(name="New"),
                                   request=None, clinic_id=CLINIC, user=ADMIN)
        with pytest.raises(HTTPException) as missing:
            await update_treatment(treatment_id=T1, body=TreatmentUpdate(price_from_rupees=200),
                                   request=None, clinic_id=CLINIC, user=ADMIN)
    assert bad.value.status_code == 400
    assert missing.value.status_code == 404
    payload = fake.table.return_value.update.call_args.args[0]
    assert payload["price_from_paise"] == 20000 and "updated_at" in payload


@pytest.mark.asyncio
async def test_doctor_mapping_rejects_a_doctor_from_another_clinic():
    treatment = MagicMock(data=[{"id": T1}])
    owned = MagicMock(data=[{"id": D1}])  # D2 is not this clinic's doctor
    fake, *ps = _patches([treatment, owned])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await set_treatment_doctors(treatment_id=T1, body=TreatmentDoctorsUpdate(doctor_ids=[D1, D2]),
                                         request=None, clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 400
    fake.table.return_value.delete.assert_not_called()


@pytest.mark.asyncio
async def test_doctor_mapping_replaces_links_with_clinic_id_on_every_row():
    treatment = MagicMock(data=[{"id": T1}])
    owned = MagicMock(data=[{"id": D1}, {"id": D2}])
    fake, *ps = _patches([treatment, owned, MagicMock(data=[]), MagicMock(data=[])])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        result = await set_treatment_doctors(treatment_id=T1, body=TreatmentDoctorsUpdate(doctor_ids=[D2, D1, D1]),
                                             request=None, clinic_id=CLINIC, user=ADMIN)
    assert result == {"treatment_id": T1, "doctor_ids": sorted([D1, D2])}
    rows = fake.table.return_value.insert.call_args.args[0]
    assert {r["doctor_id"] for r in rows} == {D1, D2}
    assert all(r["clinic_id"] == CLINIC and r["treatment_id"] == T1 for r in rows)


@pytest.mark.asyncio
async def test_bulk_status_is_scoped_and_validated():
    fake, *ps = _patches([MagicMock(data=[{"id": T1}])])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        result = await set_treatments_status(body=TreatmentBulkStatus(treatment_ids=[T1], is_active=True),
                                             request=None, clinic_id=CLINIC, user=ADMIN)
        with pytest.raises(HTTPException) as bad:
            await set_treatments_status(body=TreatmentBulkStatus(treatment_ids=["x"], is_active=True),
                                         request=None, clinic_id=CLINIC, user=ADMIN)
    assert result == {"updated": 1}
    assert bad.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_404_when_not_this_clinics_row():
    fake, *ps = _patches([MagicMock(data=[])])
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await delete_treatment(treatment_id=T1, request=None, clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_starter_list_uses_the_plan_specialty_never_the_request():
    fake, *ps = _patches([], plan="eye")
    with ps[0], ps[1], ps[2], ps[3], ps[4], \
         patch.object(admin_router, "seed_starter_treatments", AsyncMock(return_value={"added": 12, "skipped": 0})) as seed:
        result = await load_starter_treatments(request=None, clinic_id=CLINIC, user=ADMIN)
    seed.assert_awaited_once_with(CLINIC, "ophthalmology")
    assert result == {"added": 12, "skipped": 0}


@pytest.mark.asyncio
async def test_starter_list_refused_for_override_enabled_general_plan():
    fake, *ps = _patches([], plan="polyclinic", features={"specialty_treatments": True})
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        with pytest.raises(HTTPException) as exc:
            await load_starter_treatments(request=None, clinic_id=CLINIC, user=ADMIN)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_ai_description_endpoint_returns_a_draft_and_saves_nothing():
    draft = {"description": "a\nb", "description_hi": "c\nd", "description_te": "e\nf", "source": "ai"}
    fake, *ps = _patches([], plan="dental")
    with ps[0], ps[1], ps[2], ps[3], ps[4], \
         patch.object(admin_router, "generate_treatment_description", AsyncMock(return_value=draft)) as gen:
        result = await generate_treatment_description_admin(
            body=TreatmentDescriptionRequest(name="Root Canal Treatment", category="Tooth Pain"),
            clinic_id=CLINIC, user=ADMIN)
    assert result == draft
    assert gen.await_args.args[2] == "dental"
    fake.table.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("plan, features, specialty, enabled", [
    ("derma", {}, "dermatology", True),
    ("ivf", {}, "fertility", True),
    ("enterprise", {}, None, False),
    ("polyclinic", {"specialty_treatments": True}, None, True),
])
async def test_me_reports_specialty(plan, features, specialty, enabled):
    with patch.object(admin_router, "get_clinic_by_id", _clinic(plan, features)):
        me = await get_current_admin(user=ADMIN)
    assert me["specialty"] == specialty
    assert me["specialty_enabled"] is enabled


@pytest.mark.asyncio
async def test_me_without_a_clinic_reports_no_specialty():
    user = AdminUser("super", role="super_admin", clinic_id=None, user_id="super_admin_env")
    me = await get_current_admin(user=user)
    assert me["specialty"] is None and me["specialty_enabled"] is False
