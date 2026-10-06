"""Platform owner controls for the AI receptionist — /platform/voice.

Enabling/disabling a clinic is the existing PATCH /platform/clinics/{id}/features
with feature="ai_receptionist". This router adds what only the owner may do:
map Exotel numbers to a clinic/branch, set the monthly voice budget, and see
every clinic's usage and cost. Owner Basic auth (verify_owner_credentials).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.database import is_uuid, sb, supabase
from app.routers.admin import AdminUser, log_admin_action
from app.routers.platform import verify_owner_credentials
from app.services.tenant import ai_receptionist_enabled, invalidate_tenant_cache
from app.voice import store
from app.voice.phone import to_e164

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platform/voice", tags=["voice-platform"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/overview")
async def overview(owner: AdminUser = Depends(verify_owner_credentials)):
    res = await sb(
        # unscoped: platform_admin
        supabase.table("clinics").select("id, name, plan, features, config, account_type, is_active")
        .eq("account_type", "tenant").eq("is_active", True).order("name"))
    nums = await sb(
        # unscoped: platform_admin
        supabase.table("voice_numbers").select("id, clinic_id, branch_id, exophone, published_number, "
                                               "reception_number, label, is_active").order("created_at"))
    by_clinic: dict = {}
    for n in nums.data or []:
        by_clinic.setdefault(n["clinic_id"], []).append(n)
    clinics = []
    for c in res.data or []:
        enabled = ai_receptionist_enabled(c)
        usage = await store.month_usage(c["id"]) if enabled else {"calls": 0, "minutes": 0, "cost_paise": 0}
        clinics.append({"id": c["id"], "name": c["name"], "plan": c.get("plan"), "enabled": enabled,
                        "budget_paise": int(store.voice_config(c).get("monthly_budget_paise") or 0),
                        "numbers": by_clinic.get(c["id"], []), **usage})
    return {
        "clinics": clinics,
        "platform": {"voice_enabled": settings.voice_enabled,
                     "stream_url": settings.voice_public_wss_url or None,
                     "sarvam_configured": bool(settings.sarvam_api_key),
                     "exotel_configured": bool(settings.exotel_api_key and settings.exotel_account_sid),
                     "token_configured": bool(settings.voice_stream_token),
                     "rates_configured": bool(store.usage_cost_paise(60, 1000, 60))},
    }


class NumberIn(BaseModel):
    clinic_id: str
    branch_id: Optional[str] = None
    exophone: str = Field(min_length=8, max_length=20)
    published_number: Optional[str] = Field(None, max_length=20)
    reception_number: Optional[str] = Field(None, max_length=20)
    label: Optional[str] = Field(None, max_length=60)


@router.post("/numbers")
async def add_number(body: NumberIn, request: Request, owner: AdminUser = Depends(verify_owner_credentials)):
    exo = to_e164(body.exophone)
    if not exo or not is_uuid(body.clinic_id):
        raise HTTPException(status_code=422, detail="A valid clinic and Exophone (E.164) are required.")
    clinic = await sb(supabase.table("clinics").select("id, account_type").eq("id", body.clinic_id).limit(1))
    if not clinic.data or (clinic.data[0].get("account_type") or "tenant") != "tenant":
        raise HTTPException(status_code=404, detail="Clinic not found")
    if body.branch_id:
        br = await sb(supabase.table("branches").select("id").eq("clinic_id", body.clinic_id)
                      .eq("id", body.branch_id).limit(1))
        if not br.data:
            raise HTTPException(status_code=422, detail="That branch does not belong to this clinic.")
    row = {"clinic_id": body.clinic_id, "branch_id": body.branch_id, "exophone": exo,
           "published_number": body.published_number, "reception_number": to_e164(body.reception_number),
           "label": body.label}
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("voice_numbers").insert(row))
    except Exception as e:
        if "uq_voice_numbers_exophone" in str(e) or "23505" in str(e):
            raise HTTPException(status_code=409, detail="This Exophone is already mapped to a clinic.")
        raise HTTPException(status_code=500, detail="Could not save the number.")
    invalidate_tenant_cache()
    await log_admin_action(owner, "voice_number_add", "voice_number", res.data[0]["id"], details=row,
                           ip_address=_ip(request))
    return {"number": res.data[0]}


class NumberPatch(BaseModel):
    is_active: Optional[bool] = None
    reception_number: Optional[str] = Field(None, max_length=20)
    label: Optional[str] = Field(None, max_length=60)


@router.patch("/numbers/{number_id}")
async def patch_number(number_id: str, body: NumberPatch, request: Request,
                       owner: AdminUser = Depends(verify_owner_credentials)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "reception_number" in fields:
        fields["reception_number"] = to_e164(fields["reception_number"])
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    # unscoped: platform_admin
    res = await sb(supabase.table("voice_numbers").update(fields).eq("id", number_id))
    if not res.data:
        raise HTTPException(status_code=404, detail="Number not found")
    await log_admin_action(owner, "voice_number_update", "voice_number", number_id, details=fields,
                           ip_address=_ip(request))
    return {"number": res.data[0]}


class BudgetIn(BaseModel):
    monthly_budget_paise: int = Field(ge=0, le=100_000_000)


@router.put("/clinics/{clinic_id}/budget")
async def set_budget(clinic_id: str, body: BudgetIn, request: Request,
                     owner: AdminUser = Depends(verify_owner_credentials)):
    """0 = no cap. When the month's voice cost reaches the cap, new calls go
    straight to reception (the caller is never left unanswered)."""
    # unscoped: platform_admin
    res = await sb(supabase.table("clinics").select("config").eq("id", clinic_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Clinic not found")
    config = dict(res.data[0].get("config") or {})
    voice = dict(config.get("voice") or {})
    voice["monthly_budget_paise"] = body.monthly_budget_paise
    config["voice"] = voice
    # unscoped: platform_admin
    await sb(supabase.table("clinics").update({"config": config}).eq("id", clinic_id))
    invalidate_tenant_cache()
    await log_admin_action(owner, "voice_budget_set", "clinic", clinic_id, details=body.model_dump(),
                           ip_address=_ip(request))
    return {"success": True}
