"""Tests for Diagnostic Center Feature Gating and Plan Definitions."""

import pytest
from app.services.tenant import PLAN_FEATURES, has_feature
from app.services.permissions import PERMISSIONS, ROLE_PRESETS, resolve_permissions


def test_diagstream_plan_features():
    """Verify diagstream plan feature boundaries."""
    diag_features = set(PLAN_FEATURES["diagstream"])

    assert "diagnostic_reports" in diag_features
    assert "lab_reports" in diag_features
    assert "ai_report_summary" in diag_features
    assert "multilingual" in diag_features

    # Gated out from pure diagnostic centers
    assert "booking" not in diag_features
    assert "roster_management" not in diag_features
    assert "prescriptions" not in diag_features
    assert "payments_razorpay" not in diag_features


def test_polyclinic_plan_features():
    """Verify polyclinic plan includes both booking and diagnostic features."""
    poly_features = set(PLAN_FEATURES["polyclinic"])

    assert "diagnostic_reports" in poly_features
    assert "lab_reports" in poly_features
    assert "booking" in poly_features
    assert "multi_branch" in poly_features
    assert "analytics" in poly_features


def test_rbac_diagnostic_permissions_registry():
    """Verify diagnostic permissions are registered and recognized."""
    assert "REPORTS_VIEW" in PERMISSIONS
    assert "REPORTS_RESOLVE" in PERMISSIONS
    assert "CONNECTOR_MANAGE" in PERMISSIONS


def test_diagnostic_operator_role_preset():
    """Verify DIAGNOSTIC_OPERATOR role preset permissions."""
    assert "DIAGNOSTIC_OPERATOR" in ROLE_PRESETS
    perms = resolve_permissions("DIAGNOSTIC_OPERATOR", [])
    assert "REPORTS_VIEW" in perms
    assert "REPORTS_RESOLVE" in perms
    assert "CONNECTOR_MANAGE" in perms


def test_lab_operator_role_preset():
    """Verify LAB_OPERATOR role preset permissions."""
    perms = resolve_permissions("LAB_OPERATOR", [])
    assert "REPORTS_VIEW" in perms
    assert "REPORTS_RESOLVE" in perms

