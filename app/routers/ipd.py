"""Kriya IPD OS API (migration 111). Design: docs/ipd-os/ARCHITECTURE.md.

Every route: permission -> enforce_clinic_access -> ipd_access(clinic) ->
(new work) wind-down rule -> branch scope -> service. Zero-LLM.
"""

import logging
import re
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.database import sb, scoped_query, supabase
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services import ipd
from app.services.permissions import resolve_owned_branch
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache, ipd_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ipd", tags=["IPD Admin"])

ANY_IPD = ("IPD_ADMIN", "IPD_ADMISSIONS", "IPD_NURSING", "IPD_CLINICAL", "IPD_BILLING")
CODE = r"^[A-Za-z0-9_\-]{1,20}$"
CATEGORY = r"^[a-z0-9_]{2,30}$"
PHONE = r"^\+?[0-9]{10,15}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── models ──────────────────────────────────────────────────────────────────

class SettingsIn(_In):
    bed_tariffs: Optional[dict[str, int]] = None          # bed category -> paise per day
    discharge_cutoff_hour: Optional[int] = Field(None, ge=0, le=23)
    reservation_minutes: Optional[int] = Field(None, ge=5, le=1440)

    @model_validator(mode="after")
    def _tariffs(self):
        for k, v in (self.bed_tariffs or {}).items():
            if not re.match(CATEGORY, k) or not 0 <= v <= 100_000_000:
                raise ValueError(f"Invalid tariff {k!r}")
        return self


class WardIn(_In):
    code: str = Field(..., pattern=CODE)
    name: str = Field(..., min_length=1, max_length=80)
    ward_type: Literal["general", "semi_private", "private", "icu", "hdu", "nicu", "picu", "maternity",
                       "isolation", "daycare", "emergency", "other"] = "general"
    gender_restriction: Literal["any", "male", "female"] = "any"
    branch_id: Optional[str] = None


class WardPatch(_In):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    gender_restriction: Optional[Literal["any", "male", "female"]] = None
    is_active: Optional[bool] = None
    branch_id: Optional[str] = None     # sent as null = make the ward shared by all branches


class BedIn(_In):
    code: str = Field(..., pattern=CODE)
    bed_category: str = Field(..., pattern=CATEGORY)
    room_label: Optional[str] = Field(None, max_length=40)
    features: list[str] = Field(default_factory=list, max_length=20)


class BedsIn(_In):
    beds: list[BedIn] = Field(..., min_length=1, max_length=200)


class BedPatch(_In):
    room_label: Optional[str] = Field(None, max_length=40)
    bed_category: Optional[str] = Field(None, pattern=CATEGORY)
    features: Optional[list[str]] = Field(None, max_length=20)
    is_active: Optional[bool] = None


class BedStatusIn(_In):
    status: Literal["available", "cleaning", "maintenance", "blocked"]
    reason: Optional[str] = Field(None, max_length=200)


class AdmissionIn(_In):
    patient_id: str
    family_member_id: Optional[str] = None
    admitting_doctor_id: str
    attending_doctor_id: Optional[str] = None
    source: Literal["opd", "emergency", "direct", "referral", "transfer_in"] = "direct"
    source_appointment_id: Optional[str] = None
    source_encounter_id: Optional[str] = None
    priority: Literal["elective", "urgent", "emergency"] = "elective"
    reason: str = Field(..., min_length=3, max_length=500)
    provisional_diagnosis: Optional[str] = Field(None, max_length=500)
    requested_bed_category: Optional[str] = Field(None, pattern=CATEGORY)
    payer_type: Literal["self", "insurance", "corporate", "government_scheme"] = "self"
    payer_name: Optional[str] = Field(None, max_length=120)
    policy_number: Optional[str] = Field(None, max_length=60)
    attendant_name: Optional[str] = Field(None, max_length=100)
    attendant_phone: Optional[str] = Field(None, pattern=PHONE)
    attendant_relation: Optional[str] = Field(None, max_length=40)
    consent_recorded: bool = False
    expected_discharge_on: Optional[date] = None
    bed_id: Optional[str] = None
    branch_id: Optional[str] = None
    idempotency_key: str = Field(..., min_length=8, max_length=80)


class AdmissionPatch(_In):
    attending_doctor_id: Optional[str] = None
    expected_discharge_on: Optional[date] = None
    payer_type: Optional[Literal["self", "insurance", "corporate", "government_scheme"]] = None
    payer_name: Optional[str] = Field(None, max_length=120)
    policy_number: Optional[str] = Field(None, max_length=60)
    attendant_name: Optional[str] = Field(None, max_length=100)
    attendant_phone: Optional[str] = Field(None, pattern=PHONE)
    attendant_relation: Optional[str] = Field(None, max_length=40)
    consent_recorded: Optional[bool] = None


class BedChoiceIn(_In):
    bed_id: str


class ReserveIn(_In):
    bed_id: str
    minutes: Optional[int] = Field(None, ge=5, le=1440)


class TransferIn(_In):
    to_bed_id: str
    expected_bed_id: str            # the bed the screen showed: a stale screen gets 409
    reason: str = Field(..., min_length=3, max_length=300)


class ReasonIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class DischargeInitIn(_In):
    discharge_type: Literal["regular", "lama", "dama", "absconded", "death", "transfer_out"]


class DischargeCompleteIn(_In):
    waiver_reason: Optional[str] = Field(None, min_length=5, max_length=300)


class ObservationIn(_In):
    kind: Literal["vitals", "intake", "output"]
    observed_at: datetime
    bp_systolic: Optional[int] = Field(None, ge=50, le=300)
    bp_diastolic: Optional[int] = Field(None, ge=20, le=200)
    pulse_bpm: Optional[int] = Field(None, ge=20, le=250)
    resp_rate: Optional[int] = Field(None, ge=4, le=80)
    temperature_c: Optional[float] = Field(None, ge=30, le=45)
    spo2_pct: Optional[int] = Field(None, ge=50, le=100)
    pain_score: Optional[int] = Field(None, ge=0, le=10)
    blood_glucose_mg_dl: Optional[int] = Field(None, ge=10, le=1500)
    consciousness: Optional[Literal["alert", "confused", "voice", "pain", "unresponsive"]] = None
    io_route: Optional[Literal["oral", "iv", "ng_tube", "urine", "drain", "vomitus", "stool", "other"]] = None
    volume_ml: Optional[int] = Field(None, ge=1, le=10000)
    note: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _shape(self):
        vitals = (self.bp_systolic, self.pulse_bpm, self.resp_rate, self.temperature_c, self.spo2_pct,
                  self.pain_score, self.blood_glucose_mg_dl, self.consciousness)
        if (self.bp_systolic is None) != (self.bp_diastolic is None):
            raise ValueError("Blood pressure needs both systolic and diastolic values.")
        if self.kind == "vitals" and all(v is None for v in vitals):
            raise ValueError("Enter at least one vital sign.")
        if self.kind != "vitals" and (self.volume_ml is None or self.io_route is None):
            raise ValueError("Intake/output needs a volume and a route.")
        return self


class CorrectionIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)
    replacement: Optional[dict] = None


class NoteIn(_In):
    note_type: Literal["nursing", "progress", "handover", "procedure", "plan"]
    body: str = Field(..., min_length=1, max_length=8000)


class AmendNoteIn(_In):
    body: str = Field(..., min_length=1, max_length=8000)
    reason: str = Field(..., min_length=3, max_length=300)


class OrderIn(_In):
    order_type: Literal["medication", "investigation", "procedure", "diet", "nursing", "other"]
    description: str = Field(..., min_length=1, max_length=300)
    drug_name: Optional[str] = Field(None, max_length=120)
    dose: Optional[str] = Field(None, max_length=60)
    route: Optional[Literal["oral", "iv", "im", "sc", "topical", "inhalation", "pr", "sl", "other"]] = None
    frequency: Optional[str] = Field(None, max_length=60)
    is_prn: bool = False
    instructions: Optional[str] = Field(None, max_length=500)
    lab_test_id: Optional[str] = None
    priority: Literal["routine", "urgent", "stat"] = "routine"
    idempotency_key: str = Field(..., min_length=8, max_length=80)

    @model_validator(mode="after")
    def _medication(self):
        if self.order_type == "medication" and not all((self.drug_name, self.dose, self.route, self.frequency)):
            raise ValueError("A medication order needs drug, dose, route and frequency.")
        return self


class OrderEndIn(_In):
    status: Literal["completed", "discontinued"]
    reason: Optional[str] = Field(None, max_length=300)


class AdministrationIn(_In):
    order_id: str
    outcome: Literal["given", "omitted", "refused", "delayed", "held"]
    administered_at: datetime
    dose_given: Optional[str] = Field(None, max_length=60)
    reason: Optional[str] = Field(None, max_length=300)
    idempotency_key: str = Field(..., min_length=8, max_length=80)

    @model_validator(mode="after")
    def _reason(self):
        if self.outcome != "given" and len((self.reason or "").strip()) < 3:
            raise ValueError("A reason is required when a dose is not given.")
        return self


class SummaryIn(_In):
    final_diagnosis: Optional[str] = Field(None, max_length=2000)
    presenting_complaints: Optional[str] = Field(None, max_length=4000)
    course_in_hospital: Optional[str] = Field(None, max_length=8000)
    procedures_performed: Optional[str] = Field(None, max_length=4000)
    condition_at_discharge: Optional[str] = Field(None, max_length=2000)
    discharge_medications: Optional[str] = Field(None, max_length=4000)
    follow_up: Optional[str] = Field(None, max_length=2000)
    advice: Optional[str] = Field(None, max_length=4000)
    amend_reason: Optional[str] = Field(None, min_length=3, max_length=300)
    expected_updated_at: Optional[str] = None


class ChargeIn(_In):
    category: Literal["bed", "nursing", "doctor_visit", "procedure", "investigation", "pharmacy", "consumable",
                      "package", "other"]
    description: str = Field(..., min_length=1, max_length=200)
    service_date: date
    quantity: int = Field(1, ge=1, le=999)
    unit_price_paise: int = Field(..., ge=0, le=100_000_000)
    source_ref: str = Field(..., min_length=8, max_length=120)   # client-generated: a retry never double-charges


class PostChargesIn(_In):
    charge_ids: list[str] = Field(..., min_length=1, max_length=500)


# ── guards ──────────────────────────────────────────────────────────────────

def _holds(user: AdminUser, *perms: str) -> bool:
    return user.role in ("clinic_admin", "super_admin") or any(p in (user.permissions or []) for p in perms)


def _clinical(user: AdminUser) -> bool:
    return _holds(user, "IPD_CLINICAL", "IPD_NURSING")


def _require_doctor(user: AdminUser, what: str) -> None:
    """Clinical decisions are made by an identified clinician, never by role alone."""
    if not getattr(user, "doctor_id", None):
        raise HTTPException(status_code=403, detail=f"Only a login linked to a doctor can {what}.")


async def _ipd_scope(user: AdminUser, clinic_id: str, *perms: str, new_work: bool = False) -> tuple[str, dict]:
    """Permission, tenant, entitlement. new_work=True also refuses in WIND_DOWN."""
    if perms and not _holds(user, *perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    access = ipd_access(clinic)
    if access is None:
        raise HTTPException(status_code=403, detail="The IPD module is not enabled for this clinic.")
    if new_work and access == "WIND_DOWN":
        raise HTTPException(status_code=409, detail="IPD is being switched off for this clinic: new admissions "
                                                    "and configuration are closed. Existing stays continue.")
    return scope, clinic


def _read_branch(user: AdminUser, requested: Optional[str]) -> Optional[str]:
    if requested:
        enforce_branch_scope(user, requested)
    return str(user.branch_id) if getattr(user, "branch_id", None) else requested


async def _admission(user: AdminUser, scope: str, admission_id: str) -> dict:
    a = await ipd.get_admission_row(scope, admission_id)
    enforce_branch_scope(user, a.get("branch_id"))
    return a


async def _ward_in_branch(user: AdminUser, scope: str, ward_id: Optional[str] = None,
                          bed_id: Optional[str] = None) -> None:
    """Branch-pinned staff may change only their own branch's wards and beds, and place
    patients only in their own branch's beds (404 if absent)."""
    if bed_id:
        bed = await sb(scoped_query("ipd_beds", scope, "id, ward_id").eq("id", bed_id).limit(1))
        if not bed.data:
            raise HTTPException(status_code=404, detail="Bed not found.")
        ward_id = bed.data[0]["ward_id"]
    ward = await sb(scoped_query("ipd_wards", scope, "id, branch_id").eq("id", ward_id).limit(1))
    if not ward.data:
        raise HTTPException(status_code=404, detail="Ward not found.")
    enforce_branch_scope(user, ward.data[0].get("branch_id"))


async def _child(user: AdminUser, scope: str, table: str, row_id: str, label: str) -> dict:
    res = await sb(scoped_query(table, scope).eq("id", row_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    await _admission(user, scope, res.data[0]["admission_id"])
    return res.data[0]


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── setup: settings, wards, beds ────────────────────────────────────────────

@router.get("/setup")
async def get_setup(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _ipd_scope(user, clinic_id, *ANY_IPD)
    return {"access": ipd_access(clinic), "settings": {**ipd.DEFAULT_SETTINGS, **(clinic.get("ipd_settings") or {})},
            "wards": await ipd.list_wards(scope)}


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _ipd_scope(user, clinic_id, "IPD_ADMIN")
    changes = body.model_dump(exclude_none=True)
    merged = {**ipd.DEFAULT_SETTINGS, **(clinic.get("ipd_settings") or {}), **changes}
    # unscoped: this clinic's own row by primary key (scope from enforce_clinic_access)
    await sb(supabase.table("clinics").update({"ipd_settings": merged}).eq("id", scope))
    invalidate_tenant_cache()
    await _audit(user, request, "ipd_settings_update", "clinic", scope, {"keys": sorted(changes)})
    return {"settings": merged}


@router.post("/wards", status_code=status.HTTP_201_CREATED)
async def create_ward(body: WardIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMIN", new_work=True)
    if body.branch_id:
        await resolve_owned_branch(user, body.branch_id, scope)
    elif getattr(user, "branch_id", None):
        body.branch_id = str(user.branch_id)   # a branch-pinned admin's ward is their branch's (like admissions)
    ward = await ipd.create_ward(scope, body.model_dump(exclude_none=True))
    await _audit(user, request, "ipd_ward_create", "ipd_ward", ward["id"], {"code": body.code})
    return ward


@router.patch("/wards/{ward_id}")
async def patch_ward(ward_id: str, body: WardPatch, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMIN")
    await _ward_in_branch(user, scope, ward_id=ward_id)
    patch = body.model_dump(exclude_none=True)
    if "branch_id" in body.model_fields_set:      # a move; the DB refuses it while a bed is occupied/reserved
        if body.branch_id:
            await resolve_owned_branch(user, body.branch_id, scope)
        elif getattr(user, "branch_id", None):
            raise HTTPException(status_code=403, detail="Only a hospital-wide administrator can make a ward shared.")
        patch["branch_id"] = body.branch_id
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    ward = await ipd.update_ward(scope, ward_id, patch)
    await _audit(user, request, "ipd_ward_update", "ipd_ward", ward_id, patch)
    return ward


@router.post("/wards/{ward_id}/beds", status_code=status.HTTP_201_CREATED)
async def create_beds(ward_id: str, body: BedsIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMIN", new_work=True)
    await _ward_in_branch(user, scope, ward_id=ward_id)
    beds = await ipd.create_beds(scope, ward_id, [b.model_dump() for b in body.beds])
    await _audit(user, request, "ipd_beds_create", "ipd_ward", ward_id, {"count": len(beds)})
    return {"beds": beds}


@router.patch("/beds/{bed_id}")
async def patch_bed(bed_id: str, body: BedPatch, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMIN")
    await _ward_in_branch(user, scope, bed_id=bed_id)
    bed = await ipd.update_bed(scope, bed_id, body.model_dump(exclude_none=True))
    await _audit(user, request, "ipd_bed_update", "ipd_bed", bed_id, body.model_dump(exclude_none=True))
    return bed


@router.post("/beds/{bed_id}/status")
async def set_bed_status(bed_id: str, body: BedStatusIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    # cleaning turnover is housekeeping; taking a bed out of service is an admin decision
    perms = ("IPD_ADMIN",) if body.status in ("maintenance", "blocked") else ("IPD_ADMIN", "IPD_NURSING", "IPD_ADMISSIONS")
    scope, _ = await _ipd_scope(user, clinic_id, *perms)
    await _ward_in_branch(user, scope, bed_id=bed_id)
    bed = await ipd.set_bed_status(scope, bed_id, body.status, body.reason, user)
    await _audit(user, request, "ipd_bed_status", "ipd_bed", bed_id, {"status": body.status})
    return bed


@router.post("/beds/{bed_id}/release-reservation")
async def release_reservation(bed_id: str, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS")
    await _ward_in_branch(user, scope, bed_id=bed_id)
    bed = await ipd.release_reservation(scope, bed_id, user)
    await _audit(user, request, "ipd_reservation_release", "ipd_bed", bed_id)
    return bed


# ── operational views ───────────────────────────────────────────────────────

@router.get("/board")
async def get_board(branch_id: Optional[str] = None, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    return await ipd.bed_board(scope, _read_branch(user, branch_id), clinical=_clinical(user))


@router.get("/census")
async def get_census(branch_id: Optional[str] = None, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    return await ipd.census(scope, _read_branch(user, branch_id))


@router.get("/worklist")
async def get_worklist(branch_id: Optional[str] = None, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    out = await ipd.worklist(scope, _read_branch(user, branch_id))
    if not _clinical(user):
        out.pop("active_orders", None)
    return out


# ── admissions ──────────────────────────────────────────────────────────────

@router.get("/admissions")
async def list_admissions(status_filter: Optional[str] = Query(None, alias="status"), q: Optional[str] = None,
                          branch_id: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
                          offset: int = Query(0, ge=0, le=100_000), clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    statuses = [s for s in (status_filter or "").split(",") if s] or None
    if statuses and not set(statuses) <= {"pending", "admitted", "discharge_pending", "discharged", "cancelled"}:
        raise HTTPException(status_code=422, detail="Unknown status filter.")
    return await ipd.list_admissions(scope, statuses, q, _read_branch(user, branch_id), limit, offset)


@router.post("/admissions", status_code=status.HTTP_201_CREATED)
async def create_admission(body: AdmissionIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS", new_work=True)
    branch = str(user.branch_id) if getattr(user, "branch_id", None) else body.branch_id
    if branch:
        await resolve_owned_branch(user, branch, scope)
    adm = await ipd.admit(scope, ipd.admission_payload(body.model_dump(exclude={"branch_id"}), branch),
                          body.bed_id, user)
    if not adm.get("replayed"):
        await _audit(user, request, "ipd_admit", "ipd_admission", adm["id"],
                     {"bed": bool(body.bed_id), "source": body.source, "priority": body.priority})
    return adm


@router.get("/admissions/{admission_id}")
async def get_admission(admission_id: str, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    await _admission(user, scope, admission_id)
    clinical = _clinical(user)
    out = await ipd.admission_detail(scope, admission_id, clinical=clinical,
                                     billing=_holds(user, "IPD_BILLING", "IPD_ADMISSIONS", "IPD_ADMIN"))
    if clinical:   # clinical reads are audited (DPDP / NABH access trail)
        await _audit(user, request, "ipd_chart_view", "ipd_admission", admission_id)
    return out


@router.patch("/admissions/{admission_id}")
async def patch_admission(admission_id: str, body: AdmissionPatch, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS", "IPD_CLINICAL")
    await _admission(user, scope, admission_id)
    patch = body.model_dump(exclude_none=True, mode="json")
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    adm = await ipd.update_admission(scope, admission_id, patch)
    await _audit(user, request, "ipd_admission_update", "ipd_admission", admission_id, {"fields": sorted(patch)})
    return adm


@router.post("/admissions/{admission_id}/assign-bed")
async def assign_bed(admission_id: str, body: BedChoiceIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS")
    await _admission(user, scope, admission_id)
    await _ward_in_branch(user, scope, bed_id=body.bed_id)
    adm = await ipd.assign_bed(scope, admission_id, body.bed_id, user)
    await _audit(user, request, "ipd_assign_bed", "ipd_admission", admission_id, {"bed_id": body.bed_id})
    return adm


@router.post("/admissions/{admission_id}/reserve")
async def reserve_bed(admission_id: str, body: ReserveIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS")
    await _admission(user, scope, admission_id)
    await _ward_in_branch(user, scope, bed_id=body.bed_id)
    minutes = body.minutes or int((clinic.get("ipd_settings") or {}).get("reservation_minutes")
                                  or ipd.DEFAULT_SETTINGS["reservation_minutes"])
    bed = await ipd.reserve_bed(scope, admission_id, body.bed_id, minutes, user)
    await _audit(user, request, "ipd_reserve_bed", "ipd_admission", admission_id, {"bed_id": body.bed_id})
    return bed


@router.post("/admissions/{admission_id}/transfer")
async def transfer(admission_id: str, body: TransferIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS", "IPD_NURSING")
    await _admission(user, scope, admission_id)
    await _ward_in_branch(user, scope, bed_id=body.to_bed_id)
    adm = await ipd.transfer(scope, admission_id, body.to_bed_id, body.expected_bed_id, body.reason, user)
    await _audit(user, request, "ipd_transfer", "ipd_admission", admission_id, {"to_bed_id": body.to_bed_id})
    return adm


@router.post("/admissions/{admission_id}/cancel")
async def cancel_admission(admission_id: str, body: ReasonIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS")
    await _admission(user, scope, admission_id)
    adm = await ipd.cancel_admission(scope, admission_id, body.reason, user)
    await _audit(user, request, "ipd_cancel", "ipd_admission", admission_id)
    return adm


# ── discharge ───────────────────────────────────────────────────────────────

@router.post("/admissions/{admission_id}/discharge/initiate")
async def initiate_discharge(admission_id: str, body: DischargeInitIn, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    # LAMA / DAMA / absconding are recorded by the desk; every other discharge is a doctor's decision
    administrative = body.discharge_type in ("lama", "dama", "absconded")
    perms = ("IPD_CLINICAL", "IPD_ADMISSIONS") if administrative else ("IPD_CLINICAL",)
    scope, _ = await _ipd_scope(user, clinic_id, *perms)
    if not administrative:
        _require_doctor(user, "decide a discharge")
    await _admission(user, scope, admission_id)
    adm = await ipd.initiate_discharge(scope, admission_id, body.discharge_type, user)
    await _audit(user, request, "ipd_discharge_initiate", "ipd_admission", admission_id,
                 {"discharge_type": body.discharge_type})
    return adm


@router.post("/admissions/{admission_id}/discharge/revert")
async def revert_discharge(admission_id: str, body: ReasonIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_CLINICAL", "IPD_ADMISSIONS")
    await _admission(user, scope, admission_id)
    adm = await ipd.revert_discharge(scope, admission_id, body.reason, user)
    await _audit(user, request, "ipd_discharge_revert", "ipd_admission", admission_id)
    return adm


@router.get("/admissions/{admission_id}/discharge/checklist")
async def discharge_checklist(admission_id: str, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, *ANY_IPD)
    await _admission(user, scope, admission_id)
    return await ipd.discharge_checklist(scope, admission_id)


@router.post("/admissions/{admission_id}/discharge/complete")
async def complete_discharge(admission_id: str, body: DischargeCompleteIn, request: Request,
                             clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_ADMISSIONS", "IPD_BILLING")
    if body.waiver_reason and not _holds(user, "IPD_ADMIN"):
        raise HTTPException(status_code=403, detail="Only an IPD administrator can waive settlement.")
    await _admission(user, scope, admission_id)
    adm = await ipd.complete_discharge(scope, admission_id, body.waiver_reason, user)
    if not adm.get("replayed"):
        await _audit(user, request, "ipd_discharge_complete", "ipd_admission", admission_id,
                     {"waiver": bool(body.waiver_reason)})
    return adm


# ── nursing + clinical records ──────────────────────────────────────────────

@router.post("/admissions/{admission_id}/observations", status_code=status.HTTP_201_CREATED)
async def add_observation(admission_id: str, body: ObservationIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_NURSING", "IPD_CLINICAL")
    await _admission(user, scope, admission_id)
    obs = await ipd.add_observation(scope, admission_id, body.model_dump(exclude_none=True, mode="json"), user)
    await _audit(user, request, "ipd_observation_add", "ipd_admission", admission_id, {"kind": body.kind})
    return obs


@router.post("/observations/{observation_id}/correct")
async def correct_observation(observation_id: str, body: CorrectionIn, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_NURSING", "IPD_CLINICAL")
    row = await _child(user, scope, "ipd_observations", observation_id, "Observation")
    replacement = None
    if body.replacement is not None:   # same validation as a new entry; the kind never changes
        try:
            replacement = ObservationIn(**{"observed_at": row["observed_at"], **body.replacement,
                                           "kind": row["kind"]}).model_dump(exclude_none=True, mode="json")
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=[
                {"loc": err["loc"], "msg": err["msg"]} for err in e.errors()])
    out = await ipd.correct_observation(scope, observation_id, body.reason, replacement, user)
    await _audit(user, request, "ipd_observation_correct", "ipd_admission", row["admission_id"])
    return out


@router.post("/admissions/{admission_id}/notes", status_code=status.HTTP_201_CREATED)
async def add_note(admission_id: str, body: NoteIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    doctor_note = body.note_type in ("progress", "procedure", "plan")
    perms = ("IPD_CLINICAL",) if doctor_note else ("IPD_NURSING", "IPD_CLINICAL")
    scope, _ = await _ipd_scope(user, clinic_id, *perms)
    if doctor_note:
        _require_doctor(user, "write a clinician note")
    await _admission(user, scope, admission_id)
    note = await ipd.add_note(scope, admission_id, body.note_type, body.body, user)
    await _audit(user, request, "ipd_note_add", "ipd_admission", admission_id, {"note_type": body.note_type})
    return note


@router.post("/notes/{note_id}/amend")
async def amend_note(note_id: str, body: AmendNoteIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_NURSING", "IPD_CLINICAL")
    row = await _child(user, scope, "ipd_notes", note_id, "Note")
    note = await ipd.amend_note(scope, note_id, body.body, body.reason, user)
    await _audit(user, request, "ipd_note_amend", "ipd_admission", row["admission_id"])
    return note


@router.post("/admissions/{admission_id}/orders", status_code=status.HTTP_201_CREATED)
async def add_order(admission_id: str, body: OrderIn, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_CLINICAL")
    _require_doctor(user, "write an order")
    await _admission(user, scope, admission_id)
    order = await ipd.add_order(scope, admission_id, body.model_dump(exclude_none=True), user)
    if not order.get("replayed"):
        await _audit(user, request, "ipd_order_add", "ipd_admission", admission_id, {"order_type": body.order_type})
    return order


@router.post("/orders/{order_id}/end")
async def end_order(order_id: str, body: OrderEndIn, request: Request, clinic_id: str = "default",
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_CLINICAL", "IPD_NURSING")
    row = await _child(user, scope, "ipd_orders", order_id, "Order")
    # stopping treatment is a clinician's decision; a nurse may close a finished non-drug task
    if body.status == "discontinued" or row["order_type"] == "medication":
        if not _holds(user, "IPD_CLINICAL"):
            raise HTTPException(status_code=403, detail="Missing permission: IPD_CLINICAL")
        _require_doctor(user, "stop an order")
    if body.status == "discontinued" and len((body.reason or "").strip()) < 3:
        raise HTTPException(status_code=422, detail="A reason is required to discontinue an order.")
    order = await ipd.end_order(scope, order_id, body.status, body.reason, user)
    await _audit(user, request, "ipd_order_end", "ipd_admission", row["admission_id"], {"status": body.status})
    return order


@router.post("/admissions/{admission_id}/administrations", status_code=status.HTTP_201_CREATED)
async def record_administration(admission_id: str, body: AdministrationIn, request: Request,
                                clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_NURSING", "IPD_CLINICAL")
    await _admission(user, scope, admission_id)
    mar = await ipd.record_administration(scope, admission_id, body.model_dump(exclude_none=True, mode="json"), user)
    if not mar.get("replayed"):
        await _audit(user, request, "ipd_mar_record", "ipd_admission", admission_id, {"outcome": body.outcome})
    return mar


@router.post("/administrations/{mar_id}/error")
async def mark_administration_error(mar_id: str, body: ReasonIn, request: Request, clinic_id: str = "default",
                                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_NURSING", "IPD_CLINICAL")
    row = await _child(user, scope, "ipd_med_administrations", mar_id, "Entry")
    out = await ipd.mark_administration_error(scope, mar_id, body.reason, user)
    await _audit(user, request, "ipd_mar_error", "ipd_admission", row["admission_id"])
    return out


@router.put("/admissions/{admission_id}/discharge-summary")
async def save_discharge_summary(admission_id: str, body: SummaryIn, request: Request, clinic_id: str = "default",
                                 user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_CLINICAL")
    await _admission(user, scope, admission_id)
    fields = body.model_dump(exclude_none=True, exclude={"expected_updated_at"})
    out = await ipd.save_summary_draft(scope, admission_id, fields, user, body.expected_updated_at)
    await _audit(user, request, "ipd_summary_save", "ipd_admission", admission_id, {"version": out.get("version")})
    return out


@router.post("/discharge-summaries/{summary_id}/sign")
async def sign_discharge_summary(summary_id: str, request: Request, clinic_id: str = "default",
                                 user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_CLINICAL")
    _require_doctor(user, "sign a discharge summary")
    row = await _child(user, scope, "ipd_discharge_summaries", summary_id, "Discharge summary")
    out = await ipd.sign_summary(scope, summary_id, user)
    await _audit(user, request, "ipd_summary_sign", "ipd_admission", row["admission_id"],
                 {"version": out.get("version")})
    return out


# ── charges + final bill ────────────────────────────────────────────────────

@router.post("/admissions/{admission_id}/charges", status_code=status.HTTP_201_CREATED)
async def add_charge(admission_id: str, body: ChargeIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_BILLING")
    await _admission(user, scope, admission_id)
    ch = await ipd.add_charge(scope, admission_id, body.model_dump(mode="json"), user)
    if not ch.get("replayed"):
        await _audit(user, request, "ipd_charge_add", "ipd_admission", admission_id,
                     {"category": body.category, "total_paise": body.quantity * body.unit_price_paise})
    return ch


@router.post("/admissions/{admission_id}/charges/accrue-bed")
async def accrue_bed_charges(admission_id: str, request: Request, clinic_id: str = "default",
                             user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_BILLING")
    await _admission(user, scope, admission_id)
    out = await ipd.accrue_bed_charges(scope, admission_id, user)
    await _audit(user, request, "ipd_charge_accrue", "ipd_admission", admission_id, {"added": out.get("added")})
    return out


@router.post("/admissions/{admission_id}/charges/post")
async def post_charges(admission_id: str, body: PostChargesIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_BILLING")
    await _admission(user, scope, admission_id)
    out = await ipd.post_charges(scope, admission_id, body.charge_ids, user)
    await _audit(user, request, "ipd_charge_post", "ipd_admission", admission_id, {"posted": out.get("posted")})
    return out


@router.post("/charges/{charge_id}/void")
async def void_charge(charge_id: str, body: ReasonIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_BILLING")
    row = await _child(user, scope, "ipd_charges", charge_id, "Charge")
    out = await ipd.void_charge(scope, charge_id, body.reason, user)
    await _audit(user, request, "ipd_charge_void", "ipd_admission", row["admission_id"], {"charge_id": charge_id})
    return out


@router.post("/admissions/{admission_id}/final-bill")
async def generate_final_bill(admission_id: str, request: Request, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _ipd_scope(user, clinic_id, "IPD_BILLING")
    await _admission(user, scope, admission_id)
    out = await ipd.generate_final_bill(scope, admission_id, user)
    if not out.get("replayed"):
        await _audit(user, request, "ipd_final_bill", "ipd_admission", admission_id,
                     {"invoice_id": out.get("invoice_id")})
    return out
