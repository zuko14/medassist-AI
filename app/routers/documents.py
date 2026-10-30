"""Clinical Documents API (G9, migration 119).

/admin/ipd/admissions/{id}/discharge-summary/pdf   signed discharge summary: download / send on WhatsApp
/admin/clinical/consents                           recorded informed consents
/admin/clinical/certificates                       numbered medical / fitness certificates

Permissions: DOCUMENTS_READ / DOCUMENTS_WRITE (app.services.permissions.holds_document_permission).
Issuing a certificate also needs a login linked to a doctor with a registration number.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
import re
from datetime import date
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
from app.services import clinical_documents as docs
from app.services import ipd
from app.services.opd_pdf import render_certificate_pdf, render_consent_pdf
from app.services.permissions import holds_document_permission
from app.services.tenant import get_clinic_by_id, ipd_access, opd_enabled

logger = logging.getLogger(__name__)

ipd_docs_router = APIRouter(prefix="/admin/ipd", tags=["Clinical Documents"])
router = APIRouter(prefix="/admin/clinical", tags=["Clinical Documents"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_KEY = r"^[A-Za-z0-9_-]{8,72}$"


# ── models ──────────────────────────────────────────────────────────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentIn(_In):
    admission_id: Optional[str] = Field(None, pattern=_UUID)
    patient_id: Optional[str] = Field(None, pattern=_UUID)
    family_member_id: Optional[str] = Field(None, pattern=_UUID)
    consent_type: Literal["general_admission", "surgical", "anesthesia", "high_risk_procedure", "blood_transfusion",
                          "refusal_lama"]
    language: Literal["en", "hi", "te"] = "en"
    title: str = Field(..., min_length=3, max_length=200)
    procedure_name: Optional[str] = Field(None, max_length=200)
    body_text: str = Field(..., min_length=20, max_length=8000)
    risks_explained: Optional[str] = Field(None, max_length=4000)
    doctor_id: Optional[str] = Field(None, pattern=_UUID)
    signer_relationship: Literal["self", "parent", "spouse", "guardian", "relative", "other"] = "self"
    signer_name: str = Field(..., min_length=1, max_length=120)
    witness_name: Optional[str] = Field(None, min_length=1, max_length=120)
    witness_phone: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    idempotency_key: Optional[str] = Field(None, pattern=_KEY)

    @model_validator(mode="after")
    def _shape(self):
        if not (self.admission_id or self.patient_id):
            raise ValueError("Give the admission or the patient.")
        if self.signer_relationship != "self" and not (self.witness_name or "").strip():
            raise ValueError("A consent signed by a representative needs a witness.")
        if not self.signer_name.strip() or not self.title.strip():
            raise ValueError("Signer name and title are required.")
        return self


class CertificateIn(_In):
    patient_id: str = Field(..., pattern=_UUID)
    family_member_id: Optional[str] = Field(None, pattern=_UUID)
    encounter_id: Optional[str] = Field(None, pattern=_UUID)
    admission_id: Optional[str] = Field(None, pattern=_UUID)
    certificate_type: Literal["medical_leave", "fitness", "travel_fitness", "disability_assessment",
                              "birth_intimation", "death_intimation"]
    diagnosis_text: str = Field(..., min_length=3, max_length=2000)
    rest_days_advised: Optional[int] = Field(None, ge=0, le=365)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    remarks: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, pattern=_KEY)

    @model_validator(mode="after")
    def _shape(self):
        if self.certificate_type == "medical_leave" and not (self.start_date and self.end_date):
            raise ValueError("A medical leave certificate needs the leave start and end dates.")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("The end date is before the start date.")
        if self.start_date and self.end_date and (self.end_date - self.start_date).days > 365:
            raise ValueError("A certificate covers at most one year.")
        return self


class VoidIn(_In):
    reason: str = Field(..., min_length=5, max_length=300)


# ── guards ──────────────────────────────────────────────────────────────────

async def _docs_scope(user: AdminUser, clinic_id: str, perm: str, ipd_only: bool = False) -> str:
    """Permission, tenant, and OPD or IPD entitlement (IPD for inpatient documents)."""
    if not holds_document_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or (ipd_access(clinic) is None if ipd_only else not (opd_enabled(clinic) or ipd_access(clinic))):
        raise HTTPException(status_code=403, detail="Clinical documents need the OPD or IPD module."
                            if not ipd_only else "The IPD module is not enabled for this clinic.")
    return scope


async def _admission(user: AdminUser, scope: str, admission_id: str) -> dict:
    a = await ipd.get_admission_row(scope, admission_id)
    enforce_branch_scope(user, a.get("branch_id"))
    return a


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


def _pdf(content: bytes, filename: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}"', "Cache-Control": "no-store"})


# ── discharge summary ───────────────────────────────────────────────────────

@ipd_docs_router.get("/admissions/{admission_id}/discharge-summary/pdf")
async def discharge_summary_pdf(admission_id: str, version: Optional[int] = Query(None, ge=1, le=99),
                                clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_READ", ipd_only=True)
    await _admission(user, scope, admission_id)
    pdf, row = await docs.render_summary(scope, admission_id, version)
    return _pdf(pdf, docs.summary_filename(row))


@ipd_docs_router.post("/admissions/{admission_id}/discharge-summary/pdf")
async def send_discharge_summary(admission_id: str, request: Request, clinic_id: str = "default",
                                 user: AdminUser = Depends(verify_credentials)):
    """Store the signed summary's PDF and send it to the patient's WhatsApp now."""
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_WRITE", ipd_only=True)
    await _admission(user, scope, admission_id)
    out = await docs.send_discharge_summary(scope, admission_id)
    await _audit(user, request, "DOCS_DISCHARGE_SUMMARY_SEND", "ipd_admission", admission_id, out)
    return out


# ── consents ────────────────────────────────────────────────────────────────

@router.get("/consents")
async def list_consents(admission_id: Optional[str] = Query(None, pattern=_UUID),
                        patient_id: Optional[str] = Query(None, pattern=_UUID), clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_READ")
    if not (admission_id or patient_id):
        raise HTTPException(status_code=422, detail="Give admission_id or patient_id.")
    if admission_id:
        await _admission(user, scope, admission_id)
    rows = await docs.list_consents(scope, admission_id, patient_id)
    return {"consents": [r for r in rows if not _hidden(user, r)]}


def _hidden(user: AdminUser, row: dict) -> bool:
    """Branch-pinned staff see their branch's documents and branch-less ones."""
    pinned = getattr(user, "branch_id", None) and user.role not in ("super_admin", "clinic_admin")
    return bool(pinned and row.get("branch_id") and str(row["branch_id"]) != str(user.branch_id))


@router.post("/consents", status_code=201)
async def create_consent(body: ConsentIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_WRITE", ipd_only=bool(body.admission_id))
    if body.admission_id:
        await _admission(user, scope, body.admission_id)
    row = await docs.create_consent(scope, body.model_dump(), user)
    if not row.get("replayed"):
        await _audit(user, request, "DOCS_CONSENT_RECORD", "clinical_consent", row["id"],
                     {"type": body.consent_type, "admission_id": body.admission_id,
                      "relationship": body.signer_relationship})
    return row


@router.get("/consents/{consent_id}/pdf")
async def consent_pdf(consent_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_READ")
    row = await docs.get_consent(scope, consent_id)
    enforce_branch_scope(user, row.get("branch_id"))
    return _pdf(render_consent_pdf(row), f"Consent_{row['consent_type']}_{row['id'][:8]}.pdf")


# ── certificates ────────────────────────────────────────────────────────────

@router.get("/certificates")
async def list_certificates(patient_id: Optional[str] = Query(None, pattern=_UUID),
                            encounter_id: Optional[str] = Query(None, pattern=_UUID), clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_READ")
    if not (patient_id or encounter_id):
        raise HTTPException(status_code=422, detail="Give patient_id or encounter_id.")
    rows = await docs.list_certificates(scope, patient_id, encounter_id)
    return {"certificates": [r for r in rows if not _hidden(user, r)]}


@router.post("/certificates", status_code=201)
async def issue_certificate(body: CertificateIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_WRITE")
    if body.admission_id:
        await _admission(user, scope, body.admission_id)
    cert = await docs.issue_medical_certificate(scope, body.patient_id, body.model_dump(), user)
    if not cert.get("replayed"):
        await _audit(user, request, "DOCS_CERTIFICATE_ISSUE", "clinical_certificate", cert["id"],
                     {"number": cert["certificate_number"], "type": body.certificate_type})
    return cert


@router.get("/certificates/{certificate_id}/pdf")
async def certificate_pdf(certificate_id: str, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_READ")
    cert = await docs.get_certificate(scope, certificate_id)
    enforce_branch_scope(user, cert.get("branch_id"))
    return _pdf(render_certificate_pdf(cert), docs.certificate_filename(cert))


@router.post("/certificates/{certificate_id}/void")
async def void_certificate(certificate_id: str, body: VoidIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_WRITE")
    cert = await docs.get_certificate(scope, certificate_id)
    enforce_branch_scope(user, cert.get("branch_id"))
    out = await docs.void_certificate(scope, certificate_id, body.reason, user)
    await _audit(user, request, "DOCS_CERTIFICATE_VOID", "clinical_certificate", certificate_id,
                 {"number": cert["certificate_number"], "reason": body.reason})
    return out


@router.post("/certificates/{certificate_id}/send")
async def send_certificate(certificate_id: str, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope = await _docs_scope(user, clinic_id, "DOCUMENTS_WRITE")
    cert = await docs.get_certificate(scope, certificate_id)
    enforce_branch_scope(user, cert.get("branch_id"))
    out = await docs.send_certificate(scope, certificate_id)
    await _audit(user, request, "DOCS_CERTIFICATE_SEND", "clinical_certificate", certificate_id, out)
    return out
