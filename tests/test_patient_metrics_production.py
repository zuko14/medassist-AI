import os
import sys
import asyncio
import pytest
from app.utils.validators import validate_name
from app.services.analytics import analytics_service
from app.database import get_genuine_patients, supabase, sb

# The three tests below query the LIVE Supabase project by hard-coded clinic
# UUID — they verify real production data, not application logic, so they
# cannot pass against the dummy credentials the rest of the suite runs on.
# Without this gate they failed for everyone and their signal was lost in the
# noise. Point SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY at the real project and
# set RUN_LIVE_SUPABASE_TESTS=1 to run them.
_LIVE_SUPABASE = pytest.mark.skipif(
    os.getenv("RUN_LIVE_SUPABASE_TESTS") != "1",
    reason="needs live Supabase credentials; set RUN_LIVE_SUPABASE_TESTS=1",
)


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


async def _clinical_phones(clinic_id: str) -> set:
    """Phones with an appointment, lab report, or prescription at this clinic."""
    phones = set()
    for table in ("appointments", "lab_reports", "prescriptions"):
        res = await sb(
            supabase.table(table)
            .select("patient_phone")
            .eq("clinic_id", clinic_id)
            .limit(2000)
        )
        phones |= {r["patient_phone"] for r in (res.data or []) if r.get("patient_phone")}
    return phones


@_LIVE_SUPABASE
@pytest.mark.asyncio
async def test_visakha_clinic_counts_only_genuine_patients():
    """Metrics must reflect clinical engagement, not raw WhatsApp contacts.

    This used to assert a hardcoded 0 against a live clinic, so it broke the
    moment a real patient booked (MC-2026-KR4692AD with Dr. Latchireddy Naidu)
    and total_appointments went 0 -> 1. A real booking is not a regression. The
    invariant worth guarding is the FILTER: everything counted has genuine
    engagement, everything excluded has none, and the dashboard total agrees
    with get_genuine_patients — all true at any volume.
    """
    stats = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
    genuine = await get_genuine_patients(VISAKHA_CLINIC_ID)

    assert stats["total_patients"] == len(genuine), (
        f"Dashboard total_patients ({stats['total_patients']}) disagrees with "
        f"get_genuine_patients ({len(genuine)}) — the two filters have drifted apart"
    )

    clinical_phones = await _clinical_phones(VISAKHA_CLINIC_ID)

    # Everything counted is genuine: a visit, clinical history, or a real name.
    for patient in genuine:
        name = (patient.get("name") or "").strip()
        assert (
            (patient.get("visit_count") or 0) > 0
            or patient.get("phone") in clinical_phones
            or len(name) >= 3
        ), f"Unengaged contact counted as a patient: {patient.get('phone')}"

    # Nothing genuine was filtered out.
    raw = await sb(
        supabase.table("patients").select("*").eq("clinic_id", VISAKHA_CLINIC_ID).limit(2000)
    )
    genuine_phones = {p["phone"] for p in genuine}
    for patient in (raw.data or []):
        if patient["phone"] in genuine_phones:
            continue
        assert (patient.get("visit_count") or 0) == 0, (
            f"Patient {patient['phone']} has visits but was filtered out"
        )
        assert patient["phone"] not in clinical_phones, (
            f"Patient {patient['phone']} has clinical history but was filtered out"
        )

    # Breakdowns stay internally consistent whatever today's volume is.
    assert (
        stats["confirmed"] + stats["cancelled"] + stats["completed"]
        <= stats["total_appointments"]
    )
    assert stats["new_patients"] <= stats["total_patients"]


@_LIVE_SUPABASE
@pytest.mark.asyncio
async def test_unengaged_whatsapp_ping_does_not_inflate_metrics():
    """A raw WhatsApp contact with no visits must not move the counters.

    Measured as a DELTA against a baseline taken in the same run. The clinic is
    live, so its absolute counts change underneath the test — only the change
    this test causes is attributable to the filter under test.
    """
    dummy_phone = "+910000099999"
    try:
        # 1. Clean up in case exists
        await sb(supabase.table("patients").delete().eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))

        # 2. Baseline, taken now rather than assumed to be zero
        baseline = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
        baseline_genuine = len(await get_genuine_patients(VISAKHA_CLINIC_ID))

        # 3. Insert transient unengaged contact (simulating WhatsApp 'hi')
        await sb(supabase.table("patients").insert({
            "clinic_id": VISAKHA_CLINIC_ID,
            "phone": dummy_phone,
            "name": None,
            "visit_count": 0,
            "opted_in": True,
        }))

        # 4. It must not increment anything
        stats = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
        assert stats["total_patients"] == baseline["total_patients"], (
            "Unengaged WhatsApp ping inflated total_patients "
            f"({baseline['total_patients']} -> {stats['total_patients']})"
        )
        assert stats["new_patients"] == baseline["new_patients"], (
            "Unengaged WhatsApp ping inflated new_patients "
            f"({baseline['new_patients']} -> {stats['new_patients']})"
        )

        genuine = await get_genuine_patients(VISAKHA_CLINIC_ID)
        assert len(genuine) == baseline_genuine, (
            f"Genuine patient count moved on an unengaged ping "
            f"({baseline_genuine} -> {len(genuine)})"
        )
        assert dummy_phone not in {p["phone"] for p in genuine}, (
            "Unengaged contact was included in genuine patients!"
        )

        # 5. Now simulate booking an appointment (visit_count becomes 1)
        await sb(supabase.table("patients").update({"visit_count": 1, "name": "Real Patient"}).eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))

        # 6. Exactly one patient is added — not zero, not two
        stats_after = await analytics_service.get_dashboard_stats(VISAKHA_CLINIC_ID, days=30)
        assert stats_after["total_patients"] == baseline["total_patients"] + 1, (
            "Genuine patient with visits was not counted "
            f"({baseline['total_patients']} -> {stats_after['total_patients']})"
        )
        assert stats_after["new_patients"] == baseline["new_patients"] + 1, (
            "Genuine new patient was not counted "
            f"({baseline['new_patients']} -> {stats_after['new_patients']})"
        )

        genuine_after = await get_genuine_patients(VISAKHA_CLINIC_ID)
        assert dummy_phone in {p["phone"] for p in genuine_after}, (
            "Patient with a visit was still excluded from genuine patients!"
        )

    finally:
        # Cleanup dummy row
        await sb(supabase.table("patients").delete().eq("clinic_id", VISAKHA_CLINIC_ID).eq("phone", dummy_phone))


@_LIVE_SUPABASE
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
