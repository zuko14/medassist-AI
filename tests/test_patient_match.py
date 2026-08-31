"""Tests for PatientMatchService."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.patient_match import (
    PatientMatchService,
    compute_name_similarity,
    normalize_name,
    MatchResult,
)


def test_normalize_name():
    assert normalize_name("Mr. Ramesh Kumar") == "ramesh kumar"
    assert normalize_name("Mrs. C Varalakshmi") == "c varalakshmi"
    assert normalize_name("Baby of Smt. Anita Devi") == "anita devi"
    assert normalize_name("Dr. K. Srinivas Rao") == "k srinivas rao"
    assert normalize_name("") == ""


def test_compute_name_similarity():
    # Exact match after normalization
    assert compute_name_similarity("Mr. John Doe", "John Doe") == 1.0
    # Token reordering
    sim1 = compute_name_similarity("Varalakshmi C", "C Varalakshmi")
    assert sim1 >= 0.85
    # Completely different names (well below the 0.75 threshold)
    sim2 = compute_name_similarity("Ramesh Kumar", "Sita Sharma")
    assert sim2 < 0.5
    # Slight spelling variation
    sim3 = compute_name_similarity("Srinivas Rao", "Srinivasa Rao")
    assert sim3 >= 0.8


def _no_patient_records():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )
    return mock_sb


@pytest.mark.asyncio
async def test_walkin_unknown_phone_is_delivered_and_flagged_unverified():
    """A diagnostic centre's walk-in report must go out automatically.

    There is no patient registry to match a walk-in against — the number is
    handed to the receptionist and typed into the HMIS — so holding on "phone
    not known to this clinic" verifies nothing and blocks every delivery. That
    happened in production: 27 discovered, 27 held, 0 delivered.

    The AUDIT-P1-1 misrouting concern is kept as a detective control instead:
    recipient_unverified drives an admin notification, and the row stays
    stamped moc_doc_only so a misroute stays findable.
    """
    service = PatientMatchService(similarity_threshold=0.75)

    with patch("app.services.patient_match.supabase", _no_patient_records()):
        result = await service.match(
            clinic_id="clinic-1",
            scraped_name="Mrs. Sunita Verma",
            scraped_phone="+919876543210",
        )

    assert result.status == "matched"
    assert result.is_safe_to_send is True
    assert result.match_source == "moc_doc_only"
    assert result.match_confidence == 1.0
    assert result.normalized_phone == "+919876543210"
    assert result.recipient_unverified is True, "delivery must be flagged for the owner"


@pytest.mark.asyncio
async def test_a_clinic_can_opt_in_to_holding_unknown_numbers():
    """A clinic that keeps a real patient registry can turn the gate back on."""
    service = PatientMatchService(similarity_threshold=0.75)

    with patch("app.services.patient_match.supabase", _no_patient_records()), patch.object(
        service, "_hold_unverified", new_callable=AsyncMock, return_value=True
    ):
        result = await service.match(
            clinic_id="clinic-1",
            scraped_name="Mrs. Sunita Verma",
            scraped_phone="+919876543210",
        )

    assert result.status == "needs_review"
    assert result.is_safe_to_send is False
    assert result.match_confidence == 0.0
    assert result.review_reason and "not registered" in result.review_reason


@pytest.mark.asyncio
async def test_per_clinic_config_overrides_the_platform_default():
    service = PatientMatchService(similarity_threshold=0.75)

    async def _clinic(_cid):
        return {"id": _cid, "config": {"hold_unknown_phone_reports": True}}

    with patch("app.services.tenant.get_clinic_by_id", new=_clinic):
        assert await service._hold_unverified("clinic-1") is True


@pytest.mark.asyncio
async def test_hold_policy_falls_back_to_platform_default_when_config_unreadable():
    """A config read failure must not silently stop a clinic's deliveries."""
    service = PatientMatchService(similarity_threshold=0.75)

    async def _boom(_cid):
        raise RuntimeError("clinic lookup failed")

    with patch("app.services.tenant.get_clinic_by_id", new=_boom), patch(
        "app.services.patient_match.settings.hold_unknown_phone_reports", False
    ):
        assert await service._hold_unverified("clinic-1") is False


@pytest.mark.asyncio
async def test_patient_match_exact_db_record():
    """Matching patient in database -> Safe to send with patients_table attribution."""
    service = PatientMatchService(similarity_threshold=0.75)
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "pat-123", "name": "Sunita Verma", "phone": "+919876543210"}]
    )

    with patch("app.services.patient_match.supabase", mock_sb):
        result = await service.match(
            clinic_id="clinic-1",
            scraped_name="Mrs. Sunita Verma",
            scraped_phone="+919876543210",
        )

    assert result.status == "matched"
    assert result.is_safe_to_send is True
    assert result.match_source == "patients_table"
    assert result.matched_patient_id == "pat-123"
    assert result.match_confidence >= 0.75


@pytest.mark.asyncio
async def test_patient_match_conflict_name_mismatch():
    """Same phone number but completely different patient name -> NEEDS_REVIEW."""
    service = PatientMatchService(similarity_threshold=0.75)
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "pat-123", "name": "Rajesh Kumar", "phone": "+919876543210"}]
    )

    with patch("app.services.patient_match.supabase", mock_sb):
        result = await service.match(
            clinic_id="clinic-1",
            scraped_name="Pooja Sharma",
            scraped_phone="+919876543210",
        )

    assert result.status == "needs_review"
    assert result.is_safe_to_send is False
    assert result.match_source == "conflict"
    assert "conflict" in result.review_reason.lower()


@pytest.mark.asyncio
async def test_patient_match_missing_or_invalid_phone():
    """Missing or garbage phone -> NEEDS_REVIEW."""
    service = PatientMatchService()
    result_none = await service.match(clinic_id="c1", scraped_name="Test", scraped_phone=None)
    assert result_none.status == "needs_review"
    assert result_none.is_safe_to_send is False
    assert result_none.match_source == "missing_phone"

    result_bad = await service.match(clinic_id="c1", scraped_name="Test", scraped_phone="123")
    assert result_bad.status == "needs_review"
    assert result_bad.is_safe_to_send is False


@pytest.mark.asyncio
async def test_patient_match_db_failure_fails_closed():
    """P0-4: Database query exception must fail closed into needs_review instead of auto-matching."""
    service = PatientMatchService(similarity_threshold=0.75)
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.side_effect = RuntimeError(
        "Database connection dropped"
    )

    with patch("app.services.patient_match.supabase", mock_sb):
        result = await service.match(
            clinic_id="clinic-1",
            scraped_name="Mrs. Sunita Verma",
            scraped_phone="+919876543210",
        )

    assert result.status == "needs_review"
    assert result.is_safe_to_send is False
    assert result.match_source == "database_error"
    assert result.match_confidence == 0.0
    assert "Database query error" in result.review_reason

