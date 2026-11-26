"""Kriya Specialty OS API — /admin/sp (migrations 151–155; docs/specialty-os/README.md).

Every route: SP permission → enforce_clinic_access → sp_access(clinic[, pack]) → service. A clinic without the module
gets 403 before any SP query (zero impact). Clinical content is entered and decided by clinicians; Kriya validates,
computes only arithmetic on entered values, stores, prints and messages on explicit staff action. Zero-LLM.
"""

import uuid
from datetime import date, datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import sp, sp_commerce, sp_eye_dental, sp_forms, sp_ivf_wc, sp_wc
from app.services.permissions import holds_sp_permission
from app.services.tenant import SP_PACKS, get_clinic_by_id, sp_access

router = APIRouter(prefix="/admin/sp", tags=["Specialty OS"])
Name = Field(..., min_length=1, max_length=120)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _scope(user: AdminUser, clinic_id: Optional[str], perm: str, pack: Optional[str] = None) -> tuple[str, dict]:
    if not holds_sp_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not sp_access(clinic):
        raise HTTPException(status_code=403, detail="Specialty OS is not enabled for this clinic.")
    if pack and not sp_access(clinic, pack):
        raise HTTPException(status_code=403, detail="This specialty module is not enabled for this clinic.")
    return scope, clinic


async def _audit(user, action: str, rtype: str, rid: Optional[str], details: Optional[dict] = None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid, details=details or {})


# ── module status ────────────────────────────────────────────────────────────

@router.get("/status")
async def sp_status(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_CLINICAL" if holds_sp_permission(user, "SP_CLINICAL") else "SP_DESK")
    packs = {p: sp_access(clinic, p) for p in SP_PACKS}
    return {"packs": packs, "forms": [{"code": c, "title": f[3], "pack": f[1]} for c, f in sp_forms.FORMS.items()
                                      if packs.get(f[1])],
            "consent_templates": [{"code": k, "title": v["title"]["en"], "specialty": v["specialty"]}
                                  for k, v in sp.CONSENT_TEMPLATES.items()],
            "can": {k: holds_sp_permission(user, k) for k in ("SP_CLINICAL", "SP_DESK", "SP_ADMIN")}}


# ── S2 forms ─────────────────────────────────────────────────────────────────

class FormIn(_M):
    data: dict[str, Any]
    staff_confirmed_primary: bool = False


@router.get("/encounters/{encounter_id}/forms")
async def get_forms(encounter_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    return {"forms": await sp.list_forms(scope, str(encounter_id))}


@router.put("/encounters/{encounter_id}/forms/{form_code}")
async def put_form(encounter_id: uuid.UUID, form_code: str, body: FormIn, clinic_id: Optional[str] = None,
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", sp.form_pack(form_code))
    return await sp.save_form(scope, str(encounter_id), form_code, body.data, user, body.staff_confirmed_primary)


@router.delete("/encounters/{encounter_id}/forms/{form_code}")
async def del_form(encounter_id: uuid.UUID, form_code: str, clinic_id: Optional[str] = None,
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", sp.form_pack(form_code))
    await sp.delete_form(scope, str(encounter_id), form_code)
    return {"deleted": True}


@router.get("/patients/{patient_id}/forms/{form_code}")
async def form_history(patient_id: uuid.UUID, form_code: str, clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", sp.form_pack(form_code))
    return {"forms": await sp.patient_forms(scope, str(patient_id), form_code)}


# ── S1 media vault ───────────────────────────────────────────────────────────

class MediaConsentIn(_M):
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    allow_share: bool = False
    method: Literal["counter_signed", "whatsapp_button"] = "counter_signed"
    signer_name: str = Name
    signer_relationship: Literal["self", "parent", "spouse", "guardian", "relative", "other"] = "self"


class ReasonIn(_M):
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("/media/consents", status_code=201)
async def media_consent(body: MediaConsentIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.grant_media_consent(scope, body.model_dump(mode="json"), user)
    await _audit(user, "SP_MEDIA_CONSENT", "sp_media_consent", out["id"], {"allow_share": body.allow_share})
    return out


@router.post("/media/consents/{consent_id}/withdraw")
async def media_consent_withdraw(consent_id: uuid.UUID, body: ReasonIn, clinic_id: Optional[str] = None,
                                 user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.withdraw_media_consent(scope, str(consent_id), body.reason)
    await _audit(user, "SP_MEDIA_CONSENT_WITHDRAW", "sp_media_consent", str(consent_id))
    return out


@router.get("/patients/{patient_id}/media")
async def media_list(patient_id: uuid.UUID, context: Optional[Literal["derma", "dental", "eye", "wound", "general"]] = None,
                     clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.list_media(scope, str(patient_id), context)
    await _audit(user, "SP_MEDIA_VIEW", "patient", str(patient_id), {"count": len(out)})
    return {"media": out}


@router.post("/patients/{patient_id}/media", status_code=201)
async def media_upload(patient_id: uuid.UUID, file: UploadFile = File(...),
                       context: Literal["derma", "dental", "eye", "wound", "general"] = Form("general"),
                       angle: str = Form("front"), body_site: Optional[str] = Form(None),
                       session_no: Optional[int] = Form(None, ge=1, le=100), encounter_id: Optional[uuid.UUID] = Form(None),
                       package_sale_id: Optional[uuid.UUID] = Form(None), clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    raw = await file.read(sp.MEDIA_MAX_BYTES + 1)
    out = await sp.upload_media(scope, str(patient_id), raw, {
        "context": context, "angle": angle, "body_site": (body_site or None), "session_no": session_no,
        "encounter_id": str(encounter_id) if encounter_id else None,
        "package_sale_id": str(package_sale_id) if package_sale_id else None}, user)
    await _audit(user, "SP_MEDIA_UPLOAD", "sp_media", out["id"], {"context": context})
    return {k: v for k, v in out.items() if k != "storage_path"}


@router.get("/patients/{patient_id}/media/compare")
async def media_compare(patient_id: uuid.UUID, before: uuid.UUID, after: uuid.UUID, clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.compare(scope, str(patient_id), str(before), str(after))
    await _audit(user, "SP_MEDIA_VIEW", "patient", str(patient_id), {"compare": True})
    return out


@router.post("/media/{media_id}/share")
async def media_share(media_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.share_media(clinic, str(media_id), user)
    await _audit(user, "SP_MEDIA_SHARE", "sp_media", str(media_id))
    return out


@router.post("/media/{media_id}/delete")
async def media_delete(media_id: uuid.UUID, body: ReasonIn, clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.delete_media(scope, str(media_id), body.reason)
    await _audit(user, "SP_MEDIA_DELETE", "sp_media", str(media_id))
    return out


# ── S6 imaging links ─────────────────────────────────────────────────────────

class ImagingIn(_M):
    modality: Literal["oct", "fundus", "perimetry", "biometry", "topography", "rvg", "opg", "cbct", "usg", "dermoscopy", "other"]
    viewer_url: str = Field(..., pattern=r"^https://\S{4,}$", max_length=1000)
    device_id: Optional[str] = Field(None, max_length=60)
    label: Optional[str] = Field(None, max_length=120)


@router.post("/encounters/{encounter_id}/imaging", status_code=201)
async def imaging_add(encounter_id: uuid.UUID, body: ImagingIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    return await sp.add_imaging_link(scope, str(encounter_id), body.model_dump(), user)


@router.get("/encounters/{encounter_id}/imaging")
async def imaging_list(encounter_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    return {"links": await sp.list_imaging_links(scope, encounter_id=str(encounter_id))}


# ── S4 consent templates ─────────────────────────────────────────────────────

class TemplateConsentIn(_M):
    template: str = Field(..., pattern=r"^[a-z_]{3,40}$")
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    language: Literal["en", "hi", "te"] = "en"
    signer_name: str = Name
    signer_relationship: Literal["self", "parent", "spouse", "guardian", "relative", "other"] = "self"
    witness_name: Optional[str] = Field(None, max_length=120)
    doctor_id: Optional[uuid.UUID] = None
    procedure_detail: Optional[str] = Field(None, max_length=200)
    risks_explained: Optional[str] = Field(None, max_length=4000)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=80)


@router.post("/consents", status_code=201)
async def template_consent(body: TemplateConsentIn, clinic_id: Optional[str] = None,
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL")
    out = await sp.record_template_consent(scope, body.model_dump(mode="json"), user)
    await _audit(user, "SP_CONSENT", "clinical_consent", out.get("id"), {"template": body.template})
    return out


# ── S3 packages & instalments ────────────────────────────────────────────────

class PackageIn(_M):
    code: str = Field(..., pattern=r"^[A-Z0-9_-]{2,20}$")
    name: str = Field(..., min_length=2, max_length=120)
    specialty: Literal["derma", "eye", "dental", "ivf", "womenchild", "general"]
    service_code: str = Field(..., min_length=1, max_length=40)
    sessions_total: int = Field(..., ge=1, le=100)
    validity_days: int = Field(365, ge=1, le=1095)


class PackagePatch(_M):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    service_code: Optional[str] = Field(None, min_length=1, max_length=40)
    sessions_total: Optional[int] = Field(None, ge=1, le=100)
    validity_days: Optional[int] = Field(None, ge=1, le=1095)
    is_active: Optional[bool] = None


class SaleIn(_M):
    package_id: uuid.UUID
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None


class ConsumeIn(_M):
    appointment_id: Optional[uuid.UUID] = None
    encounter_id: Optional[uuid.UUID] = None


class InstalmentIn(_M):
    due_on: date
    amount_paise: int = Field(..., gt=0)


class InstalmentPlanIn(_M):
    instalments: list[InstalmentIn] = Field(..., min_length=1, max_length=60)


@router.get("/packages")
async def packages(active: bool = False, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"packages": await sp_commerce.list_packages(scope, active)}


@router.post("/packages", status_code=201)
async def package_create(body: PackageIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    out = await sp_commerce.create_package(scope, body.model_dump())
    await _audit(user, "SP_PACKAGE_CREATE", "sp_package", out["id"], {"code": body.code})
    return out


@router.patch("/packages/{package_id}")
async def package_update(package_id: uuid.UUID, body: PackagePatch, clinic_id: Optional[str] = None,
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    return await sp_commerce.update_package(scope, str(package_id), body.model_dump(exclude_none=True))


@router.post("/package-sales", status_code=201)
async def package_sell(body: SaleIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.sell_package(scope, body.model_dump(mode="json"), user)
    await _audit(user, "SP_PACKAGE_SELL", "sp_package_sale", out["id"], {"invoice_id": out["invoice_id"]})
    return out


@router.get("/patients/{patient_id}/package-sales")
async def package_sales(patient_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"sales": await sp_commerce.list_sales(scope, str(patient_id))}


@router.post("/package-sales/{sale_id}/consume", status_code=201)
async def package_consume(sale_id: uuid.UUID, body: ConsumeIn, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.consume(scope, str(sale_id), body.model_dump(mode="json"), user)
    await _audit(user, "SP_PACKAGE_CONSUME", "sp_package_sale", str(sale_id), {"session_no": out["session_no"]})
    return out


@router.post("/package-consumptions/{consumption_id}/reverse")
async def package_reverse(consumption_id: uuid.UUID, body: ReasonIn, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    out = await sp_commerce.reverse_consumption(scope, str(consumption_id), body.reason)
    await _audit(user, "SP_PACKAGE_REVERSE", "sp_package_consumption", str(consumption_id))
    return out


@router.post("/package-sales/{sale_id}/whatsapp-balance")
async def package_balance(sale_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_DESK")
    return await sp_commerce.send_balance(clinic, str(sale_id))


@router.put("/invoices/{invoice_id}/instalments")
async def instalments_set(invoice_id: uuid.UUID, body: InstalmentPlanIn, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.set_instalments(scope, str(invoice_id), [i.model_dump() for i in body.instalments], user)
    await _audit(user, "SP_INSTALMENTS_SET", "opd_invoice", str(invoice_id), {"count": len(out)})
    return {"instalments": out}


@router.get("/invoices/{invoice_id}/instalments")
async def instalments_get(invoice_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"instalments": await sp_commerce.instalments(scope, str(invoice_id))}


@router.get("/instalments/due")
async def instalments_due(within_days: int = Query(7, ge=0, le=90), clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"instalments": await sp_commerce.instalments_due(scope, within_days)}


# ── S5 estimates ─────────────────────────────────────────────────────────────

class EstLineIn(_M):
    label: str = Field(..., min_length=1, max_length=80)
    amount_paise: int = Field(..., ge=0, le=10_000_000_00)


class EstOptionIn(_M):
    code: str = Field(..., pattern=r"^[a-z0-9_]{1,20}$")
    label: str = Field(..., min_length=1, max_length=40)
    lines: list[EstLineIn] = Field(..., min_length=1, max_length=20)


class EstimateIn(_M):
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    specialty: Literal["derma", "eye", "dental", "ivf", "womenchild", "general"]
    title: str = Field(..., min_length=3, max_length=120)
    options: list[EstOptionIn] = Field(..., min_length=1, max_length=4)
    valid_until: date
    dental_plan_id: Optional[uuid.UUID] = None


class EstRespondIn(_M):
    option: Optional[str] = Field(None, pattern=r"^[a-z0-9_]{1,20}$")         # None = declined


class EstConvertIn(_M):
    package_id: uuid.UUID


@router.post("/estimates", status_code=201)
async def estimate_create(body: EstimateIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return await sp_commerce.create_estimate(scope, body.model_dump(mode="json") | {"valid_until": body.valid_until}, user)


@router.get("/estimates")
async def estimate_list(patient_id: Optional[uuid.UUID] = None,
                        status: Optional[Literal["draft", "sent", "accepted", "declined", "expired", "converted"]] = None,
                        clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"estimates": await sp_commerce.list_estimates(scope, str(patient_id) if patient_id else None, status)}


@router.post("/estimates/{estimate_id}/send")
async def estimate_send(estimate_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.send_estimate(clinic, str(estimate_id))
    await _audit(user, "SP_ESTIMATE_SEND", "sp_estimate", str(estimate_id))
    return out


@router.post("/estimates/{estimate_id}/respond")
async def estimate_respond(estimate_id: uuid.UUID, body: EstRespondIn, clinic_id: Optional[str] = None,
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.staff_respond(scope, str(estimate_id), body.option)
    await _audit(user, "SP_ESTIMATE_RESPOND", "sp_estimate", str(estimate_id), {"option": body.option})
    return out


@router.post("/estimates/{estimate_id}/convert", status_code=201)
async def estimate_convert(estimate_id: uuid.UUID, body: EstConvertIn, clinic_id: Optional[str] = None,
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    out = await sp_commerce.convert_estimate(scope, str(estimate_id), str(body.package_id), user)
    await _audit(user, "SP_ESTIMATE_CONVERT", "sp_estimate", str(estimate_id), {"sale_id": out["id"]})
    return out


# ── S9 camps ─────────────────────────────────────────────────────────────────

class CampIn(_M):
    name: str = Field(..., min_length=3, max_length=120)
    kind: Literal["cataract", "dental", "skin", "health", "immunisation", "other"]
    camp_date: date
    location: str = Field(..., min_length=3, max_length=200)
    branch_id: Optional[uuid.UUID] = None


class CampRegIn(_M):
    name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    age_years: Optional[int] = Field(None, ge=0, le=120)
    patient_id: Optional[uuid.UUID] = None


class CampScreenIn(_M):
    screening: dict[str, Any] = Field(default_factory=dict)
    outcome: Literal["screened_ok", "referred", "not_referred"]
    referral_reason: Optional[str] = Field(None, min_length=3, max_length=300)
    patient_id: Optional[uuid.UUID] = None
    appointment_id: Optional[uuid.UUID] = None


@router.post("/camps", status_code=201)
async def camp_create(body: CampIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    return await sp.create_camp(scope, body.model_dump(mode="json"), user)


@router.get("/camps")
async def camp_list(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"camps": await sp.list_camps(scope)}


@router.post("/camps/{camp_id}/registrations", status_code=201)
async def camp_register(camp_id: uuid.UUID, body: CampRegIn, clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return await sp.register_at_camp(scope, str(camp_id), body.model_dump(mode="json"), user)


@router.post("/camp-registrations/{registration_id}/screen")
async def camp_screen(registration_id: uuid.UUID, body: CampScreenIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return await sp.screen_at_camp(scope, str(registration_id), body.model_dump(mode="json"))


@router.get("/camps/{camp_id}/report")
async def camp_report(camp_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return await sp.camp_report(scope, str(camp_id))


# ── Eye: optical job board + spectacle Rx ────────────────────────────────────

class OpticalJobIn(_M):
    form_id: uuid.UUID
    lens_desc: str = Field(..., min_length=2, max_length=200)
    frame_desc: Optional[str] = Field(None, max_length=200)
    lab_name: Optional[str] = Field(None, max_length=120)
    due_on: Optional[date] = None
    total_paise: int = Field(..., ge=0, le=10_000_000)
    advance_paise: int = Field(0, ge=0, le=10_000_000)
    frame_item_id: Optional[uuid.UUID] = None
    lens_item_id: Optional[uuid.UUID] = None


class OpticalMoveIn(_M):
    to: Literal["advance_paid", "sent_to_lab", "received", "qc_passed", "ready", "delivered", "remake", "cancelled"]
    reason: Optional[str] = Field(None, min_length=3, max_length=300)
    advance_paise: Optional[int] = Field(None, gt=0, le=10_000_000)
    notify: bool = False                       # "Ready": message the patient — only after the staff confirm dialog


@router.post("/optical/jobs", status_code=201)
async def optical_create(body: OpticalJobIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK", "eye")
    out = await sp_eye_dental.create_job(scope, body.model_dump(mode="json") | {"due_on": body.due_on}, user)
    await _audit(user, "SP_OPTICAL_JOB_CREATE", "sp_optical_job", out["id"])
    return out


@router.get("/optical/board")
async def optical_board(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK", "eye")
    return await sp_eye_dental.board(scope)


@router.post("/optical/jobs/{job_id}/move")
async def optical_move(job_id: uuid.UUID, body: OpticalMoveIn, clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_DESK", "eye")
    out = await sp_eye_dental.move_job(clinic, str(job_id), body.to, user, body.reason, body.advance_paise, body.notify)
    await _audit(user, "SP_OPTICAL_JOB_" + body.to.upper(), "sp_optical_job", str(job_id), {"notify": body.notify})
    return out


@router.get("/forms/{form_id}/spectacle-rx.pdf")
async def spectacle_rx(form_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_DESK", "eye")
    pdf = await sp_eye_dental.spectacle_rx_pdf(clinic, str(form_id))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": "inline; filename=spectacle-rx.pdf",
                                                                "Cache-Control": "no-store"})


# ── Dental: lab orders · compliance dates ────────────────────────────────────

class LabOrderIn(_M):
    patient_id: uuid.UUID
    plan_id: Optional[uuid.UUID] = None
    lab_name: str = Field(..., min_length=2, max_length=120)
    work_type: Literal["crown", "bridge", "denture", "aligner", "implant_crown", "veneer", "night_guard", "other"]
    teeth: list[str] = Field(..., min_length=1, max_length=32)
    shade: Optional[str] = Field(None, max_length=20)
    due_on: Optional[date] = None
    cost_paise: Optional[int] = Field(None, ge=0, le=10_000_000)


class LabMoveIn(_M):
    to: Literal["received", "fitted", "remake", "cancelled"]
    reason: Optional[str] = Field(None, min_length=3, max_length=300)


class ComplianceIn(_M):
    kind: Literal["aerb_registration", "bmw_authorisation", "fire_noc", "pcpndt_registration", "art_registration",
                  "drug_licence", "other"]
    label: str = Field(..., min_length=3, max_length=120)
    reference_no: Optional[str] = Field(None, max_length=80)
    valid_until: date
    remind_days: int = Field(60, ge=7, le=180)


@router.post("/dental/lab-orders", status_code=201)
async def lab_create(body: LabOrderIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "dental")
    return await sp_eye_dental.create_lab_order(scope, body.model_dump(mode="json") | {"due_on": body.due_on}, user)


@router.get("/dental/lab-orders")
async def lab_list(status: Optional[Literal["sent", "received", "fitted", "remake", "cancelled"]] = None,
                   clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "dental")
    return {"orders": await sp_eye_dental.list_lab_orders(scope, status)}


@router.post("/dental/lab-orders/{order_id}/move")
async def lab_move(order_id: uuid.UUID, body: LabMoveIn, clinic_id: Optional[str] = None,
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "dental")
    return await sp_eye_dental.move_lab_order(scope, str(order_id), body.to, body.reason)


@router.get("/compliance")
async def compliance_list(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK")
    return {"items": await sp_eye_dental.list_compliance(scope)}


@router.post("/compliance", status_code=201)
async def compliance_create(body: ComplianceIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    return await sp_eye_dental.upsert_compliance(scope, body.model_dump(), user)


@router.patch("/compliance/{item_id}")
async def compliance_update(item_id: uuid.UUID, body: ComplianceIn, clinic_id: Optional[str] = None,
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN")
    return await sp_eye_dental.upsert_compliance(scope, body.model_dump(), user, str(item_id))


# ── IVF ──────────────────────────────────────────────────────────────────────

IdType = Literal["aadhaar", "passport", "voter_id", "driving_licence", "pan"]


class CoupleIn(_M):
    female_patient_id: uuid.UUID
    male_patient_id: Optional[uuid.UUID] = None
    donor_code: Optional[str] = Field(None, pattern=r"^[A-Z0-9-]{3,30}$")
    female_id_type: IdType
    female_id_last4: str = Field(..., pattern=r"^[0-9A-Z]{4}$")
    male_id_type: Optional[IdType] = None
    male_id_last4: Optional[str] = Field(None, pattern=r"^[0-9A-Z]{4}$")
    marital_status_declared: Literal["married", "single_woman"]


class CycleIn(_M):
    couple_id: uuid.UUID
    cycle_type: Literal["iui", "ivf", "icsi", "fet", "donor_oocyte", "donor_sperm"]
    protocol_text: Optional[str] = Field(None, max_length=120)
    start_date: date


class DayIn(_M):
    day_no: int = Field(..., ge=1, le=40)
    on_date: date
    prescription_id: Optional[uuid.UUID] = None
    follicles_left: list[float] = Field(default_factory=list, max_length=40)
    follicles_right: list[float] = Field(default_factory=list, max_length=40)
    endometrium_mm: Optional[float] = Field(None, ge=0, le=40)
    e2_pg_ml: Optional[float] = Field(None, ge=0, le=20000)
    lh_miu_ml: Optional[float] = Field(None, ge=0, le=500)
    p4_ng_ml: Optional[float] = Field(None, ge=0, le=200)


class TriggerIn(_M):
    trigger_at: datetime


class AckIn(_M):
    via: Literal["phone_call", "in_person"]


class EmbryoIn(_M):
    label: str = Field(..., pattern=r"^E[0-9]{1,2}$")
    day: int = Field(..., ge=1, le=7)
    grade: str = Field(..., max_length=20)


class WitnessIn(_M):
    step: Literal["oocyte_collection", "sperm_prep", "insemination", "fertilisation_check", "transfer", "freeze", "thaw",
                  "discard"]
    embryo_id: Optional[uuid.UUID] = None
    consent_id: Optional[uuid.UUID] = None
    cryo_location: Optional[str] = Field(None, pattern=r"^T[0-9]{1,2}-C[0-9]{1,2}-K[0-9]{1,2}-S[0-9]{1,2}$")
    performed_at: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=300)
    witness_username: str = Field(..., min_length=1, max_length=100)
    witness_password: str = Field(..., min_length=1, max_length=200)


class ReleaseIn(_M):
    outcome: Literal["negative", "biochemical", "clinical", "not_applicable"]


@router.post("/ivf/couples", status_code=201)
async def ivf_couple(body: CoupleIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    out = await sp_ivf_wc.create_couple(scope, body.model_dump(mode="json"), user)
    await _audit(user, "SP_IVF_COUPLE", "ivf_couple", out["id"])
    return out


@router.get("/ivf/couples")
async def ivf_couples(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    return {"couples": await sp_ivf_wc.list_couples(scope)}


@router.post("/ivf/cycles", status_code=201)
async def ivf_cycle(body: CycleIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    return await sp_ivf_wc.create_cycle(scope, body.model_dump(mode="json") | {"start_date": body.start_date}, user)


@router.get("/ivf/cycles/{cycle_id}")
async def ivf_chart(cycle_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    return await sp_ivf_wc.cycle_chart(scope, str(cycle_id))


@router.put("/ivf/cycles/{cycle_id}/days")
async def ivf_day(cycle_id: uuid.UUID, body: DayIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    d = body.model_dump(mode="json") | {"on_date": body.on_date}
    return await sp_ivf_wc.upsert_day(scope, str(cycle_id), d, user)


@router.post("/ivf/cycles/{cycle_id}/trigger")
async def ivf_trigger(cycle_id: uuid.UUID, body: TriggerIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    if not getattr(user, "doctor_id", None):
        raise HTTPException(status_code=403, detail="Only the treating doctor's login sets the trigger time.")
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    if body.trigger_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Send the trigger time with its time zone.")
    out = await sp_ivf_wc.set_trigger(scope, str(cycle_id), body.trigger_at, user)
    await _audit(user, "SP_IVF_TRIGGER_SET", "ivf_cycle", str(cycle_id))
    return out


@router.post("/ivf/cycles/{cycle_id}/trigger/send")
async def ivf_trigger_send(cycle_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    return await sp_ivf_wc.send_trigger(clinic, str(cycle_id))


@router.post("/ivf/cycles/{cycle_id}/trigger/ack")
async def ivf_trigger_ack(cycle_id: uuid.UUID, body: AckIn, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    out = await sp_ivf_wc.acknowledge_trigger(scope, str(cycle_id), body.via)
    if not out:
        raise HTTPException(status_code=409, detail="Set the trigger time first.")
    await _audit(user, "SP_IVF_TRIGGER_ACK", "ivf_cycle", str(cycle_id), {"via": body.via})
    return out


@router.post("/ivf/cycles/{cycle_id}/embryos", status_code=201)
async def ivf_embryo(cycle_id: uuid.UUID, body: EmbryoIn, clinic_id: Optional[str] = None,
                     user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    return await sp_ivf_wc.add_embryo(scope, str(cycle_id), body.model_dump(), user)


@router.post("/ivf/cycles/{cycle_id}/witness", status_code=201)
async def ivf_witness(cycle_id: uuid.UUID, body: WitnessIn, request: Request, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    """Double witnessing: the second person signs in with their own login in the same dialog (re-authenticated here,
    rate-limited like any login); same clinic, clinical permission, a different person."""
    from app.routers.admin import _authenticate_password
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    witness = await _authenticate_password(body.witness_username, body.witness_password,
                                           request.client.host if request.client else "unknown")
    if str(witness.clinic_id or "") != scope or not holds_sp_permission(witness, "SP_CLINICAL"):
        raise HTTPException(status_code=403, detail="The witness must be clinical staff of this clinic.")
    out = await sp_ivf_wc.witness(scope, str(cycle_id), body.model_dump(exclude={"witness_username", "witness_password"},
                                                                        mode="json")
                                  | {"performed_at": body.performed_at}, user, witness)
    await _audit(user, "SP_IVF_WITNESS", "ivf_cycle", str(cycle_id), {"step": body.step, "witness": witness.username})
    return {k: v for k, v in out.items() if k not in ("prev_hash", "row_hash")}


@router.post("/ivf/cycles/{cycle_id}/release")
async def ivf_release(cycle_id: uuid.UUID, body: ReleaseIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "ivf")
    out = await sp_ivf_wc.release_result(scope, str(cycle_id), body.outcome, user)
    await _audit(user, "SP_IVF_RESULT_RELEASE", "ivf_cycle", str(cycle_id))
    return out


@router.get("/ivf/witness/verify")
async def ivf_verify(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN", "ivf")
    broken = await sp_ivf_wc.verify_chain(scope)
    return {"intact": broken is None, "first_broken_seq": broken}


@router.get("/ivf/art-register.csv")
async def ivf_art_register(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN", "ivf")
    await _audit(user, "SP_IVF_ART_EXPORT", "clinic", scope)
    return Response(await sp_ivf_wc.art_register_csv(scope), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="art-register.csv"', "Cache-Control": "no-store"})


# ── Women & Child: ANC + immunisation ────────────────────────────────────────

class PregnancyIn(_M):
    patient_id: uuid.UUID
    lmp: Optional[date] = None
    edd: date
    edd_source: Literal["lmp", "dating_scan", "ivf_transfer"]
    gravida: int = Field(..., ge=1, le=20)
    para: int = Field(0, ge=0, le=20)
    abortions: int = Field(0, ge=0, le=20)
    living: int = Field(0, ge=0, le=20)
    blood_group: Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "unknown"] = "unknown"
    risk_factors: list[str] = Field(default_factory=list, max_length=20)


class AncVisitIn(_M):
    visit_date: date
    encounter_id: Optional[uuid.UUID] = None
    bp_sys: Optional[int] = Field(None, ge=60, le=260)
    bp_dia: Optional[int] = Field(None, ge=30, le=160)
    weight_kg: Optional[float] = Field(None, ge=25, le=250)
    fundal_height_cm: Optional[int] = Field(None, ge=5, le=50)
    fhr_bpm: Optional[int] = Field(None, ge=60, le=220)
    oedema: Optional[bool] = None
    urine_protein: Optional[Literal["nil", "trace", "1+", "2+", "3+", "4+"]] = None
    hb_g_dl: Optional[float] = Field(None, ge=2, le=22)
    notes: Optional[str] = Field(None, max_length=500)


class DoseIn(_M):
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    schedule_code: Literal["iap_2023", "uip"]
    vaccine_code: str = Field(..., pattern=r"^[a-z0-9_]{2,30}$")
    dose_no: int = Field(..., ge=1, le=10)
    given_on: date
    brand: Optional[str] = Field(None, max_length=80)
    batch_no: str = Field(..., min_length=2, max_length=40)
    expiry: date
    site: Literal["left_thigh", "right_thigh", "left_arm", "right_arm", "oral", "other"]


@router.post("/anc/pregnancies", status_code=201)
async def anc_create(body: PregnancyIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    d = body.model_dump(mode="json") | {"edd": body.edd, "lmp": body.lmp}
    return await sp_ivf_wc.create_pregnancy(scope, d, user)


@router.get("/patients/{patient_id}/pregnancies")
async def anc_list(patient_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    from app.database import sb, scoped_query
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    res = await sb(scoped_query("obs_pregnancies", scope).eq("patient_id", str(patient_id)).order("created_at", desc=True))
    return {"pregnancies": res.data or []}


@router.post("/anc/pregnancies/{pregnancy_id}/visits", status_code=201)
async def anc_visit(pregnancy_id: uuid.UUID, body: AncVisitIn, clinic_id: Optional[str] = None,
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    d = body.model_dump(mode="json", exclude_none=True) | {"visit_date": body.visit_date}
    return await sp_ivf_wc.add_anc_visit(scope, str(pregnancy_id), d, user)


@router.get("/anc/pregnancies/{pregnancy_id}/timeline")
async def anc_timeline(pregnancy_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    return await sp_ivf_wc.anc_timeline(scope, str(pregnancy_id))


@router.get("/immunisation")
async def imm_schedule(patient_id: uuid.UUID, family_member_id: Optional[uuid.UUID] = None,
                       schedule: Literal["iap_2023", "uip"] = "iap_2023", clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    return await sp_ivf_wc.immunisation(scope, str(patient_id), str(family_member_id) if family_member_id else None, schedule)


@router.post("/immunisation/doses", status_code=201)
async def imm_give(body: DoseIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    d = body.model_dump(mode="json") | {"given_on": body.given_on, "expiry": body.expiry}
    out = await sp_ivf_wc.give_dose(scope, d, user)
    await _audit(user, "SP_IMM_DOSE", "imm_dose", out["id"], {"vaccine": body.vaccine_code, "dose": body.dose_no})
    return out


# ── SP-EYE-02 optical stock ──────────────────────────────────────────────────

class OpticalItemIn(_M):
    sku: str = Field(..., pattern=r"^[A-Z0-9_-]{2,30}$")
    kind: Literal["frame", "lens", "contact_lens", "solution", "accessory"]
    name: str = Field(..., min_length=2, max_length=120)
    brand: Optional[str] = Field(None, max_length=80)
    power_desc: Optional[str] = Field(None, max_length=80)
    barcode: Optional[str] = Field(None, pattern=r"^[0-9A-Za-z-]{4,40}$")
    hsn_code: Optional[str] = Field(None, pattern=r"^[0-9]{4,8}$")
    mrp_paise: Optional[int] = Field(None, ge=0, le=10_000_000)
    reorder_level: int = Field(0, ge=0, le=10000)


class StockIn(_M):
    delta: int = Field(..., ge=-1000, le=1000)
    reason: Literal["receive", "adjust", "write_off"]
    note: Optional[str] = Field(None, max_length=200)


@router.get("/optical/items")
async def optical_items(kind: Optional[Literal["frame", "lens", "contact_lens", "solution", "accessory"]] = None,
                        q: Optional[str] = Query(None, max_length=60), clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK", "eye")
    return {"items": await sp_eye_dental.list_items(scope, kind, q)}


@router.post("/optical/items", status_code=201)
async def optical_item_create(body: OpticalItemIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN", "eye")
    return await sp_eye_dental.create_item(scope, body.model_dump())


@router.post("/optical/items/{item_id}/stock")
async def optical_item_stock(item_id: uuid.UUID, body: StockIn, clinic_id: Optional[str] = None,
                             user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK" if body.reason == "receive" else "SP_ADMIN", "eye")
    if body.delta == 0 or (body.reason == "receive" and body.delta < 0) or (body.reason == "write_off" and body.delta > 0):
        raise HTTPException(status_code=422, detail="Receive adds, write-off removes, adjust may do either (never 0).")
    if body.reason in ("adjust", "write_off") and len((body.note or "").strip()) < 3:
        raise HTTPException(status_code=422, detail="Say why (at least 3 characters).")
    out = await sp_eye_dental.stock_move(scope, str(item_id), body.delta, body.reason, user, note=body.note)
    await _audit(user, "SP_OPTICAL_STOCK_" + body.reason.upper(), "sp_optical_item", str(item_id), {"delta": body.delta})
    return out


@router.get("/optical/items/{item_id}/moves")
async def optical_item_moves(item_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_DESK", "eye")
    return {"moves": await sp_eye_dental.item_moves(scope, str(item_id))}


# ── SP-EYE-05 accepted estimate → IPD package ────────────────────────────────

class EstIpdIn(_M):
    admission_id: Optional[uuid.UUID] = None


@router.post("/estimates/{estimate_id}/convert-ipd", status_code=201)
async def estimate_convert_ipd(estimate_id: uuid.UUID, body: EstIpdIn, clinic_id: Optional[str] = None,
                               user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_ADMIN")
    if user.role not in ("super_admin", "clinic_admin") and "IPD_ADMIN" not in (user.permissions or []):
        raise HTTPException(status_code=403, detail="Missing permission: IPD_ADMIN (an IPD package is created)")
    out = await sp_commerce.convert_estimate_to_ipd(clinic, str(estimate_id), str(body.admission_id) if body.admission_id else None,
                                                     user)
    await _audit(user, "SP_ESTIMATE_CONVERT_IPD", "sp_estimate", str(estimate_id), {"ipd_package_id": out["ipd_package"]["id"]})
    return out


# ── Continuous Care one-tap enrollment from a specialty page ────────────────

@router.get("/care-packs")
async def specialty_care_packs(specialty: Literal["eye", "dental", "derma", "ivf", "anc", "imm"],
                               patient_id: Optional[uuid.UUID] = None, family_member_id: Optional[uuid.UUID] = None,
                               clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "SP_CLINICAL", specialty)
    return await sp.care_packs(clinic, specialty, str(patient_id) if patient_id else None,
                               str(family_member_id) if family_member_id else None)


# ── Women & Child completion (migration 156) ────────────────────────────────

class BabyIn(_M):
    outcome: Literal["live_birth", "stillbirth"]
    sex: Optional[Literal["male", "female", "indeterminate"]] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    birth_weight_g: Optional[int] = Field(None, ge=300, le=7000)
    length_cm: Optional[float] = Field(None, ge=25, le=65)
    head_cm: Optional[float] = Field(None, ge=18, le=45)
    apgar_1: Optional[int] = Field(None, ge=0, le=10)
    apgar_5: Optional[int] = Field(None, ge=0, le=10)
    nicu_admitted: bool = False
    create_child_record: bool = True


class DeliveryIn(_M):
    delivered_at: datetime
    mode: Literal["vaginal", "assisted_vaginal", "caesarean"]
    gestation_weeks: Optional[int] = Field(None, ge=20, le=44)
    place: Literal["this_facility", "other_facility", "home"] = "this_facility"
    admission_id: Optional[uuid.UUID] = None
    notes: Optional[str] = Field(None, max_length=500)
    babies: list[BabyIn] = Field(..., min_length=1, max_length=6)


class BirthRegIn(_M):
    birth_reg_no: str = Field(..., min_length=3, max_length=40)
    registered_on: date


class GrowthIn(_M):
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    measured_on: date
    weight_kg: Optional[float] = Field(None, ge=0.3, le=200)
    length_cm: Optional[float] = Field(None, ge=25, le=220)
    position: Optional[Literal["recumbent", "standing"]] = None
    head_cm: Optional[float] = Field(None, ge=18, le=65)


class GovIdsIn(_M):
    patient_id: uuid.UUID
    family_member_id: Optional[uuid.UUID] = None
    rch_id: Optional[str] = Field(None, pattern=r"^[0-9]{12}$")
    mcp_card_no: Optional[str] = Field(None, min_length=3, max_length=30)
    uwin_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9-]{6,30}$")
    abha_number: Optional[str] = Field(None, pattern=r"^[0-9]{2}-[0-9]{4}-[0-9]{4}-[0-9]{4}$")


class NicuIn(_M):
    recorded_at: datetime
    weight_g: Optional[int] = Field(None, ge=300, le=7000)
    temp_c: Optional[float] = Field(None, ge=30, le=42)
    heart_rate: Optional[int] = Field(None, ge=40, le=260)
    resp_rate: Optional[int] = Field(None, ge=5, le=120)
    spo2: Optional[int] = Field(None, ge=40, le=100)
    feed_type: Optional[Literal["breast", "expressed_breast_milk", "formula", "iv_fluids", "nil"]] = None
    feed_ml: Optional[int] = Field(None, ge=0, le=500)
    phototherapy: bool = False
    bilirubin_mg_dl: Optional[float] = Field(None, ge=0, le=40)
    resp_support: Literal["none", "oxygen", "cpap", "ventilator"] = "none"
    notes: Optional[str] = Field(None, max_length=300)


async def _wc_any(user, clinic_id):
    """Government ids serve both the mother (RCH) and the child (U-WIN): either women & child pack."""
    scope, clinic = await _scope(user, clinic_id, "SP_CLINICAL")
    if not (sp_access(clinic, "anc") or sp_access(clinic, "imm")):
        raise HTTPException(status_code=403, detail="This specialty module is not enabled for this clinic.")
    return scope


@router.post("/anc/pregnancies/{pregnancy_id}/delivery", status_code=201)
async def wc_delivery(pregnancy_id: uuid.UUID, body: DeliveryIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    if body.delivered_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Send the delivery time with its time zone.")
    d = body.model_dump(mode="json") | {"delivered_at": body.delivered_at, "babies": [b.model_dump() for b in body.babies]}
    out = await sp_wc.record_delivery(scope, str(pregnancy_id), d, user)
    await _audit(user, "SP_WC_DELIVERY", "obs_delivery", out["id"], {"babies": len(out["newborns"])})
    return out


@router.get("/wc/birth-registrations")
async def wc_births(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    return {"births": await sp_wc.birth_registrations(scope)}


@router.post("/wc/newborns/{newborn_id}/registration")
async def wc_birth_register(newborn_id: uuid.UUID, body: BirthRegIn, clinic_id: Optional[str] = None,
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "anc")
    return await sp_wc.mark_registered(scope, str(newborn_id), body.birth_reg_no, body.registered_on)


@router.get("/wc/rch.csv")
async def wc_rch_csv(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN", "anc")
    await _audit(user, "SP_WC_RCH_EXPORT", "clinic", scope)
    return Response(await sp_wc.rch_csv(scope), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="rch-pregnancies.csv"', "Cache-Control": "no-store"})


@router.post("/wc/growth", status_code=201)
async def wc_growth_add(body: GrowthIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    if (body.length_cm is None) != (body.position is None):
        raise HTTPException(status_code=422, detail="Say whether the length was taken lying (recumbent) or standing.")
    return await sp_wc.add_measurement(scope, body.model_dump(mode="json") | {"measured_on": body.measured_on}, user)


@router.get("/wc/growth")
async def wc_growth(patient_id: uuid.UUID, family_member_id: Optional[uuid.UUID] = None, clinic_id: Optional[str] = None,
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    return await sp_wc.growth_chart(scope, str(patient_id), str(family_member_id) if family_member_id else None)


@router.put("/wc/gov-ids")
async def wc_gov_ids_put(body: GovIdsIn, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope = await _wc_any(user, clinic_id)
    out = await sp_wc.upsert_gov_ids(scope, body.model_dump(mode="json"), user)
    await _audit(user, "SP_WC_GOV_IDS", "patient", str(body.patient_id))
    return out


@router.get("/wc/gov-ids")
async def wc_gov_ids_get(patient_id: uuid.UUID, family_member_id: Optional[uuid.UUID] = None, clinic_id: Optional[str] = None,
                         user: AdminUser = Depends(verify_credentials)):
    scope = await _wc_any(user, clinic_id)
    return await sp_wc.get_gov_ids(scope, str(patient_id), str(family_member_id) if family_member_id else None)


@router.get("/wc/uwin.csv")
async def wc_uwin_csv(from_: date = Query(..., alias="from"), to: date = Query(...), clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "SP_ADMIN", "imm")
    if from_ > to or (to - from_).days > 366:
        raise HTTPException(status_code=422, detail="Pick a date range of at most one year.")
    await _audit(user, "SP_WC_UWIN_EXPORT", "clinic", scope)
    return Response(await sp_wc.uwin_csv(scope, from_, to), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="uwin-vaccinations.csv"', "Cache-Control": "no-store"})


@router.post("/wc/nicu/{admission_id}/entries", status_code=201)
async def wc_nicu_add(admission_id: uuid.UUID, body: NicuIn, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    from app.services.tenant import ipd_access
    scope, clinic = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    if ipd_access(clinic) is None:
        raise HTTPException(status_code=403, detail="IPD OS is not enabled for this clinic.")
    if body.recorded_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Send the time with its time zone.")
    return await sp_wc.add_nicu_entry(scope, str(admission_id), body.model_dump(mode="json") | {"recorded_at": body.recorded_at},
                                      user)


@router.get("/wc/nicu/{admission_id}")
async def wc_nicu(admission_id: uuid.UUID, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    from app.services.tenant import ipd_access
    scope, clinic = await _scope(user, clinic_id, "SP_CLINICAL", "imm")
    if ipd_access(clinic) is None:
        raise HTTPException(status_code=403, detail="IPD OS is not enabled for this clinic.")
    return await sp_wc.nicu_flowsheet(scope, str(admission_id))
