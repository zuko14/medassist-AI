"""Kriya Data Import API — "Switch to Kriya" (migration 144; docs/data-import/SPEC.md §5).

Every route: permission -> enforce_clinic_access -> mi_access(clinic) -> service. Imports need DATA_IMPORT (clinic
admin by default); the front-desk legacy search / link / match queue also accept OPD_FRONT_DESK. Zero-LLM.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import data_import as mi
from app.services.tenant import get_clinic_by_id, mi_access

router = APIRouter(prefix="/admin/mi", tags=["Data import"])

Kind = Literal["doctors", "services", "packages", "payers", "patients", "history", "lab_values", "dues",
               "appointments_future", "inpatients_cutover", "chronic_list"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplyIn(_In):
    send_confirmations: bool = False      # imported bookings send no message unless staff choose (SPEC §3)


class LinkIn(_In):
    patient_record_id: str = Field(..., min_length=36, max_length=36)
    patient_id: str = Field(..., min_length=36, max_length=36)


class DueClose(_In):
    status: Literal["collected", "written_off"]
    invoice_id: Optional[str] = Field(None, min_length=36, max_length=36)


class DxMapIn(_In):
    legacy_text: str = Field(..., min_length=2, max_length=200)
    icd10_code: str = Field(..., min_length=3, max_length=8)


def _holds(user: AdminUser, *perms: str) -> bool:
    return user.role in ("super_admin", "clinic_admin") or bool(set(perms) & set(user.permissions or []))


async def _scope(user: AdminUser, clinic_id: Optional[str], *perms: str) -> tuple[str, dict]:
    if not _holds(user, *perms):
        raise HTTPException(status_code=403, detail=f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not mi_access(clinic):
        raise HTTPException(status_code=403, detail="Switch to Kriya (data import) is not enabled for this clinic.")
    return scope, clinic


async def _audit(user, request: Request, action: str, rid: Optional[str], details: dict):
    await log_admin_action(user=user, action=action, resource_type="mi_batch", resource_id=rid, details=details,
                           ip_address=request.client.host if request.client else None)


IMPORT, DESK = ("DATA_IMPORT",), ("DATA_IMPORT", "OPD_FRONT_DESK")


@router.get("/progress")
async def get_progress(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    return {"steps": await mi.progress(scope), "presets": list(mi.PRESETS)}


@router.get("/batches")
async def get_batches(clinic_id: Optional[str] = None, kind: Optional[Kind] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    return {"batches": await mi.list_batches(scope, kind)}


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    b = await mi.get_batch(scope, batch_id)
    b.pop("payload", None)
    return {**b, "errors": await mi.batch_errors(scope, batch_id)}


@router.get("/batches/{batch_id}/errors.csv")
async def get_batch_errors_csv(batch_id: str, clinic_id: Optional[str] = None,
                               user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    await mi.get_batch(scope, batch_id)
    return Response(mi.errors_csv(await mi.batch_errors(scope, batch_id)), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="import-errors-{batch_id[:8]}.csv"'})


@router.get("/templates/{kind}.csv")
async def get_template(kind: Kind, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    await _scope(user, clinic_id, *IMPORT)
    return Response(mi.template_csv(kind), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="kriya-{kind}-template.csv"'})


@router.post("/{kind}/dry-run")
async def dry_run(kind: Kind, request: Request, file: UploadFile = File(...), preset: str = "generic",
                  clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, *IMPORT)
    raw = await file.read(mi.MAX_BYTES + 1)
    out = await mi.dry_run(clinic, kind, preset, file.filename or "", raw, user)
    await _audit(user, request, "mi_dry_run", out["id"], {"kind": kind, "preset": preset, "rows_ok": out["rows_ok"],
                                                          "rows_error": out["rows_error"]})
    return out


@router.post("/{kind}/apply/{batch_id}")
async def apply(kind: Kind, batch_id: str, request: Request, body: Optional[ApplyIn] = None,
                clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, *IMPORT)
    if (await mi.get_batch(scope, batch_id))["kind"] != kind:
        raise HTTPException(status_code=404, detail="Import batch not found.")
    out = await mi.apply(clinic, batch_id, user, (body or ApplyIn()).send_confirmations)
    await _audit(user, request, "mi_apply", batch_id, {"kind": kind, **{k: out.get(k) for k in (
        "written", "updated", "skipped_existing", "refused", "already_applied")}})
    return out


@router.delete("/batches/{batch_id}")
async def undo(batch_id: str, request: Request, clinic_id: Optional[str] = None,
               user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    out = await mi.undo(scope, batch_id)
    await _audit(user, request, "mi_undo", batch_id, out)
    return out


# ── front desk: legacy search, link, phone match queue, patient legacy cards ──

@router.get("/legacy-search")
async def legacy_search(q: str = Query(..., min_length=3, max_length=80), clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    return {"records": await mi.legacy_search(scope, q)}


@router.post("/links", status_code=201)
async def create_link(body: LinkIn, request: Request, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    row = await mi.link(scope, body.patient_record_id, body.patient_id, user)
    await log_admin_action(user=user, action="mi_link", resource_type="patient", resource_id=body.patient_id,
                           details={"patient_record_id": body.patient_record_id},
                           ip_address=request.client.host if request.client else None)
    return row


@router.get("/patients/{patient_id}/legacy")
async def patient_legacy(patient_id: str, clinic_id: Optional[str] = None,
                         user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "DATA_IMPORT", "OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING")
    return await mi.patient_legacy(scope, patient_id)


@router.post("/matches/refresh")
async def refresh_matches(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    return {"added": await mi.refresh_matches(scope)}


@router.get("/matches")
async def list_matches(status: Literal["pending", "confirmed", "rejected"] = "pending",
                       clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    return {"matches": await mi.list_matches(scope, status)}


@router.post("/matches/{match_id}/confirm")
async def confirm_match(match_id: str, request: Request, clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    row = await mi.decide_match(scope, match_id, True, user)
    await log_admin_action(user=user, action="mi_match_confirm", resource_type="mi_match", resource_id=match_id,
                           details={}, ip_address=request.client.host if request.client else None)
    return row


@router.post("/matches/{match_id}/reject")
async def reject_match(match_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *DESK)
    return await mi.decide_match(scope, match_id, False, user)


@router.post("/dues/{due_id}/close")
async def close_due(due_id: str, body: DueClose, request: Request, clinic_id: Optional[str] = None,
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "DATA_IMPORT", "OPD_BILLING")
    row = await mi.close_due(scope, due_id, body.status, body.invoice_id, user)
    await log_admin_action(user=user, action="mi_due_close", resource_type="mi_opening_balance", resource_id=due_id,
                           details={"status": body.status, "invoice_id": body.invoice_id},
                           ip_address=request.client.host if request.client else None)
    return row


@router.get("/diagnosis-map")
async def get_diagnosis_map(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    return {"map": await mi.list_diagnosis_map(scope)}


@router.put("/diagnosis-map")
async def put_diagnosis_map(body: DxMapIn, clinic_id: Optional[str] = None,
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    return await mi.set_diagnosis_map(scope, body.legacy_text, body.icd10_code, user)


@router.get("/chronic-candidates")
async def get_chronic_candidates(status: Optional[Literal["new", "on_radar", "dismissed"]] = "new",
                                 clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, *IMPORT)
    return {"candidates": await mi.chronic_candidates(scope, status)}
