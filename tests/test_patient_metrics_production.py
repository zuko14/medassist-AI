import sys
import asyncio
import pytest
from app.utils.validators import validate_name
from app.services.analytics import analytics_service
from app.database import get_genuine_patients, supabase, sb

VISAKHA_CLINIC_ID = "9d9e9f12-c775-49c0-a326-98a59cdcc2e4"
TEST_HOSPITAL_ID = "f13ea1b8-ec12-4d15-82a8-82668b74bd29"
ACCUMX_CLINIC_ID = "c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c"


def test_validate_name_rejects_bot_buttons_and_menus():
    """Verify that validate_name rejects WhatsApp menu options, button titles, and system words."""
    invalid_cases = [
        "Our Services",
        "Services",
        "Book Appointment",
        "Cancel Appointment",
        "Reschedule",
        "Menu",
        "Main Menu",
        "Doctor",
        "Hospital",
        "Clinic",
        "Emergency",
        "Help",
        "మా సేవలు",
        "సేవలు",
        "మెనూ",
        "ముఖ్య మెనూ",
        "రిపోర్టులు",
        "వైద్యులు",
        "हमारी सेवाएं",
        "सेवाएं",
        "मेनू",
        "मुख्य मेनू",
    ]
    for case in invalid_cases:
        valid, reason = validate_name(case)
        assert not valid, f"Expected '{case}' to be invalid, but got valid: {reason}"
        assert reason in ("invalid_name", "too_short", "need_full_name"), f"Unexpected reason '{reason}' for '{case}'"


def test_validate_name_accepts_legitimate_full_names():
    """Verify that validate_name accepts real human names."""
    valid_cases = [
        "Chaitanya Kumar",
        "Dr. Suresh Kumar",
        "Priya Sharma",
        "Ravi Varma",
        "చైతన్య కుమార్",
        "రవి వర్మ",
        "सुरेश कुमार",
    ]
    for case in valid_cases:
        valid, formatted = validate_name(case)
        assert valid, f"Expected '{case}' to be valid, but got invalid: {formatted}"


@pytest.mark.asyncio
async def test_visakha_clinic_zero_fake_numbers():
    """Visakha Multi Speciality Clinic must show strictly 0 appointments and 0 patients."""
    stats = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
    assert stats["total_appointments"] == 0, f"Expected 0 appointments, got {stats['total_appointments']}"
    assert stats["total_patients"] == 0, f"Expected 0 total_patients, got {stats['total_patients']}"
    assert stats["new_patients"] == 0, f"Expected 0 new_patients, got {stats['new_patients']}"
    assert stats["confirmed"] == 0
    assert stats["cancelled"] == 0
    assert stats["completed"] == 0

    patients = await get_genuine_patients(VISAKHA_CLINIC_ID)
    assert len(patients) == 0, f"Expected 0 genuine patients, got {len(patients)}"


@pytest.mark.asyncio
async def test_unengaged_whatsapp_ping_does_not_inflate_metrics():
    """Simulate a raw WhatsApp contact with no visits or appointments.
    Verify it is NOT counted in total_patients or new_patients.
    """
    dummy_phone = "+910000099999"
    try:
        # 1. Clean up in case exists
        await sb(supabase.table("patients").delete().eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))
        
        # 2. Insert transient unengaged contact (simulating WhatsApp 'hi')
        await sb(supabase.table("patients").insert({
            "clinic_id": VISAKHA_CLINIC_ID,
            "phone": dummy_phone,
            "name": None,
            "visit_count": 0,
            "opted_in": True,
        }))

        # 3. Verify stats remain 0
        stats = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
        assert stats["total_patients"] == 0, "Unengaged WhatsApp ping falsely inflated total_patients!"
        assert stats["new_patients"] == 0, "Unengaged WhatsApp ping falsely inflated new_patients!"

        genuine = await get_genuine_patients(VISAKHA_CLINIC_ID)
        assert len(genuine) == 0, "Unengaged contact was included in genuine patients!"

        # 4. Now simulate booking an appointment (visit_count becomes 1)
        await sb(supabase.table("patients").update({"visit_count": 1, "name": "Real Patient"}).eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))

        # 5. Verify stats now accurately recognize the patient
        stats_after = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
        assert stats_after["total_patients"] == 1, "Genuine patient with visits was not counted!"
        assert stats_after["new_patients"] == 1, "Genuine new patient was not counted!"

    finally:
        # Cleanup dummy row
        await sb(supabase.table("patients").delete().eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))


@pytest.mark.asyncio
async def test_other_clinics_remain_functional():
    """Verify TestHospital and Accumx retain accurate metrics without regression."""
    th_stats = await analytics_service.get_dashboard_stats(TEST_HOSPITAL_ID, days=30)
    assert "total_appointments" in th_stats
    assert "total_patients" in th_stats
    assert th_stats["total_patients"] >= 1, "TestHospital should have genuine patients"

    accumx_stats = await analytics_service.get_dashboard_stats(ACCUMX_CLINIC_ID, days=30)
    assert "total_appointments" in accumx_stats
    assert "total_patients" in accumx_stats
    assert accumx_stats["total_patients"] >= 1, "Accumx should have genuine patients from lab reports"
