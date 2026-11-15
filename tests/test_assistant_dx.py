"""Kriya Ketu × Diagnostic OS (P9, DX-55; docs/diagnostic-os/KRIYA-KETU.md §1).

For a lab with the module on, each documented sentence reaches its DX action (the same /admin/dx route the
page uses); without the module every DX action is feature-disabled; the visit search reads the visit number
as the desk prints it; hand-offs are never executable.
"""

from types import SimpleNamespace

import pytest

from app.assistant import policy, rules
from app.assistant.registry import REGISTRY

DX_LAB = {"id": "c1", "plan": "diagstream", "features": {"dx_enabled": True}}
NO_DX = {"id": "c2", "plan": "diagstream", "features": {}}
ADMIN = SimpleNamespace(role="clinic_admin", permissions=[], staff_role=None, branch_id=None)


def _route(text, clinic=DX_LAB):
    allowed = policy.permitted_actions(ADMIN, clinic)
    return rules.route(text, actions={k: REGISTRY[k] for k in allowed})


@pytest.mark.parametrize("text,action", [
    ("kitne samples pending hain", "dx.pending_by_stage"),
    ("ఎన్ని శాంపిల్స్ పెండింగ్", "dx.pending_by_stage"),
    ("कितने सैंपल बाकी", "dx.pending_by_stage"),
    ("show TAT breaches", "dx.tat_breaches"),
    ("der wale reports", "dx.tat_breaches"),
    ("reports waiting for my signature", "dx.my_approvals"),
    ("mere sign ke liye", "dx.my_approvals"),
    ("critical results today", "dx.critical_open"),
    ("reports held for payment", "dx.held_dues"),
    ("today's lab summary", "dx.today_summary"),
    ("find report of Ramana Babu", "dx.find_visit"),
    ("LAB-2026-123 status", "dx.find_visit"),
])
def test_dx_sentences_reach_their_action(text, action):
    r = _route(text)
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), (text, r)


def test_visit_search_term():
    assert _route("LAB-2026-123 status").params["q"] == "LAB-2026-000123"
    assert _route("find report of Ramana Babu").params["q"] == "ramana babu"


@pytest.mark.parametrize("text", ["register a patient for CBC", "mark sample collected", "sign this report",
                                  "resend report on WhatsApp"])
def test_handoffs_open_the_page(text):
    r = _route(text)
    spec = REGISTRY.get(r.action_id or "")
    assert spec and spec.id.startswith("dx.") and spec.risk == "H" and spec.endpoint is None, (text, r)


def test_dx_actions_are_off_without_the_module():
    allowed = set(policy.permitted_actions(ADMIN, NO_DX))
    dx = {k for k in REGISTRY if k.startswith("dx.")}
    assert len(dx) == 11 and not (dx & allowed)
    for k in dx:
        assert policy.precheck(REGISTRY[k], ADMIN, NO_DX).reason == "feature_disabled"


def test_technician_without_validate_cannot_list_my_approvals():
    tech = SimpleNamespace(role="staff", permissions=[], staff_role="LAB_OPERATOR", branch_id=None)
    assert not policy.precheck(REGISTRY["dx.my_approvals"], tech, DX_LAB).allowed
    assert policy.precheck(REGISTRY["dx.pending_by_stage"], tech, DX_LAB).allowed      # DX_READ implied
    assert not policy.precheck(REGISTRY["dx.held_dues"], SimpleNamespace(
        role="staff", permissions=["DX_COLLECT"], staff_role="FRONT_DESK", branch_id=None), DX_LAB).allowed
