"""Tests for Prescription Pydantic Validation (Finding #4)."""

import pytest
from pydantic import ValidationError
from datetime import date

from app.routers.admin import PrescriptionCreate


def test_prescription_create_valid():
    """Verify valid PrescriptionCreate payload parses successfully."""
    payload = {
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        "medicine_name": "Paracetamol",
        "dosage": "500mg",
        "frequency": "Twice daily",
        "reminder_times": ["08:00", "20:00"],
        "start_date": "2026-08-02",
        "end_date": "2026-08-10",
        "notes": "After meals",
    }
    model = PrescriptionCreate(**payload)
    assert model.medicine_name == "Paracetamol"
    assert model.reminder_times == ["08:00", "20:00"]
    assert model.start_date == date(2026, 8, 2)
    assert model.end_date == date(2026, 8, 10)


def test_prescription_create_invalid_reminder_times():
    """Verify invalid reminder time formats fail validation."""
    payload = {
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        "medicine_name": "Paracetamol",
        "dosage": "500mg",
        "frequency": "Twice daily",
        "reminder_times": ["25:00"],  # Invalid hour
        "start_date": "2026-08-02",
        "end_date": "2026-08-10",
    }
    with pytest.raises(ValidationError) as exc:
        PrescriptionCreate(**payload)
    assert "reminder_times" in str(exc.value)


def test_prescription_create_end_date_before_start_date():
    """Verify end_date earlier than start_date fails validation."""
    payload = {
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        "medicine_name": "Paracetamol",
        "dosage": "500mg",
        "frequency": "Twice daily",
        "reminder_times": ["08:00"],
        "start_date": "2026-08-10",
        "end_date": "2026-08-02",  # Before start date
    }
    with pytest.raises(ValidationError) as exc:
        PrescriptionCreate(**payload)
    assert "end_date cannot be earlier than start_date" in str(exc.value)


def test_prescription_create_missing_required_fields():
    """Verify missing required fields raise Pydantic ValidationError."""
    payload = {
        "patient_phone": "+919876543210",
        "patient_name": "Test Patient",
        # missing medicine_name, dosage, frequency, etc.
    }
    with pytest.raises(ValidationError):
        PrescriptionCreate(**payload)
