"""Tests for FHIR R4 Schema Converters (app/services/fhir_schemas.py).

Verifies conversion of internal database dicts to valid HL7 FHIR R4 resources.
"""

from app.services.fhir_schemas import (
    patient_to_fhir,
    appointment_to_fhir,
    lab_report_to_fhir,
    create_fhir_bundle,
)


class TestFHIRSchemas:
    """Test FHIR schema conversions."""

    def test_patient_to_fhir(self):
        pat_db = {
            "id": "pat-123",
            "name": "Rahul Sharma",
            "phone": "+919876543210",
            "language": "hi",
            "updated_at": "2026-06-28T10:00:00Z",
        }
        clinic = {"id": "clinic-1", "name": "City Care Hospital"}

        fhir = patient_to_fhir(pat_db, clinic)
        assert fhir["resourceType"] == "Patient"
        assert fhir["id"] == "pat-123"
        assert fhir["name"][0]["text"] == "Rahul Sharma"
        assert fhir["name"][0]["family"] == "Sharma"
        assert fhir["telecom"][0]["value"] == "+919876543210"
        assert fhir["communication"][0]["language"]["coding"][0]["code"] == "hi-IN"
        assert fhir["managingOrganization"]["reference"] == "Organization/clinic-1"

    def test_appointment_to_fhir(self):
        appt_db = {
            "id": "appt-456",
            "booking_ref": "MC-999",
            "status": "confirmed",
            "appointment_date": "2026-07-01",
            "appointment_time": "10:30:00",
            "department": "Cardiology",
            "symptoms": "Mild chest pain",
            "patient_id": "pat-123",
            "patient_name": "Rahul Sharma",
            "doctor_name": "Dr. Smith",
        }

        fhir = appointment_to_fhir(appt_db)
        assert fhir["resourceType"] == "Appointment"
        assert fhir["id"] == "appt-456"
        assert fhir["status"] == "booked"
        assert fhir["serviceType"][0]["text"] == "Cardiology"
        assert fhir["reasonCode"][0]["text"] == "Mild chest pain"
        assert fhir["start"] == "2026-07-01T10:30:00+05:30"

        participants = {
            p["actor"].get("display"): p["status"] for p in fhir["participant"]
        }
        assert "Rahul Sharma" in participants
        assert "Dr. Smith" in participants

    def test_lab_report_to_fhir(self):
        report_db = {
            "id": "rep-789",
            "status": "sent",
            "report_name": "CBC Test",
            "report_type": "Blood Test",
            "patient_name": "Rahul Sharma",
            "patient_phone": "+919876543210",
            "file_url": "https://storage.example.com/cbc.pdf",
            "created_at": "2026-06-28T11:00:00Z",
        }

        fhir = lab_report_to_fhir(report_db)
        assert fhir["resourceType"] == "DiagnosticReport"
        assert fhir["status"] == "final"
        assert fhir["code"]["text"] == "CBC Test"
        assert fhir["presentedForm"][0]["url"] == "https://storage.example.com/cbc.pdf"
        assert fhir["presentedForm"][0]["contentType"] == "application/pdf"

    def test_create_fhir_bundle(self):
        pat = patient_to_fhir({"id": "p1", "name": "Patient One"})
        appt = appointment_to_fhir({"id": "a1", "status": "confirmed"})

        bundle = create_fhir_bundle([pat, appt], bundle_type="searchset")
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "searchset"
        assert bundle["total"] == 2
        assert len(bundle["entry"]) == 2
        assert bundle["entry"][0]["resource"]["resourceType"] == "Patient"
        assert bundle["entry"][1]["resource"]["resourceType"] == "Appointment"
