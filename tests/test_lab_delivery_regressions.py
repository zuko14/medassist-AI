"""Regressions behind the Accumax diagnostic-center report-delivery incident.

Each test fails if one of the four root causes comes back:

1. The distributed phone lease was never released on the success path, so a
   patient with two reports in one connector run lost the second to
   "another delivery in progress".
2. A `patients` row with a NULL name scored 0.00 against the scraped name and
   blocked that patient's reports forever — while a phone with no record at
   all sent on confidence 1.0.
3. Outside the 24h window the delivery template carries no summary text, so
   the AI summary was generated, stored, and never sent — while the dashboard
   read the stored text as proof of delivery.
4. The template send and its Meta-500 signed-URL retry built their body
   parameters independently, so the retry could send 2 parameters against a
   3-variable template.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import message_queue as mq
from app.services.lab_reports import (
    flatten_for_template_param,
    report_template_and_params,
)
from app.services.patient_match import PatientMatchService


# ── 1. Phone lock releases every layer it acquired ────────────────────────────

def test_release_frees_distributed_lease_and_refcount():
    """A second delivery to the same phone must not be refused."""
    phone = "+919494780490"
    job = f"phone_{phone[-10:]}"
    outstanding: set[str] = set()
    released: list[str] = []

    class FakeDistributedLock:
        async def acquire(self, job_name, lease_seconds=300):
            if job_name in outstanding:
                return False
            outstanding.add(job_name)
            return True

        async def release(self, job_name):
            released.append(job_name)
            outstanding.discard(job_name)
            return True

    fake = FakeDistributedLock()

    async def scenario():
        with patch(
            "app.services.distributed_lock.distributed_lock_manager", fake
        ):
            # Report 1 for this patient.
            assert await mq.acquire_phone_lock_with_timeout(phone, timeout=1)
            await mq.release_phone_lock_acquired(phone)

            # Report 2 for the SAME patient, immediately after — this is the
            # case that used to fail with "another delivery in progress".
            assert await mq.acquire_phone_lock_with_timeout(phone, timeout=1), (
                "second report to the same patient was refused — the "
                "distributed lease was not released"
            )
            await mq.release_phone_lock_acquired(phone)

    asyncio.run(scenario())

    assert released.count(job) == 2, released
    assert not outstanding, "distributed lease still held after release"
    # No leaked refcount entry for this phone.
    assert phone not in mq._phone_locks, "phone lock entry leaked"
    assert phone not in mq._phone_refcounts, "phone refcount leaked"


def test_release_is_safe_when_lock_was_never_held():
    """Releasing an unheld lock must not raise and mask the caller's error."""

    async def scenario():
        with patch(
            "app.services.distributed_lock.distributed_lock_manager.release",
            new=AsyncMock(return_value=True),
        ):
            await mq.release_phone_lock_acquired("+919999999999")

    asyncio.run(scenario())


# ── 2. A nameless patient record must not block delivery forever ─────────────

def _match(records, scraped_name="Mr.Morthala Bala Venkateswara Reddy"):
    svc = PatientMatchService()

    class Res:
        data = records

    class Q:
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return Res()

    class DB:
        def table(self, *a, **k):
            return Q()

    with patch("app.services.patient_match.supabase", DB()):
        return asyncio.run(
            svc.match(
                clinic_id="c1",
                scraped_name=scraped_name,
                scraped_phone="+917893551146",
            )
        )


def test_null_named_record_does_not_block_delivery():
    res = _match([{"id": "p1", "name": None, "phone": "+917893551146"}])
    assert res.is_safe_to_send, res.review_reason
    assert res.match_source == "unnamed_record"
    assert res.matched_patient_id == "p1"


def test_blank_named_record_does_not_block_delivery():
    res = _match([{"id": "p1", "name": "   ", "phone": "+917893551146"}])
    assert res.is_safe_to_send, res.review_reason


def test_genuine_name_conflict_still_blocks():
    """The safety gate must survive the fix."""
    res = _match([{"id": "p1", "name": "Sunita Devi", "phone": "+917893551146"}])
    assert not res.is_safe_to_send
    assert res.match_source == "conflict"


def test_nameless_record_ignored_when_a_named_one_matches():
    res = _match([
        {"id": "p1", "name": None, "phone": "+917893551146"},
        {"id": "p2", "name": "Morthala Bala Venkateswara Reddy",
         "phone": "+917893551146"},
    ])
    assert res.is_safe_to_send
    assert res.matched_patient_id == "p2"


# ── 3 & 4. Template choice and body parameters stay in lockstep ──────────────

CLINIC_NO_SUMMARY_TPL = {"config": {}}
CLINIC_WITH_SUMMARY_TPL = {
    "config": {"lab_report_summary_template_name": "lab_report_with_summary"}
}


def test_without_summary_template_summary_is_not_claimed_as_delivered():
    name, params, carries = report_template_and_params(
        CLINIC_NO_SUMMARY_TPL, "Ravi", "CBC", "Your haemoglobin is slightly low."
    )
    assert carries is False, (
        "no approved 3-variable template exists — must not claim delivery"
    )
    assert len(params) == 2
    assert name == "lab_report_delivery"


def test_with_summary_template_the_summary_travels_in_the_template():
    name, params, carries = report_template_and_params(
        CLINIC_WITH_SUMMARY_TPL, "Ravi", "CBC", "Your haemoglobin is slightly low."
    )
    assert carries is True
    assert name == "lab_report_with_summary"
    assert len(params) == 3
    assert params[2]["text"] == "Your haemoglobin is slightly low."


def test_summary_template_not_used_when_there_is_no_summary():
    """A fallback (AI failed) must not send an empty third parameter."""
    for empty in (None, "", "   "):
        name, params, carries = report_template_and_params(
            CLINIC_WITH_SUMMARY_TPL, "Ravi", "CBC", empty
        )
        assert carries is False, empty
        assert len(params) == 2, empty
        assert name == "lab_report_delivery"


def test_summary_is_flattened_for_meta():
    """Meta rejects newlines, tabs and 4+ spaces inside a template parameter."""
    raw = "Line one.\n\nLine two.\tTabbed.    Wide gap."
    flat = flatten_for_template_param(raw)
    assert "\n" not in flat and "\t" not in flat
    assert "    " not in flat
    assert flat == "Line one. Line two. Tabbed. Wide gap."


def test_summary_param_is_capped():
    flat = flatten_for_template_param("x" * 5000)
    assert len(flat) <= 1024


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
