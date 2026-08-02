"""FHIR R4 API Router for MediAssist AI.

Exposes standard HL7 FHIR R4 REST endpoints to enable:
  - External HMIS systems to query patient/appointment data
  - ABDM ecosystem integration
  - Health information exchange across Indian hospital networks

Endpoints:
  GET  /fhir/Patient/{phone}          — Patient resource by phone
  GET  /fhir/Appointment/{booking_ref} — Appointment resource by booking ref
  GET  /fhir/DiagnosticReport/{id}    — Lab report as DiagnosticReport
  POST /fhir/Bundle                   — Import a FHIR Bundle (future)

Security: All endpoints require HTTP Basic auth (same as /admin).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse

from app.database import supabase
from app.routers.admin import verify_credentials
from app.services.fhir_schemas import (
    patient_to_fhir,
    appointment_to_fhir,
    lab_report_to_fhir,
    create_fhir_bundle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fhir", tags=["fhir"])

_FHIR_CONTENT_TYPE = "application/fhir+json; charset=utf-8"


def _fhir_response(data: dict) -> JSONResponse:
    """Return a JSON response with the correct FHIR content-type header."""
    return JSONResponse(content=data, media_type=_FHIR_CONTENT_TYPE)


@router.get("/Patient/{phone}")
async def get_patient_fhir(
    phone: str = Path(..., description="Patient phone number (E.164 or local format)"),
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Return a patient as a FHIR R4 Patient resource.

    Args:
        phone: Patient's registered phone number.
        clinic_id: Tenant clinic ID (defaults to 'default').
    """
    try:
        # Fetch patient
        query = supabase.table("patients").select("*").eq("phone", phone)
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "not-found",
                            "details": {"text": "Patient not found"},
                        }
                    ],
                },
            )

        patient = result.data[0]

        # Fetch clinic for Organization reference
        clinic = None
        if patient.get("clinic_id"):
            clinic_res = (
                supabase.table("clinics")
                .select("*")
                .eq("id", patient["clinic_id"])
                .execute()
            )
            clinic = clinic_res.data[0] if clinic_res.data else None

        fhir_patient = patient_to_fhir(patient, clinic)
        return _fhir_response(fhir_patient)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FHIR Patient fetch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/Appointment/{booking_ref}")
async def get_appointment_fhir(
    booking_ref: str = Path(
        ..., description="Appointment booking reference (e.g. MC-ABC123)"
    ),
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Return an appointment as a FHIR R4 Appointment resource.

    Args:
        booking_ref: Booking reference code (e.g., MC-ABC123).
        clinic_id: Tenant clinic ID.
    """
    try:
        query = (
            supabase.table("appointments").select("*").eq("booking_ref", booking_ref)
        )
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "not-found",
                            "details": {"text": "Appointment not found"},
                        }
                    ],
                },
            )

        appointment = result.data[0]

        # Fetch clinic
        clinic = None
        if appointment.get("clinic_id"):
            clinic_res = (
                supabase.table("clinics")
                .select("*")
                .eq("id", appointment["clinic_id"])
                .execute()
            )
            clinic = clinic_res.data[0] if clinic_res.data else None

        fhir_appt = appointment_to_fhir(appointment, clinic)
        return _fhir_response(fhir_appt)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FHIR Appointment fetch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/DiagnosticReport/{report_id}")
async def get_diagnostic_report_fhir(
    report_id: str = Path(..., description="Lab report UUID"),
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Return a lab report as a FHIR R4 DiagnosticReport resource."""
    try:
        query = supabase.table("lab_reports").select("*").eq("id", report_id)
        if clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        result = query.execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Report not found")

        report = result.data[0]

        clinic = None
        if report.get("clinic_id"):
            clinic_res = (
                supabase.table("clinics")
                .select("*")
                .eq("id", report["clinic_id"])
                .execute()
            )
            clinic = clinic_res.data[0] if clinic_res.data else None

        fhir_report = lab_report_to_fhir(report, clinic)
        return _fhir_response(fhir_report)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FHIR DiagnosticReport fetch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/Patient/{phone}/everything")
async def get_patient_everything(
    phone: str = Path(..., description="Patient phone number"),
    clinic_id: str = "default",
    user: str = Depends(verify_credentials),
):
    """Return a FHIR Bundle with all data for a patient.

    Implements the FHIR $everything operation — returns Patient + Appointments
    + DiagnosticReports as a single Bundle resource.
    """
    try:
        resources = []

        # Patient
        q = supabase.table("patients").select("*").eq("phone", phone)
        if clinic_id != "default":
            q = q.eq("clinic_id", clinic_id)
        p_res = q.execute()
        if not p_res.data:
            raise HTTPException(status_code=404, detail="Patient not found")
        patient = p_res.data[0]

        clinic = None
        if patient.get("clinic_id"):
            c_res = (
                supabase.table("clinics")
                .select("*")
                .eq("id", patient["clinic_id"])
                .execute()
            )
            clinic = c_res.data[0] if c_res.data else None

        resources.append(patient_to_fhir(patient, clinic))

        # Appointments
        a_q = supabase.table("appointments").select("*").eq("patient_phone", phone)
        if clinic_id != "default":
            a_q = a_q.eq("clinic_id", clinic_id)
        a_res = a_q.execute()
        for appt in a_res.data or []:
            resources.append(appointment_to_fhir(appt, clinic))

        # Lab Reports
        lr_q = supabase.table("lab_reports").select("*").eq("patient_phone", phone)
        if clinic_id != "default":
            lr_q = lr_q.eq("clinic_id", clinic_id)
        lr_res = lr_q.execute()
        for report in lr_res.data or []:
            resources.append(lab_report_to_fhir(report, clinic))

        bundle = create_fhir_bundle(resources, bundle_type="searchset", clinic=clinic)
        return _fhir_response(bundle)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FHIR $everything error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
