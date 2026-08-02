"""Clinic management routes for multi-tenant onboarding."""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

from app.database import supabase
from app.config import settings
from app.services.tenant import (
    invalidate_tenant_cache,
    invalidate_branch_cache,
    get_clinic_by_id,
)
from app.services.whatsapp import whatsapp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/clinics", tags=["clinics"])


def verify_admin_secret(x_admin_secret: str = Header(...)):
    """Verify admin secret for clinic management endpoints."""
    if not settings.admin_secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET not configured. Set it in environment variables.",
        )
    if x_admin_secret != settings.admin_secret:
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
    clinic_name: str
    doctor_name: str
    language: str = "en"
    timezone: str = "Asia/Kolkata"
    system_prompt: Optional[str] = None
    logo_url: Optional[str] = None
    # Per-clinic Razorpay credentials (optional — falls back to global settings if omitted)
    razorpay_key_id: Optional[str] = None  # e.g. "rzp_live_xxxxxx"
    razorpay_key_secret: Optional[str] = None  # Keep this secret
    razorpay_webhook_secret: Optional[str] = None  # From Razorpay Dashboard → Webhooks
    # Branches — optional, for polyclinic/multi-branch onboarding
    branches: Optional[list[BranchSeed]] = None


@router.post("", dependencies=[Depends(verify_admin_secret)])
async def create_clinic(req: CreateClinicRequest):
    """Onboard a new hospital. Zero deployment needed."""
    config = {
        "meta_phone_number_id": req.meta_phone_number_id,
        "meta_access_token": req.meta_access_token,
        "clinic_name": req.clinic_name,
        "doctor_name": req.doctor_name,
        "language": req.language,
        "timezone": req.timezone,
    }
    if req.system_prompt:
        config["system_prompt"] = req.system_prompt
    if req.logo_url:
        config["logo_url"] = req.logo_url
    # Only store Razorpay keys if explicitly provided — never store empty strings
    if req.razorpay_key_id:
        config["razorpay_key_id"] = req.razorpay_key_id
    if req.razorpay_key_secret:
        config["razorpay_key_secret"] = req.razorpay_key_secret
    if req.razorpay_webhook_secret:
        config["razorpay_webhook_secret"] = req.razorpay_webhook_secret

    try:
        result = (
            supabase.table("clinics")
            .insert(
                {
                    "name": req.name,
                    "whatsapp_number": req.whatsapp_number,
                    "plan": req.plan,
                    "config": config,
                }
            )
            .execute()
        )

        if not result.data:
            raise HTTPException(500, "Failed to create clinic")

        clinic = result.data[0]
        clinic_id = clinic["id"]

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

        return {
            "success": True,
            "clinic": clinic,
            "branches": created_branches if created_branches else None,
        }

    except Exception as e:
        logger.error(f"Error creating clinic: {e}")
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(
                409, "A clinic with this WhatsApp number already exists"
            )
        raise HTTPException(500, f"Failed to create clinic: {e}")


@router.get("", dependencies=[Depends(verify_admin_secret)])
async def list_clinics():
    """List all clinics."""
    result = (
        supabase.table("clinics")
        .select("id,name,whatsapp_number,plan,is_active,created_at")
        .execute()
    )
    return {"clinics": result.data or []}


@router.patch("/{clinic_id}", dependencies=[Depends(verify_admin_secret)])
async def update_clinic(clinic_id: str, updates: dict):
    """Update plan, config, or status. Clears tenant cache."""
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
        f"✅ Test message from {clinic['name']}. Your MediAssist AI is live!",
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
