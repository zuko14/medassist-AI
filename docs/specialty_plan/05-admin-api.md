# Task 5 — Admin API: `/admin/treatments*` and `/admin/me` specialty fields

**Files:**
- Modify: `app/routers/admin.py`
  - import lists near lines 32 (`from app.database import (`) and 43 (`from app.services.tenant import (`)
  - `get_current_admin` (`GET /admin/me`, ~line 651)
  - new section inserted directly before `CSV_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB`
- Test: `tests/test_specialty_treatments_admin.py`

**Interfaces:**
- Consumes:
  - `specialty_enabled`, `SPECIALTY_BY_PLAN` (Task 2)
  - `is_uuid` (Task 3)
  - `seed_starter_treatments` (Task 3)
  - `generate_treatment_description` (Task 4)
  - existing admin.py helpers: `enforce_clinic_access`, `resolve_clinic_id_for_write`, `require_permission`, `verify_credentials`, `log_admin_action`, `get_clinic_by_id`, `_friendly_db_error`, `_is_duplicate_error`, `sb`, `supabase`
- Produces (all JSON; all tenant-scoped through `?clinic_id`, which the panel's `withScope()` appends):

| Method & path | Auth | Body | Returns |
|---|---|---|---|
| `GET /admin/treatments` | any admin/staff | — | `list[treatment + "doctor_ids": list[str]]` (active and hidden) |
| `POST /admin/treatments` | `TREATMENTS_MANAGE` | `TreatmentCreate` | created row + `doctor_ids: []` |
| `PUT /admin/treatments/{treatment_id}` | `TREATMENTS_MANAGE` | `TreatmentUpdate` | updated row |
| `DELETE /admin/treatments/{treatment_id}` | `TREATMENTS_MANAGE` | — | `{"success": true}` |
| `PUT /admin/treatments/{treatment_id}/doctors` | `TREATMENTS_MANAGE` | `{"doctor_ids": [uuid,…]}` | `{"treatment_id", "doctor_ids"}` |
| `POST /admin/treatments/status` | `TREATMENTS_MANAGE` | `{"treatment_ids": [uuid,…], "is_active": bool}` | `{"updated": int}` |
| `POST /admin/treatments/starter` | `TREATMENTS_MANAGE` | `{}` | `{"added": int, "skipped": int}` |
| `POST /admin/treatments/ai-description` | `TREATMENTS_MANAGE` | `{"name", "category"?}` | `{"description", "description_hi", "description_te", "source"}` (not saved) |

  - `GET /admin/me` gains `"specialty": str | None` and `"specialty_enabled": bool`.
  - Every treatments endpoint returns **403** `"The treatments catalogue is not enabled for this clinic's plan."` when `specialty_enabled(clinic)` is False. For an enterprise clinic without an override this is the correct answer.

---

- [ ] **Step 1: Write the failing test** — create `tests/test_specialty_treatments_admin.py`:

```python
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
```

- [ ] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_treatments_admin.py -q
```
Expected: `ImportError: cannot import name 'TreatmentCreate'`.

- [ ] **Step 3: Imports in `app/routers/admin.py`**

a) In the existing `from app.database import (` list (~line 32), add `is_uuid,`.

b) In the existing `from app.services.tenant import (` list (~line 43), add `SPECIALTY_BY_PLAN,` and `specialty_enabled,`.

c) Add near the other service imports:
```python
from app.services.ai_engine import generate_treatment_description
from app.services.specialty_catalog import seed_starter_treatments
```
If `app.services.ai_engine` is already imported with a name list, add to that list.

d) Verify these names are importable in admin.py, and add any that are missing to the matching import line: `datetime`, `timezone` (from `datetime`), `Field`, `field_validator` (from `pydantic`), `Optional` (from `typing`).
```bash
grep -n "^from datetime import\|^from pydantic import\|^from typing import" app/routers/admin.py
```

- [ ] **Step 4: `/admin/me`.** In `get_current_admin`, replace exactly:
```python
    if not scoped_clinic_id:
        return {
            **base_response,
            "plan": None,
            "features": None,
        }
```
with:
```python
    if not scoped_clinic_id:
        return {
            **base_response,
            "plan": None,
            "features": None,
            "specialty": None,
            "specialty_enabled": False,
        }
```
and replace exactly:
```python
    return {
        **base_response,
        "plan": plan,
        "features": features,
    }
```
with:
```python
    return {
        **base_response,
        "plan": plan,
        "features": features,
        # Specialty panels show the Treatments tab from THESE two keys, never
        # from features[] (the enterprise wildcard lists every feature).
        "specialty": SPECIALTY_BY_PLAN.get(plan),
        "specialty_enabled": specialty_enabled(clinic),
    }
```

- [ ] **Step 5: Treatments section.** Insert directly **before** the line `CSV_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB` (exact content):

```python
# ═══════════════════════════════════════════════════════════════════════════
# SPECIALTY TREATMENTS CATALOGUE (migration 077)
# ═══════════════════════════════════════════════════════════════════════════

#: Matches app.database._TREATMENT_MAX_ROWS, so the bot's single bounded read
#: always sees the whole catalogue.
_TREATMENT_CATALOG_LIMIT = 500
_TREATMENT_TEXT_FIELDS = (
    "short_name", "description", "description_hi", "description_te",
    "concerns", "prep_instructions",
)


def _strip_optional(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v or None


def _strip_required(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("must not be empty")
    return v


class TreatmentCreate(BaseModel):
    name: str = Field(..., max_length=120)
    category: str = Field(..., max_length=60)
    short_name: Optional[str] = Field(default=None, max_length=24)
    description: Optional[str] = Field(default=None, max_length=400)
    description_hi: Optional[str] = Field(default=None, max_length=600)
    description_te: Optional[str] = Field(default=None, max_length=600)
    concerns: Optional[str] = Field(default=None, max_length=500)
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    price_from_rupees: int = Field(default=0, ge=0, le=10_000_000)
    prep_instructions: Optional[str] = Field(default=None, max_length=600)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=10_000)

    _v_required = field_validator("name", "category")(classmethod(lambda cls, v: _strip_required(v)))
    _v_optional = field_validator(*_TREATMENT_TEXT_FIELDS)(classmethod(lambda cls, v: _strip_optional(v)))


class TreatmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    category: Optional[str] = Field(default=None, max_length=60)
    short_name: Optional[str] = Field(default=None, max_length=24)
    description: Optional[str] = Field(default=None, max_length=400)
    description_hi: Optional[str] = Field(default=None, max_length=600)
    description_te: Optional[str] = Field(default=None, max_length=600)
    concerns: Optional[str] = Field(default=None, max_length=500)
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    price_from_rupees: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    prep_instructions: Optional[str] = Field(default=None, max_length=600)
    is_active: Optional[bool] = None
    display_order: Optional[int] = Field(default=None, ge=0, le=10_000)

    _v_required = field_validator("name", "category")(classmethod(lambda cls, v: _strip_required(v)))
    _v_optional = field_validator(*_TREATMENT_TEXT_FIELDS)(classmethod(lambda cls, v: _strip_optional(v)))


class TreatmentDoctorsUpdate(BaseModel):
    doctor_ids: list[str] = Field(default_factory=list, max_length=200)


class TreatmentBulkStatus(BaseModel):
    treatment_ids: list[str] = Field(..., min_length=1, max_length=_TREATMENT_CATALOG_LIMIT)
    is_active: bool


class TreatmentDescriptionRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    category: Optional[str] = Field(default=None, max_length=60)


def _client_ip(request: Optional[Request]) -> str:
    return request.client.host if (request and request.client) else "unknown"


async def _require_specialty_clinic(clinic_id: str) -> dict:
    """403 unless the treatments catalogue applies to this clinic.

    specialty_enabled(), not has_feature(): the enterprise wildcard would
    otherwise open the catalogue for every enterprise tenant.
    """
    clinic = await get_clinic_by_id(clinic_id)
    if not specialty_enabled(clinic):
        raise HTTPException(
            status_code=403,
            detail="The treatments catalogue is not enabled for this clinic's plan.",
        )
    return clinic


def _treatment_row(body: BaseModel, partial: bool) -> dict:
    try:
        data = body.model_dump(exclude_unset=partial, exclude={"price_from_rupees"})
        fields_set = body.model_fields_set
    except AttributeError:  # pydantic v1
        data = body.dict(exclude_unset=partial, exclude={"price_from_rupees"})
        fields_set = body.__fields_set__
    if not partial or "price_from_rupees" in fields_set:
        if body.price_from_rupees is not None:
            data["price_from_paise"] = int(body.price_from_rupees) * 100
    return data


def _require_uuid(value: str, label: str) -> str:
    if not is_uuid(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")
    return str(value)


@router.get("/treatments")
async def list_treatments_admin(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """The whole catalogue (shown and hidden), each row with its doctor ids."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        await _require_specialty_clinic(effective_clinic_id)
        rows_res = await sb(
            supabase.table("specialty_treatments").select("*").eq("clinic_id", effective_clinic_id)
            .order("display_order").order("name").order("id").limit(_TREATMENT_CATALOG_LIMIT)
        )
        links_res = await sb(
            supabase.table("treatment_doctors").select("treatment_id, doctor_id")
            .eq("clinic_id", effective_clinic_id).limit(10000)
        )
        by_treatment: dict[str, list[str]] = {}
        for link in links_res.data or []:
            by_treatment.setdefault(str(link["treatment_id"]), []).append(str(link["doctor_id"]))
        rows = rows_res.data or []
        for row in rows:
            row["doctor_ids"] = by_treatment.get(str(row["id"]), [])
        return rows
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching treatments for clinic_id={effective_clinic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch treatments")


@router.post("/treatments")
async def create_treatment(
    body: TreatmentCreate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        await _require_specialty_clinic(effective_clinic_id)

        count_res = await sb(
            supabase.table("specialty_treatments").select("id", count="exact")
            .eq("clinic_id", effective_clinic_id).limit(1)
        )
        if (count_res.count or 0) >= _TREATMENT_CATALOG_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"A clinic can list at most {_TREATMENT_CATALOG_LIMIT} treatments.",
            )

        data = _treatment_row(body, partial=False)
        data["clinic_id"] = effective_clinic_id
        data["source"] = "custom"
        # unscoped: insert_scoped_by_payload
        result = await sb(supabase.table("specialty_treatments").insert(data))
        row = result.data[0]

        await log_admin_action(
            user=user,
            action="create_treatment",
            resource_type="specialty_treatment",
            resource_id=row["id"],
            details={"name": row.get("name")},
            ip_address=_client_ip(request),
        )
        row["doctor_ids"] = []
        return row
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating treatment for clinic_id={effective_clinic_id}: {e}", exc_info=True)
        if _is_duplicate_error(e):
            raise HTTPException(status_code=409, detail="A treatment with this name already exists.")
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to create treatment"))


@router.put("/treatments/{treatment_id}")
async def update_treatment(
    treatment_id: str,
    body: TreatmentUpdate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    treatment_id = _require_uuid(treatment_id, "treatment id")
    try:
        await _require_specialty_clinic(effective_clinic_id)
        data = _treatment_row(body, partial=True)
        if not data:
            return {"message": "No fields to update"}
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        result = await sb(
            supabase.table("specialty_treatments").update(data)
            .eq("clinic_id", effective_clinic_id).eq("id", treatment_id)
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Treatment not found")

        await log_admin_action(
            user=user,
            action="update_treatment",
            resource_type="specialty_treatment",
            resource_id=treatment_id,
            details={"updated_fields": sorted(k for k in data if k != "updated_at")},
            ip_address=_client_ip(request),
        )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating treatment {treatment_id}: {e}", exc_info=True)
        if _is_duplicate_error(e):
            raise HTTPException(status_code=409, detail="A treatment with this name already exists.")
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to update treatment"))


@router.delete("/treatments/{treatment_id}")
async def delete_treatment(
    treatment_id: str,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    """Hard delete. Past bookings keep their treatment_name (FK is ON DELETE
    SET NULL); doctor links cascade."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    treatment_id = _require_uuid(treatment_id, "treatment id")
    try:
        await _require_specialty_clinic(effective_clinic_id)
        result = await sb(
            supabase.table("specialty_treatments").delete()
            .eq("clinic_id", effective_clinic_id).eq("id", treatment_id)
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Treatment not found")

        await log_admin_action(
            user=user,
            action="delete_treatment",
            resource_type="specialty_treatment",
            resource_id=treatment_id,
            details={"deleted_row": result.data[0]},
            ip_address=_client_ip(request),
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting treatment {treatment_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to delete treatment"))


@router.put("/treatments/{treatment_id}/doctors")
async def set_treatment_doctors(
    treatment_id: str,
    body: TreatmentDoctorsUpdate,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    """Replace the doctors who perform a treatment. [] means any active doctor."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    treatment_id = _require_uuid(treatment_id, "treatment id")
    doctor_ids = sorted({str(d).strip() for d in body.doctor_ids if str(d).strip()})
    for doctor_id in doctor_ids:
        _require_uuid(doctor_id, "doctor id")
    try:
        await _require_specialty_clinic(effective_clinic_id)
        treatment = await sb(
            supabase.table("specialty_treatments").select("id")
            .eq("clinic_id", effective_clinic_id).eq("id", treatment_id)
        )
        if not treatment.data:
            raise HTTPException(status_code=404, detail="Treatment not found")

        if doctor_ids:
            owned = await sb(
                supabase.table("doctors").select("id")
                .eq("clinic_id", effective_clinic_id).in_("id", doctor_ids)
            )
            if {str(r["id"]) for r in (owned.data or [])} != set(doctor_ids):
                raise HTTPException(
                    status_code=400,
                    detail="One or more selected doctors do not belong to your clinic.",
                )

        # ponytail: delete-then-insert is not atomic. A failure between the two
        # leaves the treatment with no links, which the bot reads as "any active
        # doctor" — no booking or patient data is at risk. Move into a Postgres
        # function if that fallback ever matters.
        await sb(
            supabase.table("treatment_doctors").delete()
            .eq("clinic_id", effective_clinic_id).eq("treatment_id", treatment_id)
        )
        if doctor_ids:
            # unscoped: insert_scoped_by_payload
            await sb(supabase.table("treatment_doctors").insert([
                {"clinic_id": effective_clinic_id, "treatment_id": treatment_id, "doctor_id": d}
                for d in doctor_ids
            ]))

        await log_admin_action(
            user=user,
            action="set_treatment_doctors",
            resource_type="specialty_treatment",
            resource_id=treatment_id,
            details={"doctor_ids": doctor_ids},
            ip_address=_client_ip(request),
        )
        return {"treatment_id": treatment_id, "doctor_ids": doctor_ids}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting doctors for treatment {treatment_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to save doctors"))


@router.post("/treatments/status")
async def set_treatments_status(
    body: TreatmentBulkStatus,
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    """Show or hide several treatments at once (review of starter lists)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    ids = sorted({str(t).strip() for t in body.treatment_ids})
    for tid in ids:
        _require_uuid(tid, "treatment id")
    try:
        await _require_specialty_clinic(effective_clinic_id)
        result = await sb(
            supabase.table("specialty_treatments")
            .update({"is_active": body.is_active, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("clinic_id", effective_clinic_id).in_("id", ids)
        )
        updated = len(result.data or [])
        await log_admin_action(
            user=user,
            action="set_treatments_status",
            resource_type="specialty_treatment",
            resource_id=None,
            details={"treatment_ids": ids, "is_active": body.is_active, "updated": updated},
            ip_address=_client_ip(request),
        )
        return {"updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating treatment status for clinic_id={effective_clinic_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=_friendly_db_error(e, "Failed to update treatments"))


@router.post("/treatments/starter")
async def load_starter_treatments(
    request: Request = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    """Add the plan's starter treatments that are missing, hidden. Idempotent."""
    effective_clinic_id = None
    try:
        effective_clinic_id = await resolve_clinic_id_for_write(user, clinic_id)
        clinic = await _require_specialty_clinic(effective_clinic_id)
        specialty = SPECIALTY_BY_PLAN.get(clinic.get("plan"))
        if not specialty:
            raise HTTPException(
                status_code=400,
                detail="Starter treatments are available on the Dermatology, Eye, Dental and IVF plans.",
            )
        result = await seed_starter_treatments(effective_clinic_id, specialty)
        await log_admin_action(
            user=user,
            action="load_starter_treatments",
            resource_type="specialty_treatment",
            resource_id=None,
            details={"specialty": specialty, **result},
            ip_address=_client_ip(request),
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error loading starter treatments for clinic_id={effective_clinic_id}: {e}", exc_info=True)
        if _is_duplicate_error(e):
            raise HTTPException(
                status_code=409,
                detail="Starter treatments were just added by someone else. Refresh the page.",
            )
        raise HTTPException(status_code=500, detail="Failed to load starter treatments")


@router.post("/treatments/ai-description")
async def generate_treatment_description_admin(
    body: TreatmentDescriptionRequest,
    clinic_id: str = "default",
    user: AdminUser = Depends(require_permission("TREATMENTS_MANAGE")),
):
    """A 2-line EN/HI/TE draft for the admin to review. Saves nothing."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        clinic = await _require_specialty_clinic(effective_clinic_id)
        specialty = SPECIALTY_BY_PLAN.get(clinic.get("plan")) or "general"
        return await generate_treatment_description(body.name, body.category, specialty, clinic)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating treatment description for clinic_id={effective_clinic_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate a description")
```

Implementation notes (read before running tests):
- **Validators:** if the `field_validator(...)(classmethod(lambda …))` form triggers a pydantic error in this repo's pydantic version, write them as ordinary decorated methods with the same behaviour:
  ```python
  @field_validator("name", "category")
  @classmethod
  def _required_text(cls, v): return _strip_required(v)

  @field_validator("short_name", "description", "description_hi", "description_te", "concerns", "prep_instructions")
  @classmethod
  def _optional_text(cls, v): return _strip_optional(v)
  ```
- **Route order:** `POST /treatments/status`, `POST /treatments/starter` and `POST /treatments/ai-description` do not clash with `PUT` or `DELETE /treatments/{treatment_id}`, because the HTTP methods differ.
- **`log_admin_action`:** if it rejects `resource_id=None`, pass `resource_id="bulk"` instead.

- [ ] **Step 6: Run the new tests and the admin security matrices**

```bash
pytest tests/test_specialty_treatments_admin.py tests/test_admin_me.py tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_tenant_isolation_matrix.py tests/test_lint_unscoped_queries.py tests/test_phase2_unscoped_query_linter.py tests/test_lab_tests_admin.py tests/test_rls_security.py tests/test_security.py -q
```
Expected: all PASS. The two route matrices enumerate every `/admin*` route automatically and require each to refuse an unscoped super_admin. The new routes satisfy this through `enforce_clinic_access` and `resolve_clinic_id_for_write`. **Do not add the new routes to any allowlist.**

Run the orphan check.

- [ ] **Step 7: Commit**

```bash
git add app/routers/admin.py tests/test_specialty_treatments_admin.py
git commit -m "feat(admin): treatments catalogue API, doctor mapping, starter list, AI drafts, /me specialty

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
