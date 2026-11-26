"""Specialty OS zero impact (docs/specialty-os/README.md §4; continuous-care VERIFICATION zero-impact rules).

Off by default for every plan (the enterprise wildcard and the hybrids never switch it on); owner toggles enforce plan
eligibility and the shared block first; a clinic without the module: no SP query from the prescription PDF path, the
WhatsApp conversation, DPDP erasure or clinic deletion; the prescription PDF is byte-identical (tests/test_sp_forms.py
golden); existing specialty / dental suites and the webhook / FSM / voice suites are untouched (git diff empty); no
trigger lands on an existing table (tests/test_migration_sp_chain.py); UI blocks live inside SP markers only.
"""

import asyncio
import re
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tenant import OPT_IN_FEATURES, PLAN_FEATURES, SP_PACKS, has_feature, sp_access, sp_eligible
from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _clinic, _patient, _q, api  # noqa: F401  (fixture)
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SP_FLAGS = ("specialty_os",) + tuple(flag for flag, _ in SP_PACKS.values())


def _sp(paths):
    return [p for p in paths if p.rsplit("/", 1)[-1].startswith(("sp_", "opd_encounter_forms", "ivf_", "obs_", "imm_"))]


@pytest.mark.parametrize("plan", sorted(PLAN_FEATURES))
def test_off_by_default_on_every_plan_and_never_by_wildcard(plan):
    clinic = {"plan": plan, "account_type": "tenant", "features": {"opd_enabled": True}}
    assert sp_access(clinic) is False and not any(sp_access(clinic, p) for p in SP_PACKS)
    for flag in SP_FLAGS:
        assert flag in OPT_IN_FEATURES and flag not in PLAN_FEATURES[plan]
    if plan == "enterprise":
        assert has_feature(clinic, "specialty_os")                    # the wildcard says yes …
        assert not sp_access(clinic)                                  # … and the gate never asks it
    partner = {**clinic, "account_type": "partner", "features": {"opd_enabled": True, "specialty_os": True}}
    assert not sp_eligible(partner)


def test_owner_toggles_enforce_eligibility(api):  # noqa: F811
    c = api.conn
    solo = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES ('S', %s, 'soloclinic', "
                     "true, '{\"opd_enabled\": true}') RETURNING id", "+9193" + str(uuid.uuid4().int)[:8]))
    r = api.owner_toggle(solo, "specialty_os", True)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "sp_not_eligible"
    eye = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES ('E', %s, 'eye', true, "
                    "'{\"opd_enabled\": true}') RETURNING id", "+9193" + str(uuid.uuid4().int)[:8]))
    assert api.owner_toggle(eye, "optical_enabled", True).json()["detail"]["code"] == "sp_requires_shared"
    assert api.owner_toggle(eye, "specialty_os", True).status_code == 200
    assert api.owner_toggle(eye, "ivf_lab_enabled", True).json()["detail"]["code"] == "sp_pack_not_eligible"
    assert api.owner_toggle(eye, "optical_enabled", True).status_code == 200
    no_opd = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES ('N', %s, 'derma', "
                       "true, '{}') RETURNING id", "+9193" + str(uuid.uuid4().int)[:8]))
    assert api.owner_toggle(no_opd, "specialty_os", True).status_code == 400
    assert api.owner_toggle(eye, "specialty_os", False).status_code == 200        # switching off never deletes


def test_conversation_never_imports_the_hook_for_a_clinic_without_the_module():
    import sys

    from app.services.conversation import ConversationManager
    sys.modules.pop("app.services.sp_hooks", None)
    clinic = {"id": "c1", "name": "H", "config": {}, "plan": "eye", "account_type": "tenant",
              "features": {"opd_enabled": True}}
    session = {"state": "main_menu", "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    m = ConversationManager()
    m.whatsapp = AsyncMock()
    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)), \
            patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value={"id": "p", "language": "en"})), \
            patch("app.services.conversation.update_conversation", new=AsyncMock()), \
            patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")):
        try:
            asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000001", message="x",
                                                 message_type="interactive", message_id="w",
                                                 interactive_data={"id": f"spest:{uuid.uuid4()}:a"}))
        except Exception:                                                            # noqa: BLE001 - only the import matters
            pass
    assert "app.services.sp_hooks" not in sys.modules


def test_erasure_and_prescription_pdf_make_no_sp_query_without_the_module(api, spy):  # noqa: F811
    from app.services import sp
    from app.services.data_retention import DataRetentionService
    clinic = _clinic(api.conn)
    pid = _patient(api.conn, clinic)
    phone = _q(api.conn, "SELECT phone FROM patients WHERE id = %s", pid)
    before = len(spy)
    asyncio.run(DataRetentionService().anonymize_clinical_records(clinic, phone))
    assert asyncio.run(sp.forms_for_print({"id": clinic, "plan": "polyclinic", "features": {"opd_enabled": True}}, "e")) is None
    assert not _sp(spy[before:]) and not [p for p in spy[before:] if "sp_purge" in p]


def test_erasure_reaches_sp_at_a_specialty_clinic(api, spy):  # noqa: F811
    from app.services.data_retention import DataRetentionService
    w = sp_world(api, plan="derma", packs=("derma",))
    phone = _q(api.conn, "SELECT phone FROM patients WHERE id = %s", w.patient)
    before = len(spy)
    out = asyncio.run(DataRetentionService().anonymize_clinical_records(w.clinic, phone))
    assert [p for p in spy[before:] if "sp_purge_patient" in p] and "specialty_rows_purged" in out


def test_protected_suites_are_unedited():
    files = ["tests/test_specialty_plans.py", "tests/test_dental_plans.py", "tests/test_webhook.py",
             "tests/test_fsm_transitions.py", "tests/voice/", "tests/test_plan_registry_drift.py"]
    out = subprocess.run(["git", "diff", "--stat", "HEAD", "--", *files], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "", out.stdout


def test_ui_lives_inside_sp_markers_and_uses_tokens_only():
    html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    css = re.findall(r"/\* SP:START.*?/\* SP:END \*/", html, re.S)
    assert css, "SP CSS block missing"
    for block in css:
        stripped = re.sub(r"color-mix\([^;]*?\)", "", block)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", stripped), "raw colour literal in the SP block"
    assert "<!-- SP:pages:START" in html and "<!-- SP:pages:END -->" in html
    pages = html[html.index("<!-- SP:pages:START"):html.index("<!-- SP:pages:END -->")]
    for pid in ("pg-speye", "pg-spderma", "pg-spdental", "pg-spivf", "pg-spwc", "pg-spshared"):
        assert f'id="{pid}"' in pages, pid
    assert "admin.js" not in pages
