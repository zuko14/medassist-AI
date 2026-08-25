"""Clinic management routes for multi-tenant onboarding."""

import logging
import re
import secrets
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

from app.database import supabase
from app.config import settings
from app.routers.admin import hash_password
from app.services.tenant import (
    invalidate_tenant_cache,
    invalidate_branch_cache,
    get_clinic_by_id,
)
from app.services.whatsapp import whatsapp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/clinics", tags=["clinics"])


def verify_admin_secret(x_admin_secret: str = Header(...)):
    """Verify admin secret for clinic management endpoints (W8.5)."""
    if not settings.admin_secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET not configured. Set it in environment variables.",
        )
    if not secrets.compare_digest(
        x_admin_secret.encode("utf-8"),
        settings.admin_secret.encode("utf-8"),
    ):
        raise HTTPException(status_code=403, detail="Invalid admin secret")


class BranchSeed(BaseModel):
    """Branch data for seeding during clinic onboarding."""

    name: str
    short_name: Optional[str] = None
    address: Optional[str] = None
    landmark: Optional[str] = None
    maps_link: Optional[str] = None
    phone: Optional[str] = None
    is_diagnostic: bool = False
    display_order: int = 0


class CreateClinicRequest(BaseModel):
    name: str
    whatsapp_number: str  # E.164, e.g. "+919876543210"
    plan: Literal[
        "soloclinic", "diagstream", "essential", "polyclinic", "enterprise"
    ] = "soloclinic"
    meta_phone_number_id: str
    meta_access_token: str
    clinic_name: Optional[str] = None
    doctor_name: Optional[str] = "Medical Team"
    language: str = "en"
    timezone: str = "Asia/Kolkata"
    system_prompt: Optional[str] = None
    logo_url: Optional[str] = None
    # Per-clinic front-desk contact & location (optional — falls back to global settings if omitted)
    hospital_phone: Optional[str] = None  # Front-desk number, distinct from whatsapp_number
    hospital_address: Optional[str] = None
    hospital_maps_link: Optional[str] = None
    hospital_emergency_number: Optional[str] = None  # Clinic's own emergency desk line
    # Per-clinic Razorpay credentials (optional — falls back to global settings if omitted)
    razorpay_key_id: Optional[str] = None  # e.g. "rzp_live_xxxxxx"
    razorpay_key_secret: Optional[str] = None  # Keep this secret
    razorpay_webhook_secret: Optional[str] = None  # From Razorpay Dashboard → Webhooks
    # Branches — optional, for polyclinic/multi-branch onboarding
    branches: Optional[list[BranchSeed]] = None


async def provision_clinic(req: CreateClinicRequest) -> dict:
    """Onboard a new hospital: creates the clinic row, seeds branches, and
    auto-provisions a self-service clinic_admin login.

    Shared by the X-Admin-Secret curl API (POST /admin/clinics) and the
    owner-platform UI (POST /platform/clinics) so both paths stay identical.
    """
    config = {
        "meta_phone_number_id": req.meta_phone_number_id,
        "meta_access_token": req.meta_access_token,
        "clinic_name": req.clinic_name or req.name,
        "doctor_name": req.doctor_name or "Medical Team",
        "language": req.language or "en",
        "timezone": req.timezone or "Asia/Kolkata",
    }
    if req.system_prompt:
        config["system_prompt"] = req.system_prompt
    if req.logo_url:
        config["logo_url"] = req.logo_url
    # Front-desk contact & location — stored under the same config keys faq_engine.py reads
    if req.hospital_phone:
        config["phone"] = req.hospital_phone
    if req.hospital_address:
        config["address"] = req.hospital_address
    if req.hospital_maps_link:
        config["maps_link"] = req.hospital_maps_link
    if req.hospital_emergency_number:
        config["emergency_number"] = req.hospital_emergency_number
    # Only store Razorpay keys if explicitly provided — never store empty strings
    if req.razorpay_key_id:
        config["razorpay_key_id"] = req.razorpay_key_id
    if req.razorpay_key_secret:
        config["razorpay_key_secret"] = req.razorpay_key_secret
    if req.razorpay_webhook_secret:
        config["razorpay_webhook_secret"] = req.razorpay_webhook_secret

    # Also persist phone_number_id at root level for dual-key index resolution (Migration 043)
    clinic_insert_payload = {
        "name": req.name,
        "whatsapp_number": req.whatsapp_number,
        "plan": req.plan,
        "config": config,
    }
    if req.meta_phone_number_id:
        clinic_insert_payload["phone_number_id"] = str(req.meta_phone_number_id)

    try:
        # unscoped: creating new tenant clinic record in clinics table
        result = (
            supabase.table("clinics")
            .insert(clinic_insert_payload)
            .execute()
        )

        if not result.data:
            raise HTTPException(500, "Failed to create clinic")

        clinic = result.data[0]
        clinic_id = clinic["id"]

        # ── Auto-register phone number with Meta Cloud API to transition Pending -> Connected ──
        if req.meta_phone_number_id:
            token = req.meta_access_token or settings.whatsapp_token
            if token:
                import httpx
                try:
                    reg_url = f"https://graph.facebook.com/v21.0/{req.meta_phone_number_id}/register"
                    reg_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    reg_payload = {"messaging_product": "whatsapp", "pin": "123456"}
                    async with httpx.AsyncClient(timeout=20.0) as http_client:
                        reg_resp = await http_client.post(reg_url, headers=reg_headers, json=reg_payload)
                        if reg_resp.status_code == 200:
                            logger.info(f"Auto-registered phone_number_id {req.meta_phone_number_id} on Meta Cloud API: {reg_resp.text}")
                        else:
                            logger.warning(f"Meta auto-register returned status {reg_resp.status_code}: {reg_resp.text}")
                except Exception as reg_err:
                    logger.warning(f"Meta auto-register failed for phone_number_id {req.meta_phone_number_id}: {reg_err}")

        # ── Seed branches if provided ──
        created_branches = []
        if req.branches:
            for branch in req.branches:
                try:
                    branch_data = branch.dict()
                    branch_data["clinic_id"] = clinic_id
                    br_result = supabase.table("branches").insert(branch_data).execute()
                    if br_result.data:
                        created_branches.append(br_result.data[0])
                except Exception as be:
                    logger.warning(f"Failed to seed branch '{branch.name}': {be}")

            # Invalidate branch cache for the new clinic
            invalidate_branch_cache(clinic_id)

        # ── Auto-provision a self-service clinic_admin login ──
        # Without this, a newly onboarded clinic has no way to log into
        # admin/index.html except via the platform owner's env-based
        # super_admin account, which can't change its own password.
        clinic_admin = None
        slug = re.sub(r"[^a-z0-9]", "", req.name.lower())[:20] or "clinic"
        username = f"{slug}{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(16)
        try:
            admin_result = (
                supabase.table("clinic_admins")
                .insert(
                    {
                        "clinic_id": clinic_id,
                        "username": username,
                        "password_hash": hash_password(password),
                        "role": "clinic_admin",
                        "is_active": True,
                    }
                )
                .execute()
            )
            if admin_result.data:
                clinic_admin = {"username": username, "password": password}
        except Exception as ae:
            logger.warning(f"Failed to auto-provision clinic_admin for clinic {clinic_id}: {ae}")

        return {
            "success": True,
            "clinic": clinic,
            "branches": created_branches if created_branches else None,
            "clinic_admin": clinic_admin,
        }

    except Exception as e:
        logger.error(f"Error creating clinic: {e}")
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                409, "A clinic with this WhatsApp number already exists"
            )
        raise HTTPException(500, f"Failed to create clinic: {e}")


@router.post("", dependencies=[Depends(verify_admin_secret)])
async def create_clinic(req: CreateClinicRequest):
    return await provision_clinic(req)


@router.get("", dependencies=[Depends(verify_admin_secret)])
async def list_clinics():
    """List all clinics."""
    result = (
        # platform-scoped: list all clinics
        supabase.table("clinics")
        .select("id,name,whatsapp_number,plan,is_active,created_at")
        .execute()
    )
    return {"clinics": result.data or []}


class UpdateClinicRequest(BaseModel):
    name: Optional[str] = None
    plan: Optional[Literal["soloclinic", "diagstream", "essential", "polyclinic", "enterprise"]] = None
    is_active: Optional[bool] = None
    config: Optional[dict] = None
    whatsapp_number: Optional[str] = None
    meta_phone_number_id: Optional[str] = None
    meta_access_token: Optional[str] = None


@router.patch("/{clinic_id}", dependencies=[Depends(verify_admin_secret)])
async def update_clinic(clinic_id: str, req: UpdateClinicRequest | dict):
    """Update plan, config, or status with validated fields (W8.5). Clears tenant cache."""
    if isinstance(req, dict):
        updates = req
    elif hasattr(req, "model_dump"):
        updates = req.model_dump(exclude_unset=True)
    elif hasattr(req, "dict"):
        updates = req.dict(exclude_unset=True)
    else:
        updates = dict(req)

    if not updates:
        raise HTTPException(400, "No fields provided to update")

    # Guard payment_mode/payment_deposit_percent invariant
    incoming_config = updates.get("config")
    if isinstance(incoming_config, dict) and incoming_config.get("payment_mode") == "partial":
        percent = incoming_config.get("payment_deposit_percent")
        if not (isinstance(percent, int) and 1 <= percent <= 99):
            raise HTTPException(
                422,
                "config.payment_deposit_percent (1-99) is required when config.payment_mode is 'partial'",
            )

    # platform-scoped: update clinic by ID
    result = supabase.table("clinics").update(updates).eq("id", clinic_id).execute()

    if not result.data:
        raise HTTPException(404, "Clinic not found")

    # Clear cache so next message picks up new config
    clinic = result.data[0]
    invalidate_tenant_cache(clinic["whatsapp_number"])
    return {"success": True, "clinic": clinic}


@router.post("/{clinic_id}/test", dependencies=[Depends(verify_admin_secret)])
async def test_clinic(clinic_id: str, to: str):
    """Send a test WhatsApp message from the clinic's number."""
    clinic = await get_clinic_by_id(clinic_id)
    # Note: For proper multi-tenant, would need to create a per-clinic
    # WhatsApp service instance. For now, uses the global one.
    success = await whatsapp_service.send_text(
        clinic,
        to,
        f"✅ Test message from {clinic['name']}. Your Kriya AI is live!",
        _source="clinics",
    )
    return {"sent": success}


@router.delete("/{clinic_id}", dependencies=[Depends(verify_admin_secret)])
async def deactivate_clinic(clinic_id: str):
    """Soft-delete: sets is_active=false. Data preserved."""
    result = (
        supabase.table("clinics")
        .update({"is_active": False})
        .eq("id", clinic_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(404, "Clinic not found")

    invalidate_tenant_cache(result.data[0]["whatsapp_number"])
    return {"success": True, "message": "Clinic deactivated"}
