"""Admin router for analytics and management — Security Hardened."""

import logging
import secrets
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status, UploadFile, File, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from app.config import settings
from app.database import supabase
from app.services.analytics import analytics_service
from app.services.lab_reports import LabReportService
from app.services.prescriptions import PrescriptionService
from app.utils.security import login_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


async def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    """Verify admin credentials with brute-force protection.
    
    Security measures:
    - Rate limiting: 5 attempts per minute per IP address
    - Constant-time comparison to prevent timing attacks
    - Failed attempt logging for audit trail
    """
    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"

    # Check rate limit BEFORE validating credentials
    if login_rate_limiter.is_rate_limited(client_ip):
        remaining_wait = 60  # seconds
        logger.warning(
            f"Admin login rate limit exceeded — IP={client_ip}"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

    # Record this attempt
    login_rate_limiter.record_attempt(client_ip)

    # Constant-time comparison prevents timing side-channel attacks
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.admin_username.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )

    if not (username_ok and password_ok):
        remaining = login_rate_limiter.remaining_attempts(client_ip)
        logger.warning(
            f"Failed admin login attempt — IP={client_ip}, "
            f"user='{credentials.username}', remaining={remaining}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Successful login — reset rate limiter for this IP
    login_rate_limiter.reset(client_ip)
    return credentials.username


class LeaveCreate(BaseModel):
    doctor_name: str
    leave_date: date
    leave_type: str  # full, half_morning, half_evening
    end_date: Optional[date] = None
    reason: Optional[str] = None


class DoctorCreate(BaseModel):
    name: str
    specialization: str
    department: str
    available_days: str = "Mon,Tue,Wed,Thu,Fri"
    morning_slots: list[str] = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
    evening_slots: list[str] = ["17:00", "17:30", "18:00", "18:30"]
    is_active: bool = True
    consultation_fee: int = 500


class DoctorUpdate(BaseModel):
    name: Optional[str] = None
    specialization: Optional[str] = None
    department: Optional[str] = None
    available_days: Optional[str] = None
    morning_slots: Optional[list[str]] = None
    evening_slots: Optional[list[str]] = None
    is_active: Optional[bool] = None
    consultation_fee: Optional[int] = None


@router.get("/stats")
async def get_stats(clinic_id: str = "default", days: int = 30, user: str = Depends(verify_credentials)):
    """Get dashboard statistics."""
    return await analytics_service.get_dashboard_stats(clinic_id, days)


@router.get("/appointments/recent")
async def get_recent_appointments(
    clinic_id: str = "default",
    limit: int = 20,
    user: str = Depends(verify_credentials)
):
    """Get recent appointments."""
    return await analytics_service.get_recent_appointments(clinic_id, limit)


@router.get("/appointments/upcoming")
async def get_upcoming_appointments(
    clinic_id: str = "default",
    days: int = 7,
    user: str = Depends(verify_credentials)
):
    """Get upcoming appointments."""
    return await analytics_service.get_upcoming_appointments(clinic_id, days)


@router.get("/departments/popular")
async def get_popular_departments(
    clinic_id: str = "default",
    days: int = 30,
    user: str = Depends(verify_credentials)
):
    """Get popular departments."""
    return await analytics_service.get_popular_departments(clinic_id, days)


@router.get("/doctors")
async def get_doctors(clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Get all doctors."""
    try:
        query = supabase.table("doctors").select("*")
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get doctors")


@router.post("/doctors")
async def create_doctor(
    doctor: DoctorCreate,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Create a new doctor."""
    try:
        doctor_data = doctor.dict()
        doctor_data["clinic_id"] = clinic_id
        result = supabase.table("doctors").insert(doctor_data).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to create doctor")


@router.put("/doctors/{doctor_id}")
async def update_doctor(
    doctor_id: str,
    doctor: DoctorUpdate,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Update an existing doctor."""
    try:
        update_data = doctor.dict(exclude_unset=True)
        if not update_data:
            return {"message": "No fields to update"}
        result = supabase.table("doctors").update(update_data).eq("clinic_id", clinic_id).eq("id", doctor_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Doctor not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to update doctor")


@router.delete("/doctors/{doctor_id}")
async def delete_doctor(
    doctor_id: str,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Delete a doctor."""
    try:
        result = supabase.table("doctors").delete().eq("clinic_id", clinic_id).eq("id", doctor_id).execute()
        # Note: if doctor has appointments, foreign key constraints might fail unless cascading is enabled
        return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete doctor")


@router.get("/leaves")
async def get_leaves(
    doctor: Optional[str] = None,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Get doctor leaves."""
    try:
        query = supabase.table("doctor_leaves").select("*").eq("clinic_id", clinic_id)
        if doctor:
            query = query.eq("doctor_name", doctor)
        result = query.order("leave_date").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting leaves: {e}")
        raise HTTPException(status_code=500, detail="Failed to get leaves")


@router.post("/leaves")
async def create_leave(
    leave: LeaveCreate,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Create a doctor leave (single day or date range)."""
    from datetime import timedelta
    try:
        start_date = leave.leave_date
        end_date = leave.end_date or start_date
        
        if end_date < start_date:
            raise HTTPException(status_code=400, detail="End date cannot be before start date")
            
        leaves_to_insert = []
        current_date = start_date
        
        while current_date <= end_date:
            leave_data = leave.dict(exclude={"end_date"})
            leave_data["leave_date"] = str(current_date)
            leave_data["clinic_id"] = clinic_id
            leaves_to_insert.append(leave_data)
            current_date += timedelta(days=1)
            
        result = supabase.table("doctor_leaves").insert(leaves_to_insert).execute()
        
        # Return the first inserted leave just to satisfy the previous API signature somewhat
        # in case the frontend depends on it
        if result.data:
            return result.data[0]
        return {"status": "success", "count": len(leaves_to_insert)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating leave: {e}")
        raise HTTPException(status_code=500, detail="Failed to create leave")


@router.delete("/leaves/{leave_id}")
async def delete_leave(leave_id: str, clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Delete a doctor leave."""
    try:
        supabase.table("doctor_leaves").delete().eq("clinic_id", clinic_id).eq("id", leave_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting leave: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete leave")


@router.get("/holidays")
async def get_holidays(clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Get hospital holidays."""
    try:
        query = supabase.table("hospital_holidays").select("*").order("holiday_date")
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting holidays: {e}")
        raise HTTPException(status_code=500, detail="Failed to get holidays")


@router.post("/holidays")
async def create_holiday(
    holiday_date: date,
    name: str,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    """Create a hospital holiday."""
    try:
        result = supabase.table("hospital_holidays").insert({
            "clinic_id": clinic_id,
            "holiday_date": str(holiday_date),
            "name": name
        }).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to create holiday")


@router.delete("/holidays/{holiday_date}")
async def delete_holiday(holiday_date: str, clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Delete a hospital holiday."""
    try:
        supabase.table("hospital_holidays").delete().eq("clinic_id", clinic_id).eq("holiday_date", holiday_date).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete holiday")

@router.delete("/appointments/{appointment_id}")
async def cancel_appointment_by_admin(
    appointment_id: str,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials)
):
    try:
        result = supabase.table("appointments") \
                         .update({"status": "cancelled"}) \
                         .eq("clinic_id", clinic_id) \
                         .eq("id", appointment_id) \
                         .execute()
        if result.data:
            return {"success": True}
        return {"success": False, "message": "Not found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════ LAB REPORTS ═══════

@router.post("/lab-reports/upload")
async def upload_lab_report(
    file: UploadFile = File(...),
    patient_phone: str = Form(...),
    patient_name: str = Form(...),
    report_name: str = Form(...),
    report_type: str = Form("General"),
    clinic_id: str = Form("default"),
    user: str = Depends(verify_credentials),
):
    """Upload and send a lab report to a patient via WhatsApp."""
    try:
        file_bytes = await file.read()
        result = await LabReportService().upload_and_send(
            clinic_id=clinic_id,
            file_bytes=file_bytes,
            filename=file.filename,
            content_type=file.content_type or "application/pdf",
            patient_phone=patient_phone,
            patient_name=patient_name,
            report_name=report_name,
            report_type=report_type,
        )
        return {"success": True, "message": "Report sent to patient via WhatsApp", "report": result}
    except Exception as e:
        logger.error(f"Lab report upload error: {e}")
        return {"success": False, "error": str(e)}


@router.get("/lab-reports")
async def get_lab_reports(clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Get all lab reports."""
    result = await LabReportService().get_all_reports(clinic_id)
    return {"reports": result}


@router.post("/lab-reports/{report_id}/resend")
async def resend_lab_report(
    report_id: str,
    user: str = Depends(verify_credentials),
):
    """Resend a lab report to the patient."""
    try:
        await LabReportService().resend_report(report_id)
        return {"success": True, "message": "Report resent successfully"}
    except Exception as e:
        logger.error(f"Lab report resend error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/patients")
async def get_patients(clinic_id: str = "default", user: str = Depends(verify_credentials)):
    """Get all patients with appointment counts."""
    try:
        result = supabase.rpc(
            "get_patients_with_counts",
            {"p_clinic_id": clinic_id},
        ).execute()
        if result.data:
            return {"patients": result.data}
        if clinic_id == "default":
            patients = supabase.table("patients").select("*").order("phone").execute()
        else:
            patients = supabase.table("patients").select("*").eq("clinic_id", clinic_id).order("phone").execute()
        return {"patients": patients.data or []}
    except Exception:
        # Fallback if RPC doesn't exist
        if clinic_id == "default":
            patients = supabase.table("patients").select("*").order("phone").execute()
        else:
            patients = supabase.table("patients").select("*").eq("clinic_id", clinic_id).order("phone").execute()
        return {"patients": patients.data or []}


# ═══════ PRESCRIPTIONS ═══════

@router.post("/prescriptions")
async def add_prescription(
    body: dict,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Add a new prescription reminder."""
    try:
        clinic_id = body.get("clinic_id", clinic_id)
        result = await PrescriptionService().add_prescription(
            clinic_id=clinic_id,
            patient_phone=body["patient_phone"],
            patient_name=body["patient_name"],
            medicine_name=body["medicine_name"],
            dosage=body["dosage"],
            frequency=body["frequency"],
            reminder_times=body["reminder_times"],
            start_date=body["start_date"],
            end_date=body["end_date"],
            notes=body.get("notes"),
        )
        return {"success": True, "prescription": result}
    except Exception as e:
        logger.error(f"Prescription add error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prescriptions")
async def get_prescriptions(
    active_only: bool = False,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Get all prescriptions."""
    result = await PrescriptionService().get_all_prescriptions(clinic_id, active_only)
    return {"prescriptions": result}


@router.post("/prescriptions/{prescription_id}/deactivate")
async def deactivate_prescription(
    prescription_id: str,
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Deactivate a prescription reminder."""
    try:
        await PrescriptionService().deactivate_prescription(clinic_id, prescription_id)
        return {"success": True}
    except Exception as e:
        logger.error(f"Prescription deactivate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════ PAYMENTS & BOOKINGS ═══════

@router.get("/bookings")
async def get_bookings(
    clinic_id: str = "default",
    status: Optional[str] = None,
    limit: int = 50,
    user: str = Depends(verify_credentials),
):
    """Get all bookings with payment information."""
    try:
        query = supabase.table("appointments").select(
            "id, clinic_id, patient_phone, patient_name, department, doctor_name, "
            "appointment_date, appointment_time, status, razorpay_order_id, "
            "payment_id, amount_paise, hold_expires_at, booking_ref, created_at, updated_at"
        )
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).limit(limit).execute()
        return {"bookings": result.data or []}
    except Exception as e:
        logger.error(f"Error getting bookings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get bookings")


@router.get("/bookings/pending-review")
async def get_pending_review_bookings(
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Get bookings in pending_review status — needs human eyes."""
    try:
        query = supabase.table("appointments").select("*").eq("status", "pending_review")
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.order("created_at", desc=True).execute()
        return {"bookings": result.data or []}
    except Exception as e:
        logger.error(f"Error getting pending review bookings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get pending review bookings")


@router.post("/bookings/{booking_id}/confirm")
async def admin_confirm_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Manually confirm a pending_review booking (admin override)."""
    try:
        from app.services.payment import payment_service
        admin_notes = (body or {}).get("admin_notes", f"Confirmed by admin: {user}")
        result = await payment_service.admin_confirm_booking(booking_id, admin_notes)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("reason", "Failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin confirm booking error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bookings/{booking_id}/reject")
async def admin_reject_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Manually reject a pending_review booking + initiate refund."""
    try:
        from app.services.payment import payment_service
        admin_notes = (body or {}).get("admin_notes", f"Rejected by admin: {user}")
        result = await payment_service.admin_reject_booking(booking_id, admin_notes)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("reason", "Failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin reject booking error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bookings/{booking_id}/refund")
async def admin_refund_booking(
    booking_id: str,
    body: dict = None,
    user: str = Depends(verify_credentials),
):
    """Initiate a refund for a confirmed booking."""
    try:
        from app.services.payment import payment_service
        reason = (body or {}).get("reason", f"Admin refund by {user}")
        result = await payment_service.initiate_refund(booking_id, reason)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("reason", "Refund failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin refund error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/payment-events/{booking_id}")
async def get_payment_events(
    booking_id: str,
    user: str = Depends(verify_credentials),
):
    """Get the payment audit trail for a booking."""
    try:
        result = supabase.table("payment_events") \
            .select("*") \
            .eq("booking_id", booking_id) \
            .order("created_at", desc=False) \
            .execute()
        return {"events": result.data or []}
    except Exception as e:
        logger.error(f"Error getting payment events: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment events")


@router.get("/payments/reconciliation")
async def get_payment_reconciliation(
    date_str: Optional[str] = None,
    user: str = Depends(verify_credentials),
):
    """Get daily payment reconciliation summary."""
    try:
        from app.services.payment import payment_service
        summary = await payment_service.get_daily_reconciliation(date_str)
        return summary
    except Exception as e:
        logger.error(f"Reconciliation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get reconciliation data")


@router.get("/payments/stats")
async def get_payment_stats(
    clinic_id: str = "default",
    days: int = 30,
    user: str = Depends(verify_credentials),
):
    """Get payment statistics for the dashboard."""
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # Total confirmed with payments
        confirmed = supabase.table("appointments") \
            .select("id, amount_paise", count="exact") \
            .eq("status", "confirmed") \
            .not_.is_("payment_id", "null") \
            .gte("created_at", cutoff) \
            .execute()

        # Total pending review
        pending = supabase.table("appointments") \
            .select("id", count="exact") \
            .eq("status", "pending_review") \
            .execute()

        # Total refunded
        refunded = supabase.table("appointments") \
            .select("id, amount_paise", count="exact") \
            .eq("status", "refunded") \
            .gte("created_at", cutoff) \
            .execute()

        # Total expired
        expired = supabase.table("appointments") \
            .select("id", count="exact") \
            .eq("status", "expired") \
            .gte("created_at", cutoff) \
            .execute()

        confirmed_amount = sum(b.get("amount_paise", 0) for b in (confirmed.data or []))
        refunded_amount = sum(b.get("amount_paise", 0) for b in (refunded.data or []))

        # Signature failures
        sig_failures = supabase.table("payment_events") \
            .select("id", count="exact") \
            .eq("event_type", "signature_failed") \
            .gte("created_at", cutoff) \
            .execute()

        return {
            "confirmed_count": len(confirmed.data or []),
            "confirmed_amount_rupees": confirmed_amount / 100,
            "pending_review_count": len(pending.data or []),
            "refunded_count": len(refunded.data or []),
            "refunded_amount_rupees": refunded_amount / 100,
            "expired_count": len(expired.data or []),
            "signature_failures": len(sig_failures.data or []),
            "period_days": days,
        }
    except Exception as e:
        logger.error(f"Payment stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment stats")

