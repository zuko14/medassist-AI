"""Kriya OPD OS API router (Phase 1.2).

Endpoints for Setup wizard, Patient Registry, Live Queue, and Public TV Display.
Zero-LLM clinical safety compliant.
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional, Sequence

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.database import (
    sb,
    scoped_query,
    supabase,
)
from app.services.permissions import resolve_owned_branch
from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.services.opd import (
    ALLOWED_TRANSITIONS,
    DEFAULT_SETTINGS,
    STAGES,
    arrive,
    create_walk_in,
    deactivation_preview,
    dry_run,
    duplicate_candidates,
    effective_state,
    get_queue_board,
    go_live,
    get_opd_analytics,
    opd_call_next,
    provision_defaults,
    public_display_payload,
    register_patient,
    rotate_display_token,
    search_patients,
    setup_checklist,
    transition_stage,
)
from app.services.opd_clinical import (
    amend_encounter,
    amend_prescription,
    get_encounter_prescription,
    get_or_create_draft,
    patient_history,
    save_notes,
    save_prescription_draft,
    save_vitals,
    send_prescription,
    sign_encounter,
    sign_prescription,
)
from app.services.opd_billing import (
    close_shift,
    create_invoice,
    create_receipt,
    create_refund,
    get_catalog,
    get_collections_summary,
    get_current_shift,
    get_invoice,
    get_or_create_appointment_invoice,
    issue_invoice,
    list_invoices,
    list_payment_exceptions,
    list_shifts,
    open_shift,
    resolve_payment_exception,
    save_invoice_draft,
    void_invoice,
)
from app.services.opd_pdf import render_invoice_pdf, render_prescription_pdf
from app.services.payment import payment_service
from app.services.tenant import clinic_letterhead, get_clinic_by_id, opd_enabled
from app.utils.helpers import doctor_title, today_ist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/opd", tags=["OPD Admin"])
public_router = APIRouter(prefix="/public", tags=["Public OPD Display"])


# ═══════════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════════


class ChecklistItem(BaseModel):
    key: str
    label: str
    done: bool
    blocking: bool
    detail: Optional[str] = None
    fix_page: Optional[str] = None


class OpdSetupOut(BaseModel):
    state: str
    checklist: Sequence[Any]
    settings: dict
    went_live_at: Optional[str] = None
    # Letterhead for counter receipts and the UPI payee name (cashiers cannot read /admin/profile).
    clinic: dict = {}


class DryRunOut(BaseModel):
    ok: bool
    checks: list[dict]


class CatalogItem(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z0-9_\-]{2,40}$")
    name: str = Field(..., min_length=1, max_length=200)
    item_type: Literal["nursing", "diagnostic", "procedure", "other"] = "other"
    price_paise: int = Field(..., ge=0, le=100_000_000)
    active: bool = True


class OpdSettingsPatch(BaseModel):
    vitals_required: Optional[bool] = None
    after_checkin_stage: Optional[Literal["registered", "vitals_pending", "waiting"]] = None
    billing_after_consult: Optional[bool] = None
    token_rule: Optional[Literal["per_doctor_daily"]] = None
    # Same modes the counter accepts (ReceiptIn.mode).
    payment_modes: Optional[list[Literal["cash", "upi", "card"]]] = Field(None, min_length=1)
    # The payee of every counter UPI QR: a typo sends patients' money elsewhere.
    # "" clears it.
    upi_vpa: Optional[str] = Field(None, pattern=r"^$|^[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}$")
    auto_open_shift: Optional[bool] = None
    rooms: Optional[dict[str, str]] = None
    # Meta template names, keyed by the OPD event that sends them. Merged into
    # the stored set (legacy opd_* alias keys survive); "" clears one.
    templates: Optional[dict[
        Literal["token_issued", "token_called", "prescription_ready", "receipt", "payment_link"],
        Annotated[str, Field(pattern=r"^[a-z0-9_]{0,512}$")],
    ]] = None
    service_catalog: Optional[list[CatalogItem]] = None
    confirmed_steps: Optional[list[str]] = None


class DuplicateCheckIn(BaseModel):
    name: str
    phone: str
    date_of_birth: Optional[date] = None
    age_years: Optional[int] = None


class PatientRegisterIn(BaseModel):
    phone: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")
    name: str = Field(..., min_length=1, max_length=100)
    is_account_holder: bool = True
    relationship: Optional[str] = Field(None, max_length=40)
    guardian_name: Optional[str] = Field(None, max_length=100)
    date_of_birth: Optional[date] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)
    gender: Optional[Literal["male", "female", "other", "undisclosed"]] = None
    address_line: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, pattern=r"^[1-9][0-9]{5}$")
    emergency_contact_name: Optional[str] = Field(None, max_length=100)
    emergency_contact_phone: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    emergency_contact_relation: Optional[str] = Field(None, max_length=40)
    allergies_status: Literal["unknown", "none_known", "recorded"] = "unknown"
    allergies: list[str] = Field(default_factory=list, max_length=50)
    data_consent: bool = False
    whatsapp_opt_in: bool = False
    acknowledged_duplicates: list[str] = Field(default_factory=list)


class PatientPatchIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    relationship: Optional[str] = Field(None, max_length=40)
    date_of_birth: Optional[date] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)
    gender: Optional[Literal["male", "female", "other", "undisclosed"]] = None
    address_line: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, pattern=r"^[1-9][0-9]{5}$")
    emergency_contact_name: Optional[str] = Field(None, max_length=100)
    emergency_contact_phone: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    emergency_contact_relation: Optional[str] = Field(None, max_length=40)
    allergies_status: Optional[Literal["unknown", "none_known", "recorded"]] = None
    allergies: Optional[list[str]] = None


class WalkInIn(BaseModel):
    patient_id: str
    family_member_id: Optional[str] = None
    doctor_id: str
    branch_id: Optional[str] = None
    visit_type: Literal["new", "followup", "review"] = "new"
    symptoms: Optional[str] = Field(None, max_length=500)
    skip_vitals: bool = False


class ArriveIn(BaseModel):
    visit_type: Optional[Literal["new", "followup", "review"]] = None


class StageChangeIn(BaseModel):
    to_stage: Literal[
        "registered",
        "vitals_pending",
        "waiting",
        "in_consultation",
        "billing",
        "completed",
        "cancelled",
    ]
    expected_from: str
    reason: Optional[str] = Field(None, max_length=200)


class CallNextIn(BaseModel):
    doctor_id: str


class VitalsIn(BaseModel):
    bp_systolic: Optional[int] = Field(None, ge=50, le=300)
    bp_diastolic: Optional[int] = Field(None, ge=20, le=200)
    pulse_bpm: Optional[int] = Field(None, ge=20, le=250)
    temperature_c: Optional[Decimal] = Field(None, ge=30, le=45)
    spo2_pct: Optional[int] = Field(None, ge=50, le=100)
    weight_kg: Optional[Decimal] = Field(None, gt=0, le=400)
    height_cm: Optional[Decimal] = Field(None, ge=30, le=250)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.bp_systolic is not None and self.bp_diastolic is not None and self.bp_systolic <= self.bp_diastolic:
            raise ValueError("Systolic BP must be greater than diastolic BP")
        if all(v is None for v in [self.bp_systolic, self.bp_diastolic, self.pulse_bpm, self.temperature_c, self.spo2_pct, self.weight_kg, self.height_cm]):
            raise ValueError("At least one vital sign is required")
        return self


class Diagnosis(BaseModel):
    system: Optional[Literal["ICD-10"]] = None
    code: Optional[str] = Field(None, pattern=r"^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$")
    text: str = Field(..., min_length=2, max_length=200)

    @model_validator(mode="after")
    def check_code(self):
        if self.code and not self.system:
            raise ValueError("System is required when diagnosis code is provided")
        return self


class EncounterNotesIn(BaseModel):
    expected_updated_at: datetime
    chief_complaints: Optional[str] = Field(None, max_length=4000)
    clinical_findings: Optional[str] = Field(None, max_length=8000)
    examination_notes: Optional[str] = Field(None, max_length=8000)
    diagnoses: list[Diagnosis] = Field(default_factory=list, max_length=20)
    advice: Optional[str] = Field(None, max_length=4000)
    follow_up_date: Optional[date] = None

    @model_validator(mode="after")
    def check_fu(self):
        if self.follow_up_date and self.follow_up_date < today_ist():
            raise ValueError("Follow up date must be today or in the future")
        return self


class AmendIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=300)


class RxItemIn(BaseModel):
    drug_name: str = Field(..., min_length=2, max_length=200)
    formulation: Literal[
        "tablet", "capsule", "syrup", "suspension", "injection",
        "drops", "ointment", "cream", "gel", "inhaler", "powder",
        "lotion", "spray", "patch", "other"
    ]
    strength: Optional[str] = Field(None, max_length=60)
    dosage: str = Field(..., min_length=1, max_length=60)
    route: Literal[
        "oral", "topical", "iv", "im", "sc", "inhalation", "nasal",
        "ophthalmic", "otic", "rectal", "vaginal", "sublingual", "other"
    ] = "oral"
    frequency: str = Field(..., min_length=1, max_length=40)
    timing: Literal["before_food", "after_food", "with_food", "empty_stomach", "bedtime", "any"] = "any"
    duration_days: Optional[int] = Field(None, ge=1, le=365)
    instructions: Optional[str] = Field(None, max_length=500)


class PrescriptionDraftIn(BaseModel):
    expected_updated_at: Optional[datetime] = None
    general_instructions: Optional[str] = Field(None, max_length=2000)
    items: list[RxItemIn] = Field(..., max_length=40)


class AllergyAck(BaseModel):
    line_no: int
    allergen: str
    override_reason: str = Field(..., min_length=5, max_length=300)


class RxSignIn(BaseModel):
    acknowledgements: list[AllergyAck] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Billing Models (§3.7)
# ─────────────────────────────────────────────────────────────────────────────


class InvoiceItemIn(BaseModel):
    item_type: Literal["consultation", "nursing", "diagnostic", "procedure", "other"]
    catalog_code: Optional[str] = Field(None, max_length=40)
    doctor_id: Optional[str] = None
    lab_test_id: Optional[str] = None
    description: str = Field(..., min_length=1, max_length=200)
    quantity: int = Field(1, ge=1, le=999)
    unit_price_paise: int = Field(..., ge=0, le=100_000_000)
    discount_paise: int = Field(0, ge=0)


class InvoiceDraftIn(BaseModel):
    expected_updated_at: datetime
    items: list[InvoiceItemIn] = Field(..., min_length=1, max_length=100)
    discount_paise: int = Field(0, ge=0)
    discount_reason: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def check_discount_reason(self) -> "InvoiceDraftIn":
        if self.discount_paise > 0 and not (self.discount_reason and self.discount_reason.strip()):
            raise ValueError("Discount reason is required when discount_paise > 0")
        return self


class InvoiceCreateIn(BaseModel):
    patient_id: str
    family_member_id: Optional[str] = None
    appointment_id: Optional[str] = None
    items: list[InvoiceItemIn] = Field(..., min_length=1, max_length=100)
    discount_paise: int = Field(0, ge=0)
    discount_reason: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=1000)
    branch_id: Optional[str] = None

    @model_validator(mode="after")
    def check_discount_reason(self) -> "InvoiceCreateIn":
        if self.discount_paise > 0 and not (self.discount_reason and self.discount_reason.strip()):
            raise ValueError("Discount reason is required when discount_paise > 0")
        return self


class ReceiptIn(BaseModel):
    mode: Literal["cash", "upi", "card"]
    amount_paise: int = Field(..., ge=1, le=100_000_000)
    reference: Optional[str] = Field(None, max_length=100)
    idempotency_key: str = Field(..., min_length=8, max_length=64)
    shift_id: Optional[str] = None


class RefundIn(BaseModel):
    mode: Literal["cash", "upi"]
    amount_paise: int = Field(..., ge=1)
    reason: str = Field(..., min_length=5, max_length=300)
    reference: Optional[str] = Field(None, max_length=100)


class ShiftOpenIn(BaseModel):
    opening_float_paise: int = Field(0, ge=0, le=100_000_000)
    branch_id: Optional[str] = None


class ShiftCloseIn(BaseModel):
    declared_cash_paise: int = Field(..., ge=0)
    notes: Optional[str] = Field(None, max_length=500)


class VoidIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=300)


class PaymentExceptionResolveIn(BaseModel):
    action: Literal["refund_gateway", "settled_offline"]
    note: Optional[str] = Field(None, max_length=300)
    reference: Optional[str] = Field(None, max_length=100)


class OpdAnalyticsOut(BaseModel):
    footfall: dict
    waits: dict
    no_shows: int
    cancellations_in_queue: int
    collections: dict
    prescriptions: dict
    peak_hours: list[dict]


# ═══════════════════════════════════════════════════════════════════════════════
# COMMON PERMISSION & SCOPE GUARDS
# ═══════════════════════════════════════════════════════════════════════════════


def _holds(user: AdminUser, *perms: str) -> bool:
    """Check if user holds super_admin, clinic_admin, or any requested permission."""
    return user.role in ("clinic_admin", "super_admin") or any(
        p in (user.permissions or []) for p in perms
    )


def _doctor_only(user: AdminUser) -> bool:
    """A login linked to a doctor with no desk/billing/admin duty: it works its
    own doctor's queue, wherever that patient was registered."""
    return bool(user.doctor_id) and not _holds(user, "OPD_FRONT_DESK", "OPD_BILLING", "OPD_ADMIN")


def _read_branch(user: AdminUser, requested: Optional[str]) -> Optional[str]:
    """Branch filter for a read. Branch-pinned staff always read their own
    branch; asking for another is 403. Unpinned staff may filter or not."""
    if requested:
        enforce_branch_scope(user, requested)
    return str(user.branch_id) if getattr(user, "branch_id", None) else requested


async def _write_branch(user: AdminUser, scope: str, requested: Optional[str]) -> Optional[str]:
    """Verify a branch a write names: it must belong to this clinic (404) and
    to a pinned caller (403). None leaves the service's own default."""
    if requested:
        await resolve_owned_branch(user, requested, scope)
    return requested


async def _opd_scope(
    user: AdminUser,
    clinic_id: str,
    *perms: str,
    live: bool = True,
) -> tuple[str, dict]:
    """Validate clinic access, permissions, OPD entitlement, and live readiness."""
    if perms and not _holds(user, *perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")

    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    if not opd_enabled(clinic):
        raise HTTPException(status_code=403, detail="The OPD module is not enabled for this clinic.")

    if live and clinic.get("opd_state") != "READY":
        raise HTTPException(
            status_code=409,
            detail="OPD setup is not complete. Finish the setup wizard first.",
        )

    return scope, clinic


# ═══════════════════════════════════════════════════════════════════════════════
# §3.2 SETUP WIZARD
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/setup", response_model=OpdSetupOut)
async def get_setup(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve setup wizard checklist and current configuration."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_ADMIN", "OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING", live=False
    )

    checklist = await setup_checklist(clinic)
    state = await effective_state(clinic, checklist)

    return OpdSetupOut(
        state=state,
        checklist=checklist,
        settings=clinic.get("opd_settings") or {},
        went_live_at=(clinic.get("opd_settings") or {}).get("went_live_at"),
        clinic=clinic_letterhead(clinic),
    )


@router.put("/setup/settings", response_model=OpdSetupOut)
async def update_setup_settings(
    patch: OpdSettingsPatch,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Update OPD configuration settings via CAS."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN", live=False)

    patch_dict = {k: v for k, v in patch.model_dump().items() if v is not None}
    if "service_catalog" in patch_dict and patch_dict["service_catalog"] is not None:
        patch_dict["service_catalog"] = [
            i if isinstance(i, dict) else i.model_dump() for i in patch_dict["service_catalog"]
        ]

    merged_settings = dict(clinic.get("opd_settings") or {})
    if "templates" in patch_dict:
        tmpl = {**(merged_settings.get("templates") or {}), **patch_dict["templates"]}
        patch_dict["templates"] = {k: v for k, v in tmpl.items() if v}
    merged_settings.update(patch_dict)

    current_state = clinic.get("opd_state") or "NOT_CONFIGURED"
    new_state = "CONFIGURING" if current_state == "NOT_CONFIGURED" else current_state

    # CAS update
    for _ in range(3):
        res = await sb(
            # unscoped: tenant_root - the clinics row IS the tenant; pinned by .eq("id", scope)
            supabase.table("clinics")
            .update({
                "opd_settings": merged_settings,
                "opd_state": new_state,
            })
            .eq("id", scope)
        )
        if res.data:
            break

    await log_admin_action(
        user=user,
        action="OPD_SETUP_UPDATE",
        resource_type="clinic_opd_settings",
        resource_id=scope,
        details=patch_dict,
    )

    fresh_clinic = await get_clinic_by_id(scope)
    checklist = await setup_checklist(fresh_clinic)
    state = await effective_state(fresh_clinic, checklist)

    return OpdSetupOut(
        state=state,
        checklist=checklist,
        settings=fresh_clinic.get("opd_settings") or {},
        went_live_at=(fresh_clinic.get("opd_settings") or {}).get("went_live_at"),
    )


@router.post("/setup/dry-run", response_model=DryRunOut)
async def run_dry_run(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Execute pre-flight test visit simulation."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN", live=False)
    res = await dry_run(clinic)

    await log_admin_action(
        user=user,
        action="OPD_DRY_RUN",
        resource_type="clinic_opd_setup",
        resource_id=scope,
        details={"ok": res["ok"]},
    )
    return DryRunOut(ok=res["ok"], checks=res["checks"])


@router.post("/setup/go-live", response_model=OpdSetupOut)
async def activate_go_live(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Transition clinic to live OPD READY state."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN", live=False)
    res = await go_live(clinic, user)

    await log_admin_action(
        user=user,
        action="OPD_GO_LIVE",
        resource_type="clinic_opd_setup",
        resource_id=scope,
        details={"went_live_at": res.get("went_live_at")},
    )
    return OpdSetupOut(
        state=res["state"],
        checklist=res["checklist"],
        settings=res["settings"],
        went_live_at=res.get("went_live_at"),
    )


@router.post("/display-token")
async def create_display_token(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Generate and return a one-time hallway TV display URL."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN", live=False)
    token = await rotate_display_token(scope)

    await log_admin_action(
        user=user,
        action="OPD_DISPLAY_TOKEN_ROTATE",
        resource_type="clinic_opd_display",
        resource_id=scope,
    )
    return {"url": f"/public/queue-display#t={token}"}


# ═══════════════════════════════════════════════════════════════════════════════
# §3.3 PATIENT REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/patients/search")
async def search_registry_patients(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=50),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Search patients across account holders, family members, and legacy records."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING", live=True
    )
    return await search_patients(scope, q=q, limit=limit)


@router.post("/patients/duplicate-check")
async def check_duplicate_patients(
    body: DuplicateCheckIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Check for candidate duplicates prior to registration."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_FRONT_DESK", live=True)
    return await duplicate_candidates(
        scope,
        name=body.name,
        phone=body.phone,
        dob=body.date_of_birth,
        age=body.age_years,
    )


@router.post("/patients", status_code=status.HTTP_201_CREATED)
async def register_new_patient(
    body: PatientRegisterIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Register a new patient or family member."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_FRONT_DESK", live=True)
    res = await register_patient(scope, body.model_dump(), user)

    await log_admin_action(
        user=user,
        action="OPD_PATIENT_REGISTER",
        resource_type="patient",
        resource_id=res["patient_id"],
        details={"name": res["name"], "mrn": res.get("mrn")},
    )
    return res


@router.get("/patients/{patient_id}")
async def get_registry_patient_detail(
    patient_id: str,
    family_member_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Fetch complete patient demographics, visit history, and clinical records."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING", live=True
    )

    if family_member_id:
        fm_res = await sb(
            scoped_query("family_members", scope)
            .eq("id", family_member_id)
            .limit(1)
        )
        if not fm_res.data:
            raise HTTPException(status_code=404, detail="Family member not found")
        data = fm_res.data[0]
        name = data.get("full_name")
        phone = data.get("primary_phone")
    else:
        pat_res = await sb(
            scoped_query("patients", scope).eq("id", patient_id).limit(1)
        )
        if not pat_res.data:
            raise HTTPException(status_code=404, detail="Patient not found")
        data = pat_res.data[0]
        name = data.get("name")
        phone = data.get("phone")

    # Visits
    v_query = (
        scoped_query("appointments", scope)
        .eq("patient_id", patient_id)
    )
    if family_member_id:
        v_query = v_query.eq("family_member_id", family_member_id)
    v_res = await sb(v_query.order("appointment_date", desc=True).limit(20))

    # Clinical history (restricted to clinical role / admin)
    history = []
    if _holds(user, "OPD_CLINICAL"):
        h_query = (
            scoped_query("opd_encounters", scope)
            .eq("patient_id", patient_id)
            .in_("status", ["signed", "superseded"])
        )
        if family_member_id:
            h_query = h_query.eq("family_member_id", family_member_id)
        h_res = await sb(h_query.order("signed_at", desc=True).limit(20))
        history = h_res.data or []

    await log_admin_action(
        user=user,
        action="OPD_CLINICAL_VIEW",
        resource_type="patient",
        resource_id=patient_id,
    )

    return {
        "patient_id": patient_id,
        "family_member_id": family_member_id,
        "name": name,
        "phone": phone,
        "mrn": data.get("mrn"),
        "date_of_birth": data.get("date_of_birth"),
        "age_years": data.get("age_years"),
        "gender": data.get("gender"),
        "allergies": data.get("allergies") or [],
        "allergies_status": data.get("allergies_status") or "unknown",
        "visits": v_res.data or [],
        "clinical_history": history,
    }


@router.patch("/patients/{patient_id}")
async def patch_registry_patient(
    patient_id: str,
    patch: PatientPatchIn,
    family_member_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Update patient demographic fields."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL", live=True
    )
    patch_data = {k: v for k, v in patch.model_dump().items() if v is not None}
    if "date_of_birth" in patch_data and patch_data["date_of_birth"]:
        patch_data["date_of_birth"] = str(patch_data["date_of_birth"])
    if "age_years" in patch_data and patch_data["age_years"] is not None:
        patch_data["age_recorded_on"] = today_ist().isoformat()

    if family_member_id:
        if "name" in patch_data:
            patch_data["full_name"] = patch_data.pop("name")
        res = await sb(
            supabase.table("family_members")
            .update(patch_data)
            .eq("clinic_id", scope)
            .eq("id", family_member_id)
        )
    else:
        res = await sb(
            supabase.table("patients")
            .update(patch_data)
            .eq("clinic_id", scope)
            .eq("id", patient_id)
        )

    if not res.data:
        raise HTTPException(status_code=404, detail="Patient or family member not found")

    await log_admin_action(
        user=user,
        action="OPD_PATIENT_UPDATE",
        resource_type="patient",
        resource_id=family_member_id or patient_id,
        details=patch_data,
    )
    return res.data[0]


# ═══════════════════════════════════════════════════════════════════════════════
# §3.4 WALK-INS & LIVE QUEUE
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/walk-ins", status_code=status.HTTP_201_CREATED)
async def create_walk_in_appointment(
    body: WalkInIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Issue a walk-in token and assign queue stage."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_FRONT_DESK", live=True)

    await _write_branch(user, scope, body.branch_id)

    qrow = await create_walk_in(clinic, body.model_dump(), user)

    await log_admin_action(
        user=user,
        action="OPD_WALK_IN_CREATE",
        resource_type="appointment",
        resource_id=qrow["appointment_id"],
        details={"token_number": qrow["token_number"], "doctor_id": qrow["doctor_id"]},
    )
    return qrow


@router.post("/appointments/{appointment_id}/arrive")
async def arrive_scheduled_appointment(
    appointment_id: str,
    body: Optional[ArriveIn] = None,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Mark an existing scheduled appointment as arrived and assign token."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_FRONT_DESK", live=True)
    qrow = await arrive(
        clinic,
        appointment_id,
        user,
        visit_type=body.visit_type if body else None,
    )

    await log_admin_action(
        user=user,
        action="OPD_PATIENT_ARRIVE",
        resource_type="appointment",
        resource_id=appointment_id,
        details={"token_number": qrow["token_number"]},
    )
    return qrow


@router.get("/queue")
async def get_live_queue(
    request: Request,
    date: Optional[str] = Query(None),
    doctor_id: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    branch_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve full OPD live queue board with ETag caching."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING", live=True
    )

    if _doctor_only(user):
        # A doctor sees their own patients only, at whichever branch they arrived.
        doctor_id, effective_branch = str(user.doctor_id), None
    else:
        effective_branch = _read_branch(user, branch_id)
    date_val = date or today_ist().isoformat()

    board = await get_queue_board(
        scope,
        date_str=date_val,
        doctor_id=doctor_id,
        department=department,
        branch_id=effective_branch,
    )

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"') == board["etag"]:
        return Response(status_code=304, headers={"ETag": f'"{board["etag"]}"'})

    return Response(
        content=json.dumps(board),
        media_type="application/json",
        headers={"ETag": f'"{board["etag"]}"'},
    )


@router.post("/queue/{appointment_id}/stage")
async def update_queue_stage(
    appointment_id: str,
    body: StageChangeIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Transition an appointment's queue stage atomically using CAS."""
    scope, clinic = await _opd_scope(user, clinic_id, live=True)

    # Permission validation
    # Deny by default: only the desk (or an admin) moves any queue; anyone else needs
    # OPD_CLINICAL and may act on their own doctor queue only.
    if not _holds(user, "OPD_FRONT_DESK"):
        if "OPD_CLINICAL" in (user.permissions or []):
            appt_res = await sb(
                scoped_query("appointments", scope).eq("id", appointment_id).limit(1)
            )
            if not appt_res.data:
                raise HTTPException(status_code=404, detail="Appointment not found")
            appt = appt_res.data[0]
            if not user.doctor_id or str(user.doctor_id) != str(appt.get("doctor_id") or ""):
                raise HTTPException(
                    status_code=403, detail="Doctors can only advance their own consultations"
                )
            if body.expected_from != "in_consultation":
                raise HTTPException(
                    status_code=403, detail="Clinical role can only transition out of in_consultation"
                )
        else:
            raise HTTPException(status_code=403, detail="OPD_FRONT_DESK permission required")

    qrow = await transition_stage(
        clinic,
        appointment_id,
        to_stage=body.to_stage,
        expected_from=body.expected_from,
        actor=user,
        reason=body.reason,
    )

    await log_admin_action(
        user=user,
        action="OPD_STAGE_TRANSITION",
        resource_type="appointment",
        resource_id=appointment_id,
        details={"from": body.expected_from, "to": body.to_stage},
    )
    return qrow


@router.post("/queue/call-next")
async def call_next_queue_patient(
    body: CallNextIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Advance currently serving patient and claim next patient."""
    scope, clinic = await _opd_scope(user, clinic_id, live=True)

    # Deny by default: only the desk (or an admin) moves any queue; anyone else needs
    # OPD_CLINICAL and may act on their own doctor queue only.
    if not _holds(user, "OPD_FRONT_DESK"):
        if "OPD_CLINICAL" in (user.permissions or []):
            if not user.doctor_id or str(user.doctor_id) != str(body.doctor_id):
                raise HTTPException(
                    status_code=403, detail="Doctors can only call next for their own queue"
                )
        else:
            raise HTTPException(status_code=403, detail="OPD_FRONT_DESK permission required")

    res = await opd_call_next(clinic, doctor_id=body.doctor_id, actor=user)

    await log_admin_action(
        user=user,
        action="OPD_QUEUE_CALL_NEXT",
        resource_type="doctor_queue",
        resource_id=body.doctor_id,
        details={
            "called_token": (res.get("called") or {}).get("token_number"),
            "finished_token": (res.get("finished") or {}).get("token_number"),
        },
    )
    return res


@router.post("/queue/{appointment_id}/recall")
async def recall_queue_patient(
    appointment_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Send a re-announcement notification to the patient without altering stage."""
    scope, clinic = await _opd_scope(
        user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL", live=True
    )
    appt_res = await sb(
        scoped_query("appointments", scope).eq("id", appointment_id).limit(1)
    )
    if not appt_res.data:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt = appt_res.data[0]

    notified = False
    if appt.get("patient_phone"):
        try:
            from app.services.whatsapp import whatsapp_service
            await whatsapp_service.send_text(
                clinic,
                appt["patient_phone"],
                f"Recall notice: Token {appt.get('token_number')} for {doctor_title(appt.get('doctor_name'))} - please proceed to your room.",
                _source="opd_recall",
            )
            notified = True
        except Exception as e:
            logger.warning(f"Recall notification failed: {e}")

    return {"notified": notified}


# ═══════════════════════════════════════════════════════════════════════════════
# §3.5 ENCOUNTERS & VITALS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/workspace")
async def get_doctor_workspace(
    date_query: Optional[date] = Query(None, alias="date"),
    doctor_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Doctor workspace queue and context (§3.5)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    target_date = date_query or today_ist()

    # Enforce treating doctor if user is linked to a doctor
    if user.doctor_id:
        target_doc_id = user.doctor_id
    elif doctor_id:
        target_doc_id = doctor_id
    else:
        docs_res = await sb(
            supabase.table("doctors")
            .select("id")
            .eq("clinic_id", scope)
            .eq("is_active", True)
            .limit(1)
        )
        if not docs_res.data:
            raise HTTPException(status_code=404, detail="No active doctor found")
        target_doc_id = str(docs_res.data[0]["id"])

    doc_res = await sb(
        supabase.table("doctors")
        .select("id, name, department, qualifications, registration_number, registration_council")
        .eq("clinic_id", scope)
        .eq("id", target_doc_id)
        .single()
    )
    if not doc_res.data:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor = doc_res.data

    board = await get_queue_board(
        scope,
        date_str=target_date.isoformat(),
        doctor_id=target_doc_id,
        # A linked doctor's queue is theirs at every branch; a branch filter
        # would hide their own patients registered elsewhere or before branches.
        branch_id=None if user.doctor_id else user.branch_id,
    )
    active_stages = {"registered", "vitals_pending", "waiting", "in_consultation"}
    queue_rows = []
    for doc_entry in board.get("doctors", []):
        for qrow in doc_entry.get("rows", []):
            if qrow.get("stage") in active_stages:
                queue_rows.append(qrow)

    stage_order = {"in_consultation": 0, "waiting": 1, "vitals_pending": 2, "registered": 3}
    queue_rows.sort(key=lambda r: (stage_order.get(r.get("stage"), 99), r.get("token_number") or 0))

    return {
        "doctor": doctor,
        "date": target_date.isoformat(),
        "queue": queue_rows,
    }


@router.get("/encounters/by-appointment/{appointment_id}")
async def get_encounter_by_appointment(
    appointment_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Encounter details by appointment (§3.5). FRONT_DESK receives vitals only."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL", "OPD_FRONT_DESK")
    encounter = await get_or_create_draft(scope, appointment_id, user.user_id)

    # Whose chart this is: vitals and notes must never be entered against an
    # unnamed record (the encounter row itself carries only ids).
    if encounter.get("family_member_id"):
        who_res = await sb(scoped_query("family_members", scope, "full_name, mrn")
                           .eq("id", encounter["family_member_id"]).limit(1))
        who = {"patient_name": (who_res.data or [{}])[0].get("full_name"), "mrn": (who_res.data or [{}])[0].get("mrn")}
    else:
        who_res = await sb(scoped_query("patients", scope, "name, mrn")
                           .eq("id", encounter["patient_id"]).limit(1))
        who = {"patient_name": (who_res.data or [{}])[0].get("name"), "mrn": (who_res.data or [{}])[0].get("mrn")}

    has_clinical = _holds(user, "OPD_CLINICAL")
    if not has_clinical:
        return {
            **who,
            "id": encounter["id"],
            "appointment_id": appointment_id,
            "status": encounter["status"],
            "bp_systolic": encounter.get("bp_systolic"),
            "bp_diastolic": encounter.get("bp_diastolic"),
            "pulse_bpm": encounter.get("pulse_bpm"),
            "temperature_c": encounter.get("temperature_c"),
            "spo2_pct": encounter.get("spo2_pct"),
            "weight_kg": encounter.get("weight_kg"),
            "height_cm": encounter.get("height_cm"),
            "bmi": encounter.get("bmi"),
            "vitals_recorded_at": encounter.get("vitals_recorded_at"),
            "vitals_recorded_by_name": encounter.get("vitals_recorded_by_name"),
        }

    from app.services.opd_clinical import get_patient_allergies
    allergies, status_code = await get_patient_allergies(
        scope, encounter["patient_id"], encounter.get("family_member_id")
    )
    history = await patient_history(
        scope, encounter["patient_id"], encounter.get("family_member_id")
    )

    encounter_out = {**dict(encounter), **who}
    encounter_out["allergies"] = allergies
    encounter_out["allergies_status"] = status_code
    encounter_out["history"] = history
    return encounter_out


@router.put("/encounters/by-appointment/{appointment_id}/vitals")
async def update_encounter_vitals(
    appointment_id: str,
    body: VitalsIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Save vitals and advance vitals_pending -> waiting (§3.5)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_FRONT_DESK", "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "name": user.username, "email": user.username}
    enc = await save_vitals(clinic, appointment_id, body.model_dump(), actor)
    return enc


@router.put("/encounters/{encounter_id}")
async def update_encounter_notes(
    encounter_id: str,
    body: EncounterNotesIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Save clinical notes and diagnoses with CAS optimistic locking (§3.5)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    enc = await save_notes(scope, encounter_id, body.model_dump(), actor)
    return enc


@router.post("/encounters/{encounter_id}/sign")
async def sign_encounter_route(
    encounter_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Sign and lock encounter (§3.5). Treating doctor login only."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    enc = await sign_encounter(clinic, encounter_id, actor)
    return enc


@router.post("/encounters/{encounter_id}/amend")
async def amend_encounter_route(
    encounter_id: str,
    body: AmendIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create a new amendment draft encounter (§3.5). Treating doctor only."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    new_enc = await amend_encounter(clinic, encounter_id, body.reason, actor)
    return new_enc


# ═══════════════════════════════════════════════════════════════════════════════
# §3.6 e-PRESCRIPTIONS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/encounters/{encounter_id}/prescription")
async def get_prescription_route(
    encounter_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Get prescription and items for encounter (§3.6). FRONT_DESK can only view signed."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL", "OPD_FRONT_DESK")
    rx = await get_encounter_prescription(scope, encounter_id)
    if not rx:
        return None

    has_clinical = _holds(user, "OPD_CLINICAL")
    if not has_clinical and rx.get("status") == "draft":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Front desk staff can only view signed prescriptions",
        )
    return rx


@router.put("/encounters/{encounter_id}/prescription")
async def update_prescription_draft_route(
    encounter_id: str,
    body: PrescriptionDraftIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create or update prescription draft items and notes (§3.6). Treating doctor only."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    rx = await save_prescription_draft(scope, encounter_id, body.model_dump(), actor)
    return rx


@router.post("/prescriptions/{rx_id}/sign")
async def sign_prescription_route(
    rx_id: str,
    body: RxSignIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Sign and lock prescription with allergy acknowledgements (§3.6)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    acks = [ack.model_dump() for ack in body.acknowledgements]
    rx = await sign_prescription(clinic, rx_id, actor, acks)
    return rx


@router.post("/prescriptions/{rx_id}/amend")
async def amend_prescription_route(
    rx_id: str,
    body: AmendIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create an amendment draft prescription (§3.6). Treating doctor only."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL")
    actor = {"user_id": user.user_id, "doctor_id": user.doctor_id, "name": user.username}
    new_rx = await amend_prescription(clinic, rx_id, body.reason, actor)
    return new_rx


@router.get("/prescriptions/{rx_id}/pdf")
async def get_prescription_pdf_route(
    rx_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Render prescription as A5 PDF (§3.6). Refuses drafts with 409."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL", "OPD_FRONT_DESK")
    rx_res = await sb(
        supabase.table("opd_prescriptions")
        .select("*")
        .eq("clinic_id", scope)
        .eq("id", rx_id)
        .single()
    )
    if not rx_res.data:
        raise HTTPException(status_code=404, detail="Prescription not found")
    rx = rx_res.data

    items_res = await sb(
        supabase.table("opd_prescription_items")
        .select("*")
        .eq("clinic_id", scope)
        .eq("prescription_id", rx_id)
        .order("line_no")
    )
    items = items_res.data or []

    pdf_bytes = render_prescription_pdf(rx, items)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Rx_{rx_id[:8]}.pdf"},
    )


@router.post("/prescriptions/{rx_id}/send-whatsapp")
async def send_prescription_whatsapp_route(
    rx_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Dispatch signed prescription on WhatsApp (§3.6)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_CLINICAL", "OPD_FRONT_DESK")
    actor = {"user_id": user.user_id, "name": user.username}
    res = await send_prescription(clinic, rx_id, actor)
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# §3.7 INVOICING & CASHIER
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/catalog")
async def get_opd_catalog(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Catalog of doctor consultation fees, clinic services, and lab tests."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await get_catalog(scope)


@router.post("/appointments/{appointment_id}/invoice")
async def create_appointment_invoice(
    appointment_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create or retrieve the single live draft invoice for an appointment visit."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_FRONT_DESK", "OPD_ADMIN")
    return await get_or_create_appointment_invoice(scope, appointment_id, user)


@router.post("/invoices")
async def create_ad_hoc_invoice(
    body: InvoiceCreateIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Create an ad-hoc invoice for a patient."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await create_invoice(
        clinic_id=scope,
        patient_id=body.patient_id,
        items=[it.model_dump() for it in body.items],
        family_member_id=body.family_member_id,
        appointment_id=body.appointment_id,
        discount_paise=body.discount_paise,
        discount_reason=body.discount_reason,
        notes=body.notes,
        branch_id=await _write_branch(user, scope, body.branch_id),
        actor=user,
    )


@router.put("/invoices/{invoice_id}")
async def update_invoice_draft(
    invoice_id: str,
    body: InvoiceDraftIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Update draft invoice items and discounts with CAS optimistic locking."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await save_invoice_draft(
        clinic_id=scope,
        invoice_id=invoice_id,
        expected_updated_at=body.expected_updated_at,
        items=[it.model_dump() for it in body.items],
        discount_paise=body.discount_paise,
        discount_reason=body.discount_reason,
        notes=body.notes,
        actor=user,
    )


@router.post("/invoices/{invoice_id}/issue")
async def issue_opd_invoice(
    invoice_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Issue a draft invoice, assigning sequential invoice number."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await issue_invoice(scope, invoice_id, user)


@router.post("/invoices/{invoice_id}/receipts", status_code=status.HTTP_201_CREATED)
async def record_invoice_receipt(
    invoice_id: str,
    body: ReceiptIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Record an in-person payment receipt (cash, upi, card)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await create_receipt(
        clinic_id=scope,
        invoice_id=invoice_id,
        mode=body.mode,
        amount_paise=body.amount_paise,
        idempotency_key=body.idempotency_key,
        reference=body.reference,
        actor=user,
        shift_id=body.shift_id,
    )


@router.post("/invoices/{invoice_id}/payment-link")
async def generate_payment_link(
    invoice_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Generate an online payment link (Razorpay/PhonePe) for outstanding balance."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    inv = await get_invoice(scope, invoice_id)
    if inv.get("status") not in ("issued", "partially_paid"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot create payment link for invoice in '{inv.get('status')}' status.",
        )
    total = inv.get("total_paise", 0)
    paid = inv.get("paid_paise", 0)
    balance = max(0, total - paid)
    if balance <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invoice is already fully paid.")

    pat_res = await sb(scoped_query("patients", scope).eq("id", inv["patient_id"]).limit(1))
    patient = pat_res.data[0] if pat_res.data else {}

    link_res = await payment_service.create_opd_payment_link(clinic, inv, patient, balance)
    # Dispatch WhatsApp payment link if patient phone is present
    if patient.get("phone"):
        try:
            import asyncio
            from app.services.whatsapp import whatsapp_service
            templates = (clinic.get("opd_settings") or {}).get("templates") or {}
            tmpl_name = templates.get("payment_link") or templates.get("opd_payment_link")
            pay_url = link_res.get("url") or ""
            amt_inr = f"₹{balance / 100:.2f}"
            inv_num = inv.get("invoice_number") or f"INV-{invoice_id[:8]}"
            if tmpl_name:
                asyncio.create_task(
                    whatsapp_service.send_template(
                        clinic,
                        patient["phone"],
                        template_name=tmpl_name,
                        components=[
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": inv_num},
                                    {"type": "text", "text": amt_inr},
                                    {"type": "text", "text": pay_url},
                                ],
                            }
                        ],
                        _source="opd_payment_link",
                    )
                )
            elif pay_url:
                asyncio.create_task(
                    whatsapp_service.send_text(
                        clinic,
                        patient["phone"],
                        f"Please pay your OPD invoice {inv_num} ({amt_inr}) using this secure payment link: {pay_url}",
                        _source="opd_payment_link",
                    )
                )
        except Exception as e:
            logger.debug(f"Failed to dispatch WhatsApp payment link: {e}")
    return link_res


@router.post("/invoices/{invoice_id}/refunds")
async def record_invoice_refund(
    invoice_id: str,
    body: RefundIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Record a refund against an invoice (OPD_ADMIN only)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN")
    return await create_refund(
        clinic_id=scope,
        invoice_id=invoice_id,
        mode=body.mode,
        amount_paise=body.amount_paise,
        reason=body.reason,
        reference=body.reference,
        actor=user,
    )


@router.post("/invoices/{invoice_id}/void")
async def void_opd_invoice(
    invoice_id: str,
    body: VoidIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Void an invoice with reason (OPD_ADMIN only). Fails if paid > 0."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN")
    return await void_invoice(scope, invoice_id, reason=body.reason, actor=user)


@router.get("/payment-exceptions")
async def list_opd_payment_exceptions(
    status_filter: Literal["open", "refunded", "settled_offline", "all"] = Query("open", alias="status"),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Online payments that could not be applied in full to their invoice (migration 104)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return {"items": await list_payment_exceptions(scope, status_filter)}


@router.post("/payment-exceptions/{exception_id}/resolve")
async def resolve_opd_payment_exception(
    exception_id: str,
    body: PaymentExceptionResolveIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Refund the excess through the gateway, or record an offline settlement (OPD_ADMIN only)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN")
    return await resolve_payment_exception(
        scope, exception_id, body.action, note=body.note, reference=body.reference, actor=user,
    )


@router.get("/invoices")
async def list_opd_invoices(
    date_query: Optional[date] = Query(None, alias="date"),
    status_filter: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """List invoices with date, status, and patient search filters."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    date_str = date_query.isoformat() if date_query else None
    return await list_invoices(
        scope, date_str=date_str, status_filter=status_filter, q=q,
        branch_id=_read_branch(user, None),
    )


@router.get("/invoices/{invoice_id}")
async def get_opd_invoice(
    invoice_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve full invoice details, line items, and receipts ledger."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_FRONT_DESK", "OPD_ADMIN")
    inv = await get_invoice(scope, invoice_id)
    enforce_branch_scope(user, inv.get("branch_id"))
    return inv


@router.get("/invoices/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: str,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Download immutable A4 invoice PDF."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_FRONT_DESK", "OPD_ADMIN")
    inv = await get_invoice(scope, invoice_id)
    enforce_branch_scope(user, inv.get("branch_id"))
    items = inv.get("items") or []
    receipts = inv.get("receipts") or []
    pdf_bytes = render_invoice_pdf(inv, items, receipts, clinic=clinic)
    inv_num = inv.get("invoice_number") or invoice_id[:8]
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="Invoice_{inv_num}.pdf"'},
    )


@router.post("/shifts/open")
async def open_cashier_shift(
    body: ShiftOpenIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Open a cashier drawer shift."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await open_shift(
        scope, opening_float_paise=body.opening_float_paise,
        branch_id=await _write_branch(user, scope, body.branch_id), actor=user,
    )


@router.get("/shifts/current")
async def get_my_current_shift(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Retrieve caller's active open shift with live mode totals."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await get_current_shift(scope, actor=user)


@router.post("/shifts/{shift_id}/close")
async def close_cashier_shift(
    shift_id: str,
    body: ShiftCloseIn,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Close cashier drawer shift with declared cash and notes."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    return await close_shift(scope, shift_id, declared_cash_paise=body.declared_cash_paise, notes=body.notes, actor=user)


@router.get("/shifts")
async def list_all_shifts(
    date_query: Optional[date] = Query(None, alias="date"),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """List cashier drawer shifts with expected, declared, and variance (OPD_ADMIN)."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN")
    date_str = date_query.isoformat() if date_query else None
    return await list_shifts(scope, date_str=date_str, actor=user)


@router.get("/collections/summary")
async def get_daily_collections_summary(
    date_query: Optional[date] = Query(None, alias="date"),
    branch_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Compute financial summary for a day across cashiers and payment modes."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_BILLING", "OPD_ADMIN")
    date_str = date_query.isoformat() if date_query else None
    return await get_collections_summary(scope, date_str=date_str, branch_id=_read_branch(user, branch_id))


# ═══════════════════════════════════════════════════════════════════════════════
# §3.8 OPERATIONAL ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/analytics", response_model=OpdAnalyticsOut)
async def get_analytics(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    branch_id: Optional[str] = Query(None),
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Compute aggregated operational metrics over scoped rows with pagination and 50,000 cap."""
    scope, clinic = await _opd_scope(user, clinic_id, "OPD_ADMIN")
    return await get_opd_analytics(
        clinic_id=scope,
        from_date_str=from_date,
        to_date_str=to_date,
        branch_id=_read_branch(user, branch_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# §3.10 PUBLIC HALLWAY TV DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════


@public_router.get("/queue-display/data")
async def get_public_display_data(
    branch_id: Optional[str] = Query(None),
    x_display_token: Optional[str] = Header(None, alias="X-Display-Token"),
    token: Optional[str] = Query(None),
    t: Optional[str] = Query(None),
):
    """Public hallway TV display queue data (zero PII returned)."""
    raw_token = x_display_token or token or t
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Display token required",
        )

    import hashlib
    tok_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    clinic_res = await sb(
        # unscoped: global_auth_lookup - the display token hash is globally unique and IS the credential
        supabase.table("clinics")
        .select("id, name, opd_state")
        .eq("opd_display_token_hash", tok_hash)
        .limit(1)
    )
    if not clinic_res.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid display token",
        )

    clinic_row = clinic_res.data[0]
    clinic_id = str(clinic_row["id"])

    if clinic_row.get("opd_state") != "READY":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OPD queue is not active",
        )

    return await public_display_payload(clinic_id, branch_id=branch_id)
