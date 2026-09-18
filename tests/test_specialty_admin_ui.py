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
               "async function generateTreatmentDescription(lang)", "async function loadStarterTreatments()",
               "async function setSelectedTreatmentsActive(active)", "window.delTreatment = async function"):
        assert fn in INDEX, fn


def test_each_description_language_has_its_own_ai_button():
    """Hindi and Telugu were fillable only by redoing English, so an operator
    who had corrected one language lost it to regenerate the other."""
    for btn, lang in (("btnTrtAi", "all"), ("btnTrtAiHi", "hi"), ("btnTrtAiTe", "te")):
        # {!r} renders the language as a quoted JS argument.
        assert 'id="{}" onclick="generateTreatmentDescription({!r})"'.format(btn, lang) in INDEX, btn
    # Each button may only write its own field.
    assert "hi:  { fields: ['f-trtDescHi'], keys: ['description_hi']" in INDEX
    assert "te:  { fields: ['f-trtDescTe'], keys: ['description_te']" in INDEX


def test_checkboxes_are_not_stretched_by_the_text_input_rule():
    """`.field input` set width:100% + 12px padding, which blew every checkbox
    out to the full width of its cell and pushed the label onto the next line.
    It was declared TWICE -- a sizing rule and a later background/border rule
    in the Controls section -- and the second one also re-applied the faint
    --border colour, undoing the 3:1 contrast an interactive control needs.
    Both must keep the exclusion."""
    assert INDEX.count('.field input:not([type="checkbox"]):not([type="radio"])') >= 4
    assert '.field input, .field select {' not in INDEX
    assert '.field input, .field select, .search-bar {' not in INDEX
    assert "border: 1.5px solid var(--text3);" in INDEX


def test_no_label_hand_rolls_its_own_checkbox_layout():
    """Every checkbox row goes through .check / .check-grid / .day-picker.
    Inline `style="display:flex"` on a label is how the panel used to paper
    over the stretching, and it hid the real bug for months."""
    assert "<label style=" not in INDEX
    assert INDEX.count('class="check-grid"') == 3
    for cls in ("staff-perm-cb", "edit-staff-perm-cb"):
        # Every permission label opens with the shared class.
        assert f'<input type="checkbox" class="{cls}"' in INDEX
    assert '<label class="check"><input type="checkbox" class="staff-perm-cb"' in INDEX


def test_native_widgets_follow_the_active_theme():
    """Without color-scheme a dark panel opens a white calendar popup. It was
    set only on .appt-date-field, so the other date and time inputs still
    opened light pickers."""
    assert "color-scheme: dark;" in INDEX
    assert "color-scheme: light;" in INDEX
    assert ".appt-date-field select { color-scheme: dark; }" not in INDEX


def test_day_picker_keeps_the_contract_submit_and_edit_rely_on():
    """The week renders as toggle chips now. submitDoctor reads
    .doc-day-cb:checked and editDoctor writes .checked, so the class and the
    day value must survive any restyling of the chip."""
    assert 'id="f-docDays" class="day-picker"' in INDEX
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert f'<input type="checkbox" class="doc-day-cb" value="{day}"' in INDEX, day
    assert "querySelectorAll('.doc-day-cb:checked')" in INDEX
    # The chip covers its own input, so the input must not keep the 18px box.
    assert '.day-picker input[type="checkbox"]::before { content: none; }' in INDEX


def test_every_treatment_endpoint_used_by_the_page_exists():
    from app.main import app as fastapi_app

    # FastAPI >= 0.13x wraps each include_router() in an _IncludedRouter, so
    # app.routes alone no longer lists the admin paths (the same unwrapping as
    # tests/test_admin_super_admin_scope_matrix.py).
    routes = []
    for r in fastapi_app.routes:
        routes.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    paths = {getattr(r, "path", "") for r in routes}
    for p in ("/admin/treatments", "/admin/treatments/{treatment_id}", "/admin/treatments/{treatment_id}/doctors",
              "/admin/treatments/status", "/admin/treatments/starter", "/admin/treatments/ai-description",
              "/admin/treatments/ai-concerns"):
        assert p in paths, p


def test_concerns_field_has_a_permission_gated_ai_suggest_button():
    """Concerns feed patient-message matching, so the button must be behind the
    same TREATMENTS_MANAGE gate as the rest of the treatment form."""
    assert 'id="btnTrtConcernsAi"' in INDEX
    assert 'class="btn btn-ghost btn-ai trt-manage" id="btnTrtConcernsAi"' in INDEX
    assert "onclick=\"generateTreatmentConcerns()\"" in INDEX
    assert "apiPost('/admin/treatments/ai-concerns'" in INDEX


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
