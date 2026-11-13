"""DX-14: exclusive bounds in lis.flag_value (118 behaviour unchanged by default),
delta checks and auto-verification eligibility (REPORT-TEMPLATES.md §2)."""

from decimal import Decimal

import pytest

from app.services import lis
from app.services.dx_results import auto_verify_eligible, delta_check


def _band(**kw):
    base = {"id": "p1", "name": "TC", "unit": "mg/dL", "data_type": "numeric", "gender": "all", "min_age_days": 0,
            "max_age_days": 43800, "normal_low": None, "normal_high": None, "critical_low": None,
            "critical_high": None, "normal_text_value": None}
    return {**base, **kw}


@pytest.mark.parametrize("raw, want", [("6.9", "critical_low"), ("7", "low"), ("13", "normal"), ("17", "normal"),
                                       ("17.1", "high"), ("20", "high"), ("20.01", "critical_high")])
def test_118_vectors_unchanged_without_the_new_keys(raw, want):
    p = _band(normal_low=13, normal_high=17, critical_low=7, critical_high=20)
    assert lis.flag_value(p, Decimal(raw), None) == want
    assert lis.flag_value({**p, "low_exclusive": False, "high_exclusive": False}, Decimal(raw), None) == want


def test_exclusive_bounds_flag_the_bound_itself():
    tc = _band(normal_high=200, high_exclusive=True)            # "Desirable < 200": 200 is high
    assert lis.flag_value(tc, Decimal("199.9"), None) == "normal"
    assert lis.flag_value(tc, Decimal("200"), None) == "high"
    assert lis.flag_value(_band(normal_high=200), Decimal("200"), None) == "normal"   # inclusive default
    hdl = _band(normal_low=40, low_exclusive=True)
    assert lis.flag_value(hdl, Decimal("40"), None) == "low"
    assert lis.flag_value(hdl, Decimal("40.1"), None) == "normal"
    tg = _band(normal_high=150, high_exclusive=True)
    assert lis.flag_value(tg, Decimal("162"), None) == "high"
    # a critical limit still wins, and a stray non-bool value never flips the rule
    assert lis.flag_value(_band(normal_high=200, critical_high=200, high_exclusive=True), Decimal("200"), None) == "high"
    assert lis.flag_value(_band(normal_high=200, high_exclusive="yes"), Decimal("200"), None) == "normal"


def test_delta_check_both_directions_and_limits():
    d = delta_check(12.0, 10.0, "both", delta_pct=15)
    assert d["delta_failed"] and d["change_pct"] == Decimal("20.00") and d["change_abs"] == Decimal("2.0")
    assert not delta_check(11.0, 10.0, "both", delta_pct=15)["delta_failed"]
    assert delta_check(8.0, 10.0, "both", delta_abs=1.5)["delta_failed"]
    assert not delta_check(9.0, 10.0, "both", delta_abs=1.5)["delta_failed"]
    # either limit is enough
    assert delta_check(10.4, 10.0, "both", delta_pct=50, delta_abs=0.3)["delta_failed"]


def test_delta_direction_only_counts_its_own_way():
    assert not delta_check(5.0, 10.0, "increase", delta_pct=10)["delta_failed"]    # a fall is fine
    assert delta_check(15.0, 10.0, "increase", delta_pct=10)["delta_failed"]
    assert not delta_check(15.0, 10.0, "decrease", delta_pct=10)["delta_failed"]
    assert delta_check(5.0, 10.0, "decrease", delta_pct=10)["delta_failed"]


def test_delta_not_applicable_cases():
    assert delta_check(5, None, "both", delta_pct=10) is None          # first result for this person
    assert delta_check(None, 5, "both", delta_pct=10) is None
    assert delta_check(5, 4, None, delta_pct=10) is None                # not configured
    assert delta_check(5, 4, "both") is None                            # no limit
    zero = delta_check(1, 0, "both", delta_pct=10)                     # percent undefined from 0 …
    assert zero["change_pct"] is None and not zero["delta_failed"]
    assert delta_check(1, 0, "both", delta_pct=10, delta_abs=0.5)["delta_failed"]   # … the absolute limit still works


def test_auto_verify_eligibility():
    ok = [{"flag": "normal"}, {"flag": "normal", "delta_failed": False, "remark": " "}]
    assert auto_verify_eligible(ok)
    for bad in ({"flag": "high"}, {"flag": "not_evaluated"}, {"flag": "critical_low"}, {"flag": "abnormal"},
                {"flag": "normal", "delta_failed": True}, {"flag": "normal", "remark": "haemolysed"},
                {"flag": "normal", "is_amendment": True}, {}):
        assert not auto_verify_eligible(ok + [bad]), bad
    assert not auto_verify_eligible([])
