"""MocDoc partial approvals: tests approved after an order's first delivery
must be sent — and only those, never the already-delivered ones again.

Accumx, Oct 2026: a patient's order had 10 tests, 5 were approved, the
connector sent those 5 and recorded the order (VAM_ReportNo) as processed.
The other 5, approved hours later, were never sent: every dedup layer keyed
on the order id, not on which tests the PDF held.
"""

import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from connectors.base import ReportMetadata
from connectors.mocdoc.worker import (
    MocDocConnector,
    _belongs_to_order,
    _plan_delivery,
    _topup_report_id,
    _topup_report_name,
)

BASE = "VAM-55492_29220"
FIRST5 = ["URIC ACID", "ELECTROLYTES, SERUM", "UREA", "SERUM CREATININE", "LIPID PROFILE"]
LATER5 = ["TOTAL LFT", "RBS (RANDOM BLOOD SUGAR)", "BLOOD UREA NITROGEN - BUN", "HBA1C", "TSH"]


class FakeModal:
    """#download-modal as MocDoc renders it: a header row, one row per
    approved test (all ticked by default), and the "Download By" options."""

    def __init__(self, tests, stuck=()):
        self.rows = [{"text": "Test Name", "checked": True, "disabled": False}]
        self.rows += [{"text": t, "checked": True, "disabled": False} for t in tests]
        self.rows += [{"text": f"Download By {x}", "checked": False, "disabled": False}
                      for x in ("Dept", "Test", "Sampleid")]
        self.stuck = set(stuck)  # checkboxes whose click MocDoc ignores
        self.clicks = []

    def page(self):
        page = MagicMock()

        async def evaluate(script, arg=None):
            assert "download-modal" in script
            return [dict(idx=i, **r) for i, r in enumerate(self.rows)]

        def locator(sel):
            assert sel == "#download-modal tr"
            rows = MagicMock()

            def nth(i):
                cb = MagicMock()

                async def set_checked(value, timeout=None):
                    self.clicks.append((self.rows[i]["text"], value))
                    if self.rows[i]["text"] not in self.stuck:
                        self.rows[i]["checked"] = value

                cb.first.set_checked = set_checked
                row = MagicMock()
                row.locator = MagicMock(return_value=cb)
                return row

            rows.nth = nth
            return rows

        page.evaluate = evaluate
        page.locator = locator
        page.wait_for_timeout = AsyncMock()
        return page

    def ticked(self):
        return {r["text"] for r in self.rows[1:-3] if r["checked"]}


def _worker(modal):
    w = MocDocConnector(
        clinic_id="clinic-1", config={"username": "u", "password": "p"},
        medassist_url="http://x", integration_secret="s",
        session_dir=tempfile.mkdtemp(),
    )
    w._page = modal.page()
    return w


def _meta():
    return ReportMetadata("Mr.X", "+919800000000", "URIC ACID", "Laboratory", BASE, vam_id="VAM-55492")


# ── decision table ──────────────────────────────────────────────────────────

def test_plan_first_delivery_sends_everything_offered():
    assert _plan_delivery(set(FIRST5), set(), base_seen=False) == ("send_all", set(FIRST5))


def test_plan_later_approvals_send_only_new_tests():
    assert _plan_delivery(set(FIRST5 + LATER5), set(FIRST5), True) == ("send_new", set(LATER5))


def test_plan_nothing_new_is_skipped():
    assert _plan_delivery(set(FIRST5), set(FIRST5), True) == ("skip", set())


def test_plan_legacy_order_is_baselined_not_resent():
    # Delivered before per-test tracking: deploy must not resend every order.
    assert _plan_delivery(set(FIRST5), None, True) == ("baseline", set(FIRST5))


def test_plan_unreadable_modal_keeps_original_whole_order_rule():
    assert _plan_delivery(set(), set(), False) == ("send_all", set())
    assert _plan_delivery(set(), set(FIRST5), True) == ("skip", set())


def test_topup_id_is_stable_and_scoped_to_its_order():
    a = _topup_report_id(BASE, set(LATER5))
    assert a == _topup_report_id(BASE, set(reversed(LATER5)))  # order-insensitive
    assert a != _topup_report_id(BASE, set(LATER5[:4]))
    assert _belongs_to_order(a, BASE) and _belongs_to_order(BASE, BASE)
    assert not _belongs_to_order("VAM-55492_292201", BASE)  # a different report No
    assert not _belongs_to_order("VAM-554921_29220", BASE)


def test_topup_name_is_one_short_line():
    name = _topup_report_name(LATER5)
    assert "\n" not in name and len(name) <= 80 and name.startswith("Updated:")


# ── the modal, end to end ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_poll_downloads_all_approved_tests_untouched():
    modal = FakeModal(FIRST5)
    w, meta = _worker(modal), _meta()
    assert await w._apply_delivery_plan(BASE, meta, (False, set())) is True
    assert modal.clicks == []  # default selection, exactly as before the fix
    assert meta.external_report_id == BASE
    assert set(meta.test_names) == set(FIRST5)
    assert meta.report_name == "URIC ACID"  # first send keeps its name


@pytest.mark.asyncio
async def test_later_poll_unticks_delivered_tests_and_sends_only_new_ones():
    modal = FakeModal(FIRST5 + LATER5)
    w, meta = _worker(modal), _meta()
    assert await w._apply_delivery_plan(BASE, meta, (True, set(FIRST5))) is True
    assert modal.ticked() == set(LATER5)
    assert meta.external_report_id == _topup_report_id(BASE, set(LATER5))
    assert set(meta.test_names) == set(LATER5)
    assert meta.report_name.startswith("Updated:")


@pytest.mark.asyncio
async def test_poll_with_nothing_new_downloads_nothing_and_counts_as_skipped():
    modal = FakeModal(FIRST5)
    w, meta = _worker(modal), _meta()
    assert await w._apply_delivery_plan(BASE, meta, (True, set(FIRST5))) is False
    assert modal.clicks == []
    # runner.py counts a None download whose id is in _processed_ids as skipped
    assert meta.external_report_id == BASE and BASE in w._processed_ids


@pytest.mark.asyncio
async def test_legacy_order_records_baseline_and_sends_nothing():
    modal = FakeModal(FIRST5)
    w, meta = _worker(modal), _meta()
    with patch.object(w, "_record_baseline", AsyncMock()) as rec:
        assert await w._apply_delivery_plan(BASE, meta, (True, None)) is False
    rec.assert_awaited_once_with(BASE, set(FIRST5))
    assert BASE in w._processed_ids


@pytest.mark.asyncio
async def test_untick_that_does_not_stick_aborts_instead_of_resending_old_results():
    modal = FakeModal(FIRST5 + LATER5, stuck={"UREA"})
    w, meta = _worker(modal), _meta()
    with pytest.raises(RuntimeError, match="not downloading"):
        await w._apply_delivery_plan(BASE, meta, (True, set(FIRST5)))
    # Failure is recorded under the top-up id, so it alerts instead of
    # being silently counted as "already processed".
    assert meta.external_report_id == _topup_report_id(BASE, set(LATER5))
    assert meta.external_report_id not in w._processed_ids


@pytest.mark.asyncio
async def test_failed_lookup_falls_back_to_whole_order_dedup():
    modal = FakeModal(FIRST5)
    w, meta = _worker(modal), _meta()
    assert await w._apply_delivery_plan(BASE, meta, None) is True
    assert modal.clicks == [] and meta.external_report_id == BASE


@pytest.mark.asyncio
async def test_order_state_unions_base_and_topup_rows_only():
    w = _worker(FakeModal([]))
    rows = [
        {"external_report_id": BASE, "test_names": FIRST5},
        {"external_report_id": _topup_report_id(BASE, {"TSH"}), "test_names": ["TSH"]},
        {"external_report_id": BASE + "1", "test_names": ["NOT THIS ORDER"]},  # LIKE over-match
    ]
    with patch("connectors.mocdoc.worker.sb", AsyncMock(return_value=MagicMock(data=rows))):
        assert await w._order_delivery_state(BASE) == (True, set(FIRST5) | {"TSH"})
    with patch("connectors.mocdoc.worker.sb",
               AsyncMock(return_value=MagicMock(data=[{"external_report_id": BASE, "test_names": None}]))):
        assert await w._order_delivery_state(BASE) == (True, None)
    with patch("connectors.mocdoc.worker.sb", AsyncMock(return_value=MagicMock(data=[]))):
        assert await w._order_delivery_state(BASE) == (False, set())
