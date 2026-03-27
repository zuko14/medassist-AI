"""Admin router for analytics and management."""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from app.config import settings
from app.database import supabase
from app.services.analytics import analytics_service
from app.services.lab_reports import LabReportService
from app.services.prescriptions import PrescriptionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials."""
    if (credentials.username != settings.admin_username or
        credentials.password != settings.admin_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"}
        )
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
async def get_stats(days: int = 30, user: str = Depends(verify_credentials)):
    """Get dashboard statistics."""
    return await analytics_service.get_dashboard_stats(days)


@router.get("/appointments/recent")
async def get_recent_appointments(
    limit: int = 20,
    user: str = Depends(verify_credentials)
):
    """Get recent appointments."""
    return await analytics_service.get_recent_appointments(limit)


@router.get("/appointments/upcoming")
async def get_upcoming_appointments(
    days: int = 7,
    user: str = Depends(verify_credentials)
):
    """Get upcoming appointments."""
    return await analytics_service.get_upcoming_appointments(days)


@router.get("/departments/popular")
async def get_popular_departments(
    days: int = 30,
    user: str = Depends(verify_credentials)
):
    """Get popular departments."""
    return await analytics_service.get_popular_departments(days)


@router.get("/doctors")
async def get_doctors(user: str = Depends(verify_credentials)):
    """Get all doctors."""
    try:
        result = supabase.table("doctors").select("*").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting doctors: {e}")
        raise HTTPException(status_code=500, detail="Failed to get doctors")


@router.post("/doctors")
async def create_doctor(
    doctor: DoctorCreate,
    user: str = Depends(verify_credentials)
):
    """Create a new doctor."""
    try:
        result = supabase.table("doctors").insert(doctor.dict()).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to create doctor")


@router.put("/doctors/{doctor_id}")
async def update_doctor(
    doctor_id: str,
    doctor: DoctorUpdate,
    user: str = Depends(verify_credentials)
):
    """Update an existing doctor."""
    try:
        update_data = doctor.dict(exclude_unset=True)
        if not update_data:
            return {"message": "No fields to update"}
        result = supabase.table("doctors").update(update_data).eq("id", doctor_id).execute()
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
    user: str = Depends(verify_credentials)
):
    """Delete a doctor."""
    try:
        result = supabase.table("doctors").delete().eq("id", doctor_id).execute()
        # Note: if doctor has appointments, foreign key constraints might fail unless cascading is enabled
        return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting doctor: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete doctor")


@router.get("/leaves")
async def get_leaves(
    doctor: Optional[str] = None,
    user: str = Depends(verify_credentials)
):
    """Get doctor leaves."""
    try:
        query = supabase.table("doctor_leaves").select("*")
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
async def delete_leave(leave_id: str, user: str = Depends(verify_credentials)):
    """Delete a doctor leave."""
    try:
        supabase.table("doctor_leaves").delete().eq("id", leave_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting leave: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete leave")


@router.get("/holidays")
async def get_holidays(user: str = Depends(verify_credentials)):
    """Get hospital holidays."""
    try:
        result = supabase.table("hospital_holidays").select("*").order("holiday_date").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting holidays: {e}")
        raise HTTPException(status_code=500, detail="Failed to get holidays")


@router.post("/holidays")
async def create_holiday(
    holiday_date: date,
    name: str,
    user: str = Depends(verify_credentials)
):
    """Create a hospital holiday."""
    try:
        result = supabase.table("hospital_holidays").insert({
            "holiday_date": str(holiday_date),
            "name": name
        }).execute()
        return result.data[0]
    except Exception as e:
        logger.error(f"Error creating holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to create holiday")


@router.delete("/holidays/{holiday_date}")
async def delete_holiday(holiday_date: str, user: str = Depends(verify_credentials)):
    """Delete a hospital holiday."""
    try:
        supabase.table("hospital_holidays").delete().eq("holiday_date", holiday_date).execute()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting holiday: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete holiday")

@router.delete("/appointments/{appointment_id}")
async def cancel_appointment_by_admin(
    appointment_id: str,
    user: str = Depends(verify_credentials)
):
    try:
        result = supabase.table("appointments") \
                         .update({"status": "cancelled"}) \
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
    user: str = Depends(verify_credentials),
):
    """Upload and send a lab report to a patient via WhatsApp."""
    try:
        file_bytes = await file.read()
        result = await LabReportService().upload_and_send(
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
async def get_lab_reports(user: str = Depends(verify_credentials)):
    """Get all lab reports."""
    result = await LabReportService().get_all_reports()
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
async def get_patients(user: str = Depends(verify_credentials)):
    """Get all patients with appointment counts."""
    try:
        result = supabase.rpc(
            "get_patients_with_counts",
            {},
        ).execute()
        if result.data:
            return {"patients": result.data}
        # Fallback: simple query
        patients = supabase.table("patients").select("*").order("phone").execute()
        return {"patients": patients.data or []}
    except Exception:
        # Fallback if RPC doesn't exist
        patients = supabase.table("patients").select("*").order("phone").execute()
        return {"patients": patients.data or []}


# ═══════ PRESCRIPTIONS ═══════

@router.post("/prescriptions")
async def add_prescription(
    body: dict,
    user: str = Depends(verify_credentials),
):
    """Add a new prescription reminder."""
    try:
        result = await PrescriptionService().add_prescription(
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
    user: str = Depends(verify_credentials),
):
    """Get all prescriptions."""
    result = await PrescriptionService().get_all_prescriptions(active_only)
    return {"prescriptions": result}


@router.post("/prescriptions/{prescription_id}/deactivate")
async def deactivate_prescription(
    prescription_id: str,
    user: str = Depends(verify_credentials),
):
    """Deactivate a prescription reminder."""
    try:
        await PrescriptionService().deactivate_prescription(prescription_id)
        return {"success": True}
    except Exception as e:
        logger.error(f"Prescription deactivate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

