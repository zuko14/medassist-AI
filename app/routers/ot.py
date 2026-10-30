"""Operation Theatre API (G8, migration 122).

GET   /admin/ot/theatres                               theatres (any OT grant)
POST  /admin/ot/theatres                               add a theatre (IPD_ADMIN)
PATCH /admin/ot/theatres/{id}                          rename / retype / switch off (IPD_ADMIN)
GET   /admin/ot/options                                admitted patients, theatres, doctors, procedures for the form
POST  /admin/ot/surgeries                              book a surgery (409 when the theatre or patient is busy)
GET   /admin/ot/schedule                               the day's theatre list (branch-pinned staff: their branch)
GET   /admin/ot/surgeries/{id}                         one surgery with checklist, anaesthesia and charge
POST  /admin/ot/surgeries/{id}/who-checklist/{phase}   sign_in / time_out / sign_out, in that order
POST  /admin/ot/surgeries/{id}/anaesthesia             save / sign the anaesthesia record
POST  /admin/ot/surgeries/{id}/status                  start / recovery / cancel / postpone
POST  /admin/ot/surgeries/{id}/complete                post-op note + the one auto-posted charge

Module gate: the IPD module (ipd_access); new bookings are refused in WIND_DOWN.
Permissions: OT_SCHEDULE / OT_CHECKLIST / OT_CLINICAL (holds_ot_permission).
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.database import sb, scoped_query
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services import ipd, ot
from app.services.permissions import OT_PERMISSIONS, holds_ot_permission, resolve_owned_branch
from app.services.tenant import get_clinic_by_id, ipd_access
from app.utils.helpers import IST, today_ist

router = APIRouter(prefix="/admin/ot", tags=["Operation Theatre"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
TheatreType = Literal["major", "minor", "labor_room", "cath_lab", "endoscopy"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TheatreIn(_In):
    name: str = Field(..., min_length=1, max_length=80)
    theatre_type: TheatreType = "major"
    branch_id: Optional[str] = Field(None, pattern=_UUID)
    is_active: bool = True


class TheatrePatch(_In):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    theatre_type: Optional[TheatreType] = None
    is_active: Optional[bool] = None


def _ist(ts: datetime) -> datetime:
    """A time without a zone is the hospital's (IST)."""
    return ts.replace(tzinfo=IST) if ts.tzinfo is None else ts


class ScheduleIn(_In):
    admission_id: str = Field(..., pattern=_UUID)
    theatre_id: str = Field(..., pattern=_UUID)
    procedure_name: str = Field(..., min_length=2, max_length=200)
    service_id: Optional[str] = Field(None, pattern=_UUID)
    scheduled_start: datetime
    scheduled_end: datetime
    primary_surgeon_id: str = Field(..., pattern=_UUID)
    assistant_surgeon_name: Optional[str] = Field(None, max_length=120)
    anaesthetist_id: Optional[str] = Field(None, pattern=_UUID)
    scrub_nurse_name: Optional[str] = Field(None, max_length=120)
    priority: Literal["elective", "urgent", "emergency"] = "elective"
    pre_op_diagnosis: str = Field(..., min_length=2, max_length=1000)
    idempotency_key: str = Field(..., pattern=r"^[A-Za-z0-9_-]{8,72}$")

    @model_validator(mode="after")
    def _times(self):
        self.scheduled_start, self.scheduled_end = _ist(self.scheduled_start), _ist(self.scheduled_end)
        if not self.scheduled_start < self.scheduled_end <= self.scheduled_start + timedelta(hours=24):
            raise ValueError("The surgery must end after it starts, within 24 hours.")
        if not self.procedure_name.strip() or not self.pre_op_diagnosis.strip():
            raise ValueError("Procedure and pre-op diagnosis are required.")
        return self


class ChecklistIn(_In):
    items: dict[str, bool] = Field(..., min_length=1, max_length=10)


class VitalEntry(_In):
    """Typo guards (same bounds as OPD / IPD / ER), not clinical thresholds."""
    time: str = Field(..., pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    bp_sys: Optional[int] = Field(None, ge=30, le=300)
    bp_dia: Optional[int] = Field(None, ge=10, le=200)
    pulse: Optional[int] = Field(None, ge=0, le=250)
    spo2: Optional[int] = Field(None, ge=30, le=100)
    etco2: Optional[int] = Field(None, ge=0, le=100)
    temp: Optional[float] = Field(None, ge=25, le=45)
    note: Optional[str] = Field(None, max_length=200)


class AnaesthesiaIn(_In):
    asa_status: Optional[Literal["I", "II", "III", "IV", "V", "VI", "IE", "IIE", "IIIE", "IVE", "VE"]] = None
    mallampati: Optional[Literal["I", "II", "III", "IV"]] = None
    technique: Optional[Literal["general", "spinal", "epidural", "nerve_block", "local", "sedation", "mac",
                                "combined"]] = None
    airway_device: Optional[str] = Field(None, max_length=100)
    induction_agents: Optional[str] = Field(None, max_length=500)
    maintenance_agents: Optional[str] = Field(None, max_length=500)
    blood_loss_ml: Optional[int] = Field(None, ge=0, le=50000)
    fluids_given_ml: Optional[int] = Field(None, ge=0, le=50000)
    urine_output_ml: Optional[int] = Field(None, ge=0, le=50000)
    vital_signs_log: Optional[list[VitalEntry]] = Field(None, max_length=500)
    extubation_notes: Optional[str] = Field(None, max_length=2000)
    recovery_aldrete_score: Optional[int] = Field(None, ge=0, le=10)
    anaesthetist_id: Optional[str] = Field(None, pattern=_UUID)
    sign: bool = False


class StatusIn(_In):
    action: Literal["start", "recovery", "cancel", "postpone"]
    reason: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _reason(self):
        if self.action in ("cancel", "postpone") and len((self.reason or "").strip()) < 3:
            raise ValueError("Give a reason (at least 3 characters).")
        return self


class CompleteIn(_In):
    procedure_notes: str = Field(..., min_length=3, max_length=8000)
    post_op_diagnosis: Optional[str] = Field(None, max_length=1000)
    surgical_findings: Optional[str] = Field(None, max_length=4000)
    implants_used: Optional[str] = Field(None, max_length=2000)
    specimens_to_lab: Optional[str] = Field(None, max_length=1000)
    post_op_instructions: Optional[str] = Field(None, max_length=4000)
    amount_paise: Optional[int] = Field(None, ge=0, le=100_000_000)   # None = the linked service's price


# ── guards ──────────────────────────────────────────────────────────────────

async def _scope(user: AdminUser, clinic_id: str, *perms: str, new_work: bool = False) -> tuple[str, dict]:
    """Permission (any of `perms`), tenant, IPD entitlement; new_work also refuses WIND_DOWN."""
    if not any(holds_ot_permission(user, p) for p in perms):
        named = OT_PERMISSIONS if perms == ("OT_ANY",) else perms
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(named)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    access = ipd_access(clinic)
    if access is None:
        raise HTTPException(status_code=403, detail="The operation theatre needs the IPD module.")
    if new_work and access == "WIND_DOWN":
        raise HTTPException(status_code=409, detail="IPD is being switched off for this clinic: no new surgeries "
                                                    "can be booked. Existing ones continue.")
    return scope, clinic


def _ipd_admin(user: AdminUser) -> None:
    if user.role not in ("super_admin", "clinic_admin") and "IPD_ADMIN" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="Missing permission: IPD_ADMIN")


async def _surgery(user: AdminUser, scope: str, surgery_id: str) -> dict:
    s = await ot.get_surgery(scope, surgery_id)
    enforce_branch_scope(user, s.get("branch_id"))
    return s


def _pinned(user: AdminUser) -> Optional[str]:
    return str(user.branch_id) if getattr(user, "branch_id", None) and user.role not in (
        "super_admin", "clinic_admin") else None


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── theatres & form options ─────────────────────────────────────────────────

@router.get("/theatres")
async def theatres(branch_id: Optional[str] = Query(None, pattern=_UUID), active_only: bool = False,
                   clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_ANY")
    if branch_id:
        enforce_branch_scope(user, branch_id)
    return {"theatres": await ot.list_theatres(scope, _pinned(user) or branch_id, active_only)}


@router.post("/theatres", status_code=201)
async def create_theatre(body: TheatreIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    _ipd_admin(user)
    scope, _ = await _scope(user, clinic_id, "OT_ANY", new_work=True)
    data = body.model_dump()
    data["name"] = data["name"].strip()
    if data.get("branch_id"):
        await resolve_owned_branch(user, data["branch_id"], scope)
    else:
        data["branch_id"] = _pinned(user)
    row = await ot.save_theatre(scope, data)
    await _audit(user, request, "OT_THEATRE_CREATE", "ot_theatre", row["id"], {"name": row["name"]})
    return row


@router.patch("/theatres/{theatre_id}")
async def update_theatre(theatre_id: str, body: TheatrePatch, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    _ipd_admin(user)
    scope, _ = await _scope(user, clinic_id, "OT_ANY")
    enforce_branch_scope(user, (await ot.get_theatre(scope, theatre_id)).get("branch_id"))
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        data["name"] = data["name"].strip()
    if not data:
        raise HTTPException(status_code=422, detail="Nothing to update.")
    row = await ot.save_theatre(scope, data, theatre_id)
    await _audit(user, request, "OT_THEATRE_UPDATE", "ot_theatre", theatre_id, {"fields": sorted(data)})
    return row


@router.get("/options")
async def form_options(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """What the booking form picks from: admitted patients, active theatres, doctors, procedures."""
    scope, _ = await _scope(user, clinic_id, "OT_SCHEDULE")
    branch = _pinned(user)
    admitted = await ipd.list_admissions(scope, status=["admitted"], branch_id=branch, limit=500)
    doctors = await sb(scoped_query("doctors", scope, "id, name, specialization").eq("is_active", True)
                       .order("name").limit(500))
    services = await sb(scoped_query("billable_services", scope, "id, service_code, name, standard_price_paise")
                        .eq("category", "procedure").eq("is_active", True).order("name").limit(500))
    return {"admissions": admitted["admissions"], "theatres": await ot.list_theatres(scope, branch, True),
            "doctors": doctors.data or [], "services": services.data or []}


# ── surgeries ───────────────────────────────────────────────────────────────

@router.post("/surgeries", status_code=201)
async def schedule(body: ScheduleIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_SCHEDULE", new_work=True)
    enforce_branch_scope(user, (await ipd.get_admission_row(scope, body.admission_id)).get("branch_id"))
    enforce_branch_scope(user, (await ot.get_theatre(scope, body.theatre_id)).get("branch_id"))
    s = await ot.schedule_surgery(scope, body.model_dump(mode="json"), user)
    if not s.get("replayed"):
        await _audit(user, request, "OT_SCHEDULE", "ot_surgery", s["id"],
                     {"number": s["surgery_number"], "admission_id": body.admission_id,
                      "theatre_id": body.theatre_id, "start": s["scheduled_start"]})
    return s


@router.get("/schedule")
async def day_schedule(day: Optional[date] = Query(None, alias="date"),
                       branch_id: Optional[str] = Query(None, pattern=_UUID),
                       clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_ANY")
    if branch_id:
        enforce_branch_scope(user, branch_id)
    return await ot.day_schedule(scope, day or today_ist(), _pinned(user) or branch_id)


@router.get("/surgeries/{surgery_id}")
async def surgery_detail(surgery_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_ANY")
    await _surgery(user, scope, surgery_id)
    return await ot.surgery_detail(scope, surgery_id)


@router.post("/surgeries/{surgery_id}/who-checklist/{phase}")
async def who_checklist(surgery_id: str, phase: Literal["sign_in", "time_out", "sign_out"], body: ChecklistIn,
                        request: Request, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_CHECKLIST")
    await _surgery(user, scope, surgery_id)
    c = await ot.update_who_checklist(scope, surgery_id, phase, body.items, user)
    if not c.get("replayed"):
        await _audit(user, request, f"OT_CHECKLIST_{phase.upper()}", "ot_surgery", surgery_id,
                     {"completed": bool(c.get(f"{phase}_completed"))})
    return c


@router.post("/surgeries/{surgery_id}/anaesthesia")
async def anaesthesia(surgery_id: str, body: AnaesthesiaIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_CLINICAL")
    await _surgery(user, scope, surgery_id)
    data = body.model_dump(mode="json", exclude_unset=True, exclude={"sign"})
    r = await ot.record_anaesthesia(scope, surgery_id, data, user, sign=body.sign)
    await _audit(user, request, "OT_ANAESTHESIA_SIGN" if body.sign else "OT_ANAESTHESIA_SAVE", "ot_surgery",
                 surgery_id, {"fields": sorted(data)})
    return r


@router.post("/surgeries/{surgery_id}/status")
async def surgery_status(surgery_id: str, body: StatusIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    perms = ("OT_SCHEDULE",) if body.action in ("cancel", "postpone") else ("OT_CHECKLIST", "OT_CLINICAL")
    scope, _ = await _scope(user, clinic_id, *perms)
    await _surgery(user, scope, surgery_id)
    s = await ot.set_status(scope, surgery_id, body.action, body.reason, user)
    if not s.get("replayed"):
        await _audit(user, request, f"OT_{body.action.upper()}", "ot_surgery", surgery_id,
                     {"status": s["status"], "reason": body.reason})
    return s


@router.post("/surgeries/{surgery_id}/complete")
async def complete(surgery_id: str, body: CompleteIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "OT_CLINICAL")
    if not getattr(user, "doctor_id", None):
        raise HTTPException(status_code=403, detail="Only a login linked to a doctor can finish a surgery.")
    await _surgery(user, scope, surgery_id)
    out = await ot.complete_surgery(scope, surgery_id, body.model_dump(exclude={"amount_paise"}),
                                    body.amount_paise, user)
    if not out.get("replayed"):
        charge = out.get("charge") or {}
        await _audit(user, request, "OT_COMPLETE", "ot_surgery", surgery_id,
                     {"charge_id": charge.get("id"), "amount_paise": charge.get("unit_price_paise")})
    return out
