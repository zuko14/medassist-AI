"""Emergency / Casualty API (G7, migration 121).

POST  /admin/emergency/register                       30-second registration (unknown patient in one click)
GET   /admin/emergency/visits                         live casualty board, RED first
GET   /admin/emergency/visits/{id}                    one visit with patient and MLC
PATCH /admin/emergency/visits/{id}                    re-triage, doctor, notes, vitals
POST  /admin/emergency/visits/{id}/mlc                record / correct / lock the medico-legal case
GET   /admin/emergency/visits/{id}/mlc/pdf            police intimation PDF
POST  /admin/emergency/visits/{id}/disposition        IPD / transfer / discharge / LAMA / death
GET   /admin/emergency/patients/search                registered patients, for identify & merge
POST  /admin/emergency/patients/merge                 atomic identity merge

Permissions: EMERGENCY_READ / EMERGENCY_WRITE / MLC_MANAGE (holds_emergency_permission).
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services import emergency
from app.services.opd_pdf import render_mlc_intimation_pdf
from app.services.permissions import holds_emergency_permission, resolve_owned_branch
from app.services.tenant import get_clinic_by_id, ipd_access, opd_enabled

router = APIRouter(prefix="/admin/emergency", tags=["Emergency"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_PHONE = r"^\+?[0-9]{10,15}$"
Triage = Literal["red", "yellow", "green", "black"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VitalsIn(_In):
    """Typo guards (same bounds as OPD / IPD), not clinical thresholds."""
    bp_sys: Optional[int] = Field(None, ge=30, le=300)
    bp_dia: Optional[int] = Field(None, ge=10, le=200)
    pulse: Optional[int] = Field(None, ge=0, le=250)
    spo2: Optional[int] = Field(None, ge=30, le=100)
    temp: Optional[float] = Field(None, ge=25, le=45)
    rr: Optional[int] = Field(None, ge=0, le=80)
    gcs: Optional[int] = Field(None, ge=3, le=15)
    grbs: Optional[int] = Field(None, ge=10, le=1500)

    def stamped(self) -> Optional[dict]:
        v = self.model_dump(exclude_none=True)
        return {**v, "recorded_at": datetime.now(timezone.utc).isoformat()} if v else None


class RegisterIn(_In):
    is_unknown: bool = False
    estimated_gender: Optional[Literal["male", "female", "other"]] = None
    estimated_age: Optional[int] = Field(None, ge=0, le=130)
    patient_id: Optional[str] = Field(None, pattern=_UUID)
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    phone: Optional[str] = Field(None, pattern=_PHONE)
    gender: Optional[Literal["male", "female", "other"]] = None
    age: Optional[int] = Field(None, ge=0, le=130)
    triage_category: Triage
    arrival_mode: Literal["ambulance", "walk_in", "police", "transfer", "other"] = "walk_in"
    brought_by_name: Optional[str] = Field(None, max_length=120)
    brought_by_phone: Optional[str] = Field(None, pattern=_PHONE)
    chief_complaints: str = Field(..., min_length=2, max_length=2000)
    initial_vitals: Optional[VitalsIn] = None
    attending_doctor_id: Optional[str] = Field(None, pattern=_UUID)
    branch_id: Optional[str] = Field(None, pattern=_UUID)
    idempotency_key: str = Field(..., pattern=r"^[A-Za-z0-9_-]{8,72}$")

    @model_validator(mode="after")
    def _who(self):
        if not self.is_unknown and not self.patient_id and not (self.name and self.phone):
            raise ValueError("Register as unknown, pick a patient, or give name and phone.")
        if not self.chief_complaints.strip():
            raise ValueError("Chief complaints are required.")
        return self


class VisitPatch(_In):
    triage_category: Optional[Triage] = None
    attending_doctor_id: Optional[str] = Field(None, pattern=_UUID)
    clinical_notes: Optional[str] = Field(None, max_length=8000)
    provisional_diagnosis: Optional[str] = Field(None, max_length=500)
    vitals: Optional[VitalsIn] = None


class MlcIn(_In):
    police_station_name: str = Field(..., min_length=2, max_length=150)
    police_intimation_time: Optional[datetime] = None
    officer_name: Optional[str] = Field(None, max_length=120)
    officer_badge: Optional[str] = Field(None, max_length=60)
    incident_place: Optional[str] = Field(None, max_length=250)
    incident_time: Optional[datetime] = None
    injury_type: Literal["rto", "physical_assault", "poisoning", "burns", "fall_from_height", "workplace",
                         "gunshot_stab", "hanging", "other"]
    injury_description: str = Field(..., min_length=5, max_length=4000)
    is_dying_declaration: bool = False
    lock: bool = False


class IpdAdmitIn(_In):
    admitting_doctor_id: str = Field(..., pattern=_UUID)
    attending_doctor_id: Optional[str] = Field(None, pattern=_UUID)
    bed_id: Optional[str] = Field(None, pattern=_UUID)
    provisional_diagnosis: Optional[str] = Field(None, max_length=500)
    payer_type: Literal["self", "insurance", "corporate", "government_scheme"] = "self"
    payer_name: Optional[str] = Field(None, max_length=120)
    consent_recorded: bool = False


class DispositionIn(_In):
    action: Literal["admitted_ipd", "transferred", "discharged", "lama", "deceased"]
    notes: Optional[str] = Field(None, max_length=1000)
    transfer_to: Optional[str] = Field(None, max_length=200)
    time_of_death: Optional[datetime] = None
    consent_id: Optional[str] = Field(None, pattern=_UUID)
    ipd: Optional[IpdAdmitIn] = None

    @model_validator(mode="after")
    def _shape(self):
        if self.action == "admitted_ipd" and not self.ipd:
            raise ValueError("Admitting to IPD needs the admitting doctor (ipd.admitting_doctor_id).")
        if self.action == "transferred" and not (self.transfer_to or "").strip():
            raise ValueError("Name the hospital the patient is transferred to.")
        return self


class MergeIn(_In):
    temp_patient_id: str = Field(..., pattern=_UUID)
    target_patient_id: str = Field(..., pattern=_UUID)
    reason: Optional[str] = Field(None, max_length=300)


# ── guards ──────────────────────────────────────────────────────────────────

async def _scope(user: AdminUser, clinic_id: str, perm: str) -> tuple[str, dict]:
    if not holds_emergency_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or not (opd_enabled(clinic) or ipd_access(clinic)):
        raise HTTPException(status_code=403, detail="The emergency module needs the OPD or IPD module.")
    return scope, clinic


async def _visit(user: AdminUser, scope: str, visit_id: str) -> dict:
    v = await emergency.get_visit(scope, visit_id)
    enforce_branch_scope(user, v.get("branch_id"))
    return v


def _pinned(user: AdminUser) -> Optional[str]:
    return str(user.branch_id) if getattr(user, "branch_id", None) and user.role not in (
        "super_admin", "clinic_admin") else None


async def _audit(user: AdminUser, request: Request, action: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type="emergency_visit", resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── routes ──────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_WRITE")
    data = body.model_dump(exclude={"initial_vitals"})
    data["initial_vitals"] = body.initial_vitals.stamped() if body.initial_vitals else None
    if data.get("branch_id"):
        await resolve_owned_branch(user, data["branch_id"], scope)
    else:
        data["branch_id"] = _pinned(user)
    visit = await emergency.rapid_register_emergency(scope, data, user)
    if not visit.get("replayed"):
        await _audit(user, request, "EMERGENCY_REGISTER", visit["id"],
                     {"number": visit["visit_number"], "triage": visit["triage_category"],
                      "unknown": visit["is_unknown_patient"]})
    return visit


@router.get("/visits")
async def board(triage: Optional[Triage] = None, include_closed: bool = False, branch_id: Optional[str] = None,
                clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_READ")
    if branch_id:
        enforce_branch_scope(user, branch_id)
    return await emergency.casualty_board(scope, _pinned(user) or branch_id, triage, include_closed)


@router.get("/visits/{visit_id}")
async def visit_detail(visit_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_READ")
    v = await _visit(user, scope, visit_id)
    board = await emergency.casualty_board(scope)   # names / MLC enrichment; small open set
    row = next((x for x in board["visits"] if x["id"] == v["id"]), None)
    return row or v


@router.patch("/visits/{visit_id}")
async def update_visit(visit_id: str, body: VisitPatch, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_WRITE")
    await _visit(user, scope, visit_id)
    data = body.model_dump(exclude_unset=True, exclude={"vitals"})
    if body.vitals:
        data["latest_vitals"] = body.vitals.stamped()
    if not data:
        raise HTTPException(status_code=422, detail="Nothing to update.")
    row = await emergency.update_visit(scope, visit_id, data)
    await _audit(user, request, "EMERGENCY_UPDATE", visit_id, {"fields": sorted(data)})
    return row


@router.post("/visits/{visit_id}/mlc")
async def record_mlc(visit_id: str, body: MlcIn, request: Request, clinic_id: str = "default",
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "MLC_MANAGE")
    await _visit(user, scope, visit_id)
    data = body.model_dump(mode="json", exclude={"lock"})
    mlc = await emergency.record_mlc(scope, visit_id, data, user, lock=body.lock)
    await _audit(user, request, "EMERGENCY_MLC_LOCK" if body.lock else "EMERGENCY_MLC_RECORD", visit_id,
                 {"mlc_number": mlc["mlc_number"], "police_station": body.police_station_name,
                  "injury_type": body.injury_type})
    return mlc


@router.get("/visits/{visit_id}/mlc")
async def get_mlc(visit_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_READ")
    await _visit(user, scope, visit_id)
    return await emergency.get_mlc(scope, visit_id)


@router.get("/visits/{visit_id}/mlc/pdf")
async def mlc_pdf(visit_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_READ")
    await _visit(user, scope, visit_id)
    mlc = await emergency.get_mlc(scope, visit_id)
    return Response(content=render_mlc_intimation_pdf(mlc), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{mlc["mlc_number"]}.pdf"',
                             "Cache-Control": "no-store"})


@router.post("/visits/{visit_id}/disposition")
async def disposition(visit_id: str, body: DispositionIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "EMERGENCY_WRITE")
    await _visit(user, scope, visit_id)
    if body.action == "admitted_ipd" and ipd_access(clinic) != "ACTIVE":
        raise HTTPException(status_code=409, detail="IPD admissions are not open for this clinic.")
    details = body.model_dump(mode="json", exclude_none=True, exclude={"action"})
    out = await emergency.disposition_emergency(scope, visit_id, body.action, details, user)
    if not out.get("replayed"):
        await _audit(user, request, "EMERGENCY_DISPOSITION", visit_id,
                     {"action": body.action, "admission_id": out.get("ipd_admission_id")})
    return out


@router.get("/patients/search")
async def patient_search(q: str = Query(..., min_length=2, max_length=60), clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_WRITE")
    return {"patients": await emergency.search_patients(scope, q)}


@router.post("/patients/merge")
async def merge(body: MergeIn, request: Request, clinic_id: str = "default",
                user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "EMERGENCY_WRITE")
    out = await emergency.merge_unknown_patient(scope, body.temp_patient_id, body.target_patient_id, user, body.reason)
    if not out.get("replayed"):
        await log_admin_action(user=user, action="EMERGENCY_PATIENT_MERGE", resource_type="patient",
                               resource_id=body.target_patient_id,
                               details={"from": body.temp_patient_id, "moved": out.get("moved")},
                               ip_address=request.client.host if request.client else None)
    return out
