"""HMIS/ABDM Integration: HL7 FHIR R4 Schema Converters.

Converts MediAssist internal data objects to standard HL7 FHIR R4 JSON
resources, enabling interoperability with Indian hospital networks, ABDM,
and external HMIS systems.

Resources supported:
  - Patient  (from patients table row)
  - Appointment (from appointments table row)
  - DiagnosticReport (from lab_reports table row)
  - Bundle (collection of resources)

Reference: https://hl7.org/fhir/R4/
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_FHIR_VERSION = "4.0.1"
_FHIR_SERVER_BASE = "https://mediassist.ai/fhir"


def patient_to_fhir(patient: dict, clinic: Optional[dict] = None) -> dict:
    """Convert a MediAssist patient dict to FHIR R4 Patient resource.

    Args:
        patient: Row from the `patients` Supabase table.
        clinic: Optional clinic row for Organization reference.

    Returns:
        FHIR R4 Patient JSON dict (https://hl7.org/fhir/R4/patient.html)
    """
    resource: dict = {
        "resourceType": "Patient",
        "id": str(patient.get("id", "")),
        "meta": {
            "versionId": "1",
            "lastUpdated": _to_fhir_dt(
                patient.get("updated_at") or patient.get("created_at")
            ),
            "profile": ["https://nrces.in/ndhm/fhir/r4/StructureDefinition/Patient"],
        },
        "identifier": [
            {
                "use": "official",
                "system": f"{_FHIR_SERVER_BASE}/identifier/patient",
                "value": str(patient.get("id", "")),
            }
        ],
        "active": True,
    }

    # Name
    if patient.get("name"):
        given_parts = patient["name"].strip().split()
        resource["name"] = [
            {
                "use": "official",
                "text": patient["name"],
                "given": given_parts[:-1] if len(given_parts) > 1 else given_parts,
                "family": given_parts[-1] if len(given_parts) > 1 else "",
            }
        ]

    # Telecom (phone)
    if patient.get("phone"):
        resource["telecom"] = [
            {
                "system": "phone",
                "value": patient["phone"],
                "use": "mobile",
            }
        ]

    # Language preference
    if patient.get("language"):
        lang_map = {"en": "en-IN", "hi": "hi-IN", "te": "te-IN"}
        fhir_lang = lang_map.get(patient["language"], "en-IN")
        resource["communication"] = [
            {
                "language": {
                    "coding": [{"system": "urn:ietf:bcp:47", "code": fhir_lang}],
                    "text": fhir_lang,
                },
                "preferred": True,
            }
        ]

    # Organization (clinic) reference
    if clinic:
        resource["managingOrganization"] = {
            "reference": f"Organization/{clinic.get('id', '')}",
            "display": clinic.get("name", ""),
        }

    return resource


def appointment_to_fhir(appointment: dict, clinic: Optional[dict] = None) -> dict:
    """Convert a MediAssist appointment dict to FHIR R4 Appointment resource.

    Args:
        appointment: Row from the `appointments` Supabase table.
        clinic: Optional clinic row for Organization reference.

    Returns:
        FHIR R4 Appointment JSON dict (https://hl7.org/fhir/R4/appointment.html)
    """
    # Map internal status to FHIR status codes
    status_map = {
        "confirmed": "booked",
        "cancelled": "cancelled",
        "rescheduled": "booked",
        "completed": "fulfilled",
        "no_show": "noshow",
    }
    fhir_status = status_map.get(appointment.get("status", "confirmed"), "booked")

    # Build start datetime
    appt_date = appointment.get("appointment_date", "")
    appt_time = appointment.get("appointment_time", "00:00")
    start_iso = f"{appt_date}T{str(appt_time)[:5]}:00+05:30" if appt_date else None

    resource: dict = {
        "resourceType": "Appointment",
        "id": str(appointment.get("id", "")),
        "meta": {
            "lastUpdated": _to_fhir_dt(appointment.get("updated_at")),
            "profile": [
                "https://nrces.in/ndhm/fhir/r4/StructureDefinition/Appointment"
            ],
        },
        "identifier": [
            {
                "system": f"{_FHIR_SERVER_BASE}/identifier/appointment",
                "value": appointment.get("booking_ref")
                or str(appointment.get("id", "")),
            }
        ],
        "status": fhir_status,
        "serviceType": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/service-type",
                        "code": "57",
                        "display": appointment.get("department", "General Medicine"),
                    }
                ],
                "text": appointment.get("department", "General Medicine"),
            }
        ],
        "reasonCode": [],
        "participant": [],
    }

    # Symptoms → reason
    if appointment.get("symptoms"):
        resource["reasonCode"] = [{"text": appointment["symptoms"][:200]}]

    # Start time
    if start_iso:
        resource["start"] = start_iso

    # Patient participant
    if appointment.get("patient_id") or appointment.get("patient_phone"):
        resource["participant"].append(
            {
                "actor": {
                    "reference": f"Patient/{appointment.get('patient_id', '')}",
                    "display": appointment.get("patient_name", ""),
                },
                "status": "accepted",
            }
        )

    # Practitioner participant
    if appointment.get("doctor_name"):
        resource["participant"].append(
            {
                "actor": {
                    "display": appointment["doctor_name"],
                },
                "status": "accepted",
            }
        )

    # Location (clinic)
    if clinic:
        resource["participant"].append(
            {
                "actor": {
                    "reference": f"Organization/{clinic.get('id', '')}",
                    "display": clinic.get("name", ""),
                },
                "status": "accepted",
            }
        )

    return resource


def lab_report_to_fhir(report: dict, clinic: Optional[dict] = None) -> dict:
    """Convert a MediAssist lab_report dict to FHIR R4 DiagnosticReport resource.

    Args:
        report: Row from the `lab_reports` Supabase table.
        clinic: Optional clinic row.

    Returns:
        FHIR R4 DiagnosticReport JSON dict.
    """
    # Map status
    status_map = {
        "sent": "final",
        "pending": "preliminary",
        "failed": "cancelled",
    }
    fhir_status = status_map.get(report.get("status", "sent"), "final")

    resource: dict = {
        "resourceType": "DiagnosticReport",
        "id": str(report.get("id", "")),
        "meta": {
            "lastUpdated": _to_fhir_dt(report.get("created_at")),
        },
        "status": fhir_status,
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                        "code": "LAB",
                        "display": "Laboratory",
                    }
                ]
            }
        ],
        "code": {
            "text": report.get("report_name")
            or report.get("report_type", "Lab Report"),
            "coding": [
                {
                    "system": "http://loinc.org",
                    "display": report.get("report_type", "Laboratory study"),
                }
            ],
        },
        "subject": {
            "display": report.get("patient_name", ""),
        },
        "effectiveDateTime": _to_fhir_dt(report.get("created_at")),
        "issued": _to_fhir_dt(report.get("created_at")),
    }

    # Add patient reference if phone available
    if report.get("patient_phone"):
        resource["subject"] = {
            "identifier": {
                "system": "phone",
                "value": report["patient_phone"],
            },
            "display": report.get("patient_name", ""),
        }

    # Add PDF URL as presentedForm
    if report.get("file_url"):
        resource["presentedForm"] = [
            {
                "contentType": "application/pdf",
                "url": report["file_url"],
                "title": report.get("report_name", "Lab Report"),
            }
        ]

    return resource


def create_fhir_bundle(
    resources: list[dict],
    bundle_type: str = "collection",
    clinic: Optional[dict] = None,
) -> dict:
    """Wrap a list of FHIR resources into a FHIR R4 Bundle.

    Args:
        resources: List of FHIR resource dicts.
        bundle_type: FHIR bundle type (collection, searchset, transaction).
        clinic: Optional clinic for Bundle identifier.

    Returns:
        FHIR R4 Bundle JSON dict.
    """
    entries = []
    for resource in resources:
        resource_type = resource.get("resourceType", "Unknown")
        resource_id = resource.get("id", "")
        entries.append(
            {
                "fullUrl": f"{_FHIR_SERVER_BASE}/{resource_type}/{resource_id}",
                "resource": resource,
            }
        )

    bundle = {
        "resourceType": "Bundle",
        "id": f"bundle-{_now_iso()}",
        "meta": {
            "lastUpdated": _now_iso(),
            "profile": ["https://nrces.in/ndhm/fhir/r4/StructureDefinition/Bundle"],
        },
        "type": bundle_type,
        "timestamp": _now_iso(),
        "total": len(entries),
        "entry": entries,
    }

    if clinic:
        bundle["identifier"] = {
            "system": f"{_FHIR_SERVER_BASE}/identifier/bundle",
            "value": f"{clinic.get('id', 'default')}-{_now_iso()}",
        }

    return bundle


def _to_fhir_dt(value) -> str:
    """Convert a datetime or ISO string to FHIR dateTime format."""
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if isinstance(value, datetime):
        return value.isoformat()
    # Already a string — normalize
    return str(value).replace("Z", "+00:00")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
