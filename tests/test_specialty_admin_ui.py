"""Static and route checks for the specialty admin UI."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "admin" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["/derma-panel", "/eye-panel", "/dental-panel", "/ivf-panel"])
def test_specialty_urls_serve_the_same_panel_with_the_same_headers(path):
    client = TestClient(app)
    base = client.get("/admin-panel")
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.text == base.text
    assert resp.headers.get("cache-control") == base.headers.get("cache-control")
    assert resp.headers.get("content-security-policy") == base.headers.get("content-security-policy")


def test_treatments_nav_is_gated_on_specialty_not_features():
    nav = re.search(r'<div class="nav-link"[^>]*data-page="treatments"[^>]*>', INDEX)
    assert nav, "Treatments nav entry missing"
    assert "data-specialty" in nav.group(0)
    assert "data-feature" not in nav.group(0), "enterprise wildcard would show it"


def test_visibility_and_state_wiring():
    assert "let mySpecialtyEnabled = false;" in INDEX
    assert "mySpecialtyEnabled = !!me.specialty_enabled;" in INDEX
    assert "mySpecialtyEnabled = !!scoped.specialty_enabled;" in INDEX
    block = INDEX.split("function applyFeatureVisibility()")[1][:900]
    assert "[data-specialty]" in block
    assert "treatments: loadTreatments" in INDEX


def test_page_and_form_exist_with_whatsapp_limits():
    assert 'id="pg-treatments"' in INDEX
    assert 'id="f-trtShort" maxlength="24"' in INDEX
    assert 'id="f-trtDesc" rows="3" maxlength="400"' in INDEX
    for fn in ("async function loadTreatments()", "async function submitTreatment()",
               "async function generateTreatmentDescription()", "async function loadStarterTreatments()",
               "async function setSelectedTreatmentsActive(active)", "window.delTreatment = async function"):
        assert fn in INDEX, fn


def test_every_treatment_endpoint_used_by_the_page_exists():
    from app.main import app as fastapi_app

    paths = {getattr(r, "path", "") for r in fastapi_app.routes}
    for p in ("/admin/treatments", "/admin/treatments/{treatment_id}", "/admin/treatments/{treatment_id}/doctors",
              "/admin/treatments/status", "/admin/treatments/starter", "/admin/treatments/ai-description"):
        assert p in paths, p


def test_staff_can_be_granted_treatments_permission_in_both_forms():
    assert INDEX.count('value="TREATMENTS_MANAGE"') == 2
    assert 'class="staff-perm-cb" value="TREATMENTS_MANAGE"' in INDEX
    assert 'class="edit-staff-perm-cb" value="TREATMENTS_MANAGE"' in INDEX


def test_booking_rows_show_the_treatment():
    cell = INDEX.split("function bookingSubjectCell(b)")[1][:900]
    assert "b.treatment_name" in cell
    assert "esc(b.treatment_name)" in cell
    assert "b.booking_type === 'lab_test'" in cell


def test_ai_draft_is_labelled_as_a_draft():
    assert "AI text is a draft" in INDEX
