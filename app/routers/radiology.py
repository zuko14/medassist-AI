"""Radiology Information System and PCPNDT Form F API (G13, migration 123).

/admin/radiology/worklist                       imaging worklist (pending / completed, by modality)
/admin/radiology/orders/{id}                    reporting console for an order (latest report or a new draft)
/admin/radiology/reports                        save the draft for an order (or open an amendment)
/admin/radiology/reports/{id}                   report, PACS link, Form F, versions
/admin/radiology/reports/{id}/finalize          radiologist signs; order completed; PDF to WhatsApp
/admin/radiology/reports/{id}/pdf               signed report PDF
/admin/radiology/reports/{id}/pcpndt-form-f     record + lock the statutory Form F
/admin/radiology/reports/{id}/pcpndt-form-f/pdf Form F PDF

Permissions: RADIOLOGY_READ / RADIOLOGY_WRITE (app.services.permissions.holds_radiology_permission).
Form F and signing additionally need a login linked to a doctor with a registration number.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
from datetime import date
from decimal import Decimal
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
from app.routers.opd import _read_branch
from app.services import orders, radiology
from app.services.permissions import holds_radiology_permission
from app.services.tenant import get_clinic_by_id, ipd_access, opd_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/radiology", tags=["Radiology"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
Modality = Literal["x_ray", "ultrasound", "ct", "mri", "mammography", "dexa", "fluoroscopy", "other"]
Indication = Literal["dating_scan_early_pregnancy", "first_trimester_screening", "anomaly_scan_target",
                     "fetal_growth_biometry", "liquor_placental_evaluation", "suspected_ectopic_pregnancy",
                     "suspected_molar_pregnancy", "fetal_viability_bleeding", "multiple_gestation_chorionicity",
                     "other_medical_indication"]


# ── models ──────────────────────────────────────────────────────────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportIn(_In):
    order_id: str = Field(..., pattern=_UUID)
    modality: Optional[Modality] = None
    procedure_name: Optional[str] = Field(None, max_length=200)
    is_obstetric: bool = False
    clinical_history: Optional[str] = Field(None, max_length=1000)
    findings: str = Field("", max_length=8000)
    impression: str = Field("", max_length=2000)
    recommendations: Optional[str] = Field(None, max_length=1000)
    pacs_study_uid: Optional[str] = Field(None, max_length=64, pattern=r"^[0-9]+(\.[0-9]+)*$")
    # http(s) only: the panel opens it in a new tab, never a javascript: link
    pacs_viewer_url: Optional[str] = Field(None, max_length=500, pattern=r"^https?://[^\s<>\"']+$")
    amendment_reason: Optional[str] = Field(None, max_length=300)
    expected_updated_at: Optional[str] = Field(None, min_length=10, max_length=40)

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        if isinstance(data, dict):
            for k in ("pacs_study_uid", "pacs_viewer_url", "amendment_reason", "expected_updated_at", "modality"):
                if isinstance(data.get(k), str) and not data[k].strip():
                    data[k] = None
        return data


class FinalizeIn(_In):
    expected_updated_at: str = Field(..., min_length=10, max_length=40)


class FormFIn(_In):
    patient_age: int = Field(..., ge=10, le=60)
    relative_name: str = Field(..., min_length=2, max_length=100)
    referred_by: Optional[str] = Field(None, max_length=150)
    num_living_children: int = Field(0, ge=0, le=20)
    num_female_children: int = Field(0, ge=0, le=20)
    num_male_children: int = Field(0, ge=0, le=20)
    last_menstrual_period: date
    gestational_age_weeks: Decimal = Field(..., ge=Decimal("1.0"), le=Decimal("45.0"), decimal_places=1)
    clinical_indication: Indication
    indication_notes: Optional[str] = Field(None, max_length=300)
    invasive_procedure_done: bool = False
    pregnant_woman_declaration_signed: bool
    doctor_declaration_signed: bool

    @model_validator(mode="after")
    def _shape(self):
        if not self.relative_name.strip():
            raise ValueError("Husband's / father's name is required.")
        if self.num_female_children + self.num_male_children != self.num_living_children:
            raise ValueError("Sons and daughters must add up to the number of living children.")
        if not (self.pregnant_woman_declaration_signed and self.doctor_declaration_signed):
            raise ValueError("Both declarations (pregnant woman and doctor) are required.")
        if self.clinical_indication == "other_medical_indication" and len((self.indication_notes or "").strip()) < 5:
            raise ValueError("Describe the other medical indication (5+ characters).")
        return self


# ── guards ──────────────────────────────────────────────────────────────────

async def _scope(user: AdminUser, clinic_id: str, perm: str) -> str:
    """Permission, tenant, and OPD or IPD entitlement."""
    if not holds_radiology_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or not (opd_enabled(clinic) or ipd_access(clinic)):
        raise HTTPException(status_code=403, detail="Radiology needs the OPD or IPD module.")
    return scope


async def _report_in_branch(user: AdminUser, scope: str, report_id: str) -> dict:
    report = await radiology.get_report(scope, report_id)
    enforce_branch_scope(user, report.get("branch_id"))
    return report


async def _audit(user: AdminUser, request: Request, action: str, rid: Optional[str], details=None,
                 rtype: str = "radiology_report"):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


def _pdf(content: bytes, filename: str) -> Response:
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"})


# ── worklist & console ──────────────────────────────────────────────────────

@router.get("/worklist")
async def worklist(modality: Optional[Literal["x_ray", "ultrasound", "ct_mri", "ct", "mri", "mammography", "dexa",
                                              "fluoroscopy", "other"]] = None,
                   status_filter: Literal["pending", "completed"] = Query("pending", alias="status"),
                   branch_id: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0, le=100_000), clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_READ")
    return await radiology.get_radiology_worklist(scope, _read_branch(user, branch_id), modality, status_filter,
                                                  limit, offset)


@router.get("/orders/{order_id}")
async def order_workspace(order_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_READ")
    order = await orders.get_order(scope, order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    return await radiology.get_order_workspace(scope, order_id)


# ── reports ─────────────────────────────────────────────────────────────────

@router.post("/reports")
async def save_report(body: ReportIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_WRITE")
    order = await orders.get_order(scope, body.order_id)
    enforce_branch_scope(user, order.get("branch_id"))
    data = body.model_dump(exclude={"order_id", "amendment_reason", "expected_updated_at"})
    saved = await radiology.create_or_update_report(scope, body.order_id, data, user, body.amendment_reason,
                                                    body.expected_updated_at)
    if saved.get("created"):
        await _audit(user, request, "RADIOLOGY_REPORT_CREATE" if saved["version"] == 1 else "RADIOLOGY_REPORT_AMEND",
                     saved["id"], {"order_id": body.order_id, "version": saved["version"],
                                   "report_number": saved.get("report_number"), "modality": saved["modality"],
                                   "is_obstetric": saved["is_obstetric"]})
    return saved


@router.get("/reports/{report_id}")
async def get_report(report_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_READ")
    await _report_in_branch(user, scope, report_id)
    return await radiology.get_report_detail(scope, report_id)


@router.post("/reports/{report_id}/finalize")
async def finalize(report_id: str, body: FinalizeIn, request: Request, clinic_id: str = "default",
                   user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_WRITE")
    await _report_in_branch(user, scope, report_id)
    signed = await radiology.finalize_report(scope, report_id, body.expected_updated_at, user)
    if not signed.get("replayed"):
        await _audit(user, request, "RADIOLOGY_REPORT_SIGN", report_id,
                     {"order_id": signed["order_id"], "version": signed["version"],
                      "report_number": signed.get("report_number"), "is_obstetric": signed["is_obstetric"],
                      "supersedes_id": signed.get("supersedes_id")})
    return signed


@router.get("/reports/{report_id}/pdf")
async def report_pdf(report_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_READ")
    await _report_in_branch(user, scope, report_id)
    pdf, report = await radiology.render_report(scope, report_id)
    return _pdf(pdf, radiology.report_filename(report))


# ── PCPNDT Form F ───────────────────────────────────────────────────────────

@router.post("/reports/{report_id}/pcpndt-form-f")
async def record_form_f(report_id: str, body: FormFIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "RADIOLOGY_WRITE")
    await _report_in_branch(user, scope, report_id)
    form = await radiology.save_pcpndt_form_f(scope, report_id, body.model_dump(), user)
    if not form.get("replayed"):
        # no maternal history in the audit row: the record itself is the statutory register
        await _audit(user, request, "PCPNDT_FORM_F_LOCK", form["id"],
                     {"report_id": report_id, "order_id": form["order_id"], "form_f_number": form["form_f_number"]},
                     rtype="pcpndt_form_f")
    return form


@router.get("/reports/{report_id}/pcpndt-form-f/pdf")
async def form_f_pdf(report_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    # Form F is a statutory register entry: the writers' grant, not every reader of reports
    scope = await _scope(user, clinic_id, "RADIOLOGY_WRITE")
    await _report_in_branch(user, scope, report_id)
    pdf, form = await radiology.render_form_f(scope, report_id)
    return _pdf(pdf, radiology.form_f_filename(form))
