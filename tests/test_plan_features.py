# tests/test_plan_features.py
"""Tests for the flat ALL_FEATURES list derived from PLAN_FEATURES."""

from app.services.tenant import ALL_FEATURES, PLAN_FEATURES, has_feature


def test_all_features_excludes_wildcard_sentinel():
    assert "*" not in ALL_FEATURES


def test_all_features_is_sorted_and_deduplicated():
    assert ALL_FEATURES == sorted(set(ALL_FEATURES))


def test_all_features_contains_every_named_plan_feature():
    named = {f for feats in PLAN_FEATURES.values() for f in feats if f != "*"}
    assert set(ALL_FEATURES) == named


def test_soloclinic_features_subset_of_all_features():
    clinic = {"plan": "soloclinic"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "booking" in resolved
    assert "lab_reports" not in resolved  # soloclinic doesn't have this feature


def test_diagstream_has_lab_reports_not_booking():
    clinic = {"plan": "diagstream"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "lab_reports" in resolved
    assert "booking" not in resolved
