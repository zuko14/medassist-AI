"""KK-P1-04: Kriya Ketu action registry rules and drift tests (action-registry-spec.md §1, §2, §5; RT-14).

The drift checks run over every registered action, so each action added in P4
is checked automatically. Endpoint-backed actions are registered only in P4
(after the KK-P0-15 hardening checklist), so the checkers are also exercised
here against real routes to prove they catch drift.
"""

import inspect
import re
from pathlib import Path

import pytest

from app.assistant.help import HELP
from app.assistant.registry import (
    ADMIN_ROLES,
    PAGES,
    REGISTRY,
    ActionSpec,
    Handoff,
    validate,
)
from app.main import app
from app.routers.admin import require_admin

HTML = (Path(__file__).resolve().parent.parent / "admin" / "index.html").read_text(encoding="utf-8")


def _noop(*_a, **_k):
    return None


# ── route introspection (same flattening as the scope matrix) ────────────────

def _routes():
    out = {}
    for r in app.routes:
        for sub in (r.original_router.routes if hasattr(r, "original_router") else [r]):
            for m in getattr(sub, "methods", None) or ():
                out[(m, getattr(sub, "path", ""))] = sub
    return out


ROUTES = _routes()


def _guards(dependant, found=None):
    """Permissions from require_permission(P) closures, plus 'admin' for require_admin."""
    found = set() if found is None else found
    for sub in dependant.dependencies:
        call = sub.call
        if call is require_admin:
            found.add("admin")
        elif inspect.isfunction(call):
            perm = inspect.getclosurevars(call).nonlocals.get("perm")
            if isinstance(perm, str):
                found.add(perm)
        _guards(sub, found)
    return found


def drift(spec: ActionSpec) -> list[str]:
    """Every way the spec disagrees with its real route ([] = none)."""
    if spec.endpoint is None:
        return []
    route = ROUTES.get(spec.endpoint)
    if route is None:
        return [f"{spec.id}: no route {spec.endpoint}"]
    guards = _guards(route.dependant)
    perms = guards - {"admin"}
    src = spec.perm_source
    if src == "dependency" and perms != {spec.permission}:
        return [f"{spec.id}: route requires {sorted(perms) or 'no permission'}, spec says {spec.permission}"]
    if src == "role" and "admin" not in guards:
        return [f"{spec.id}: route has no require_admin"]
    if src in ("none", "handler") and perms:
        return [f"{spec.id}: route has dependency permission {sorted(perms)}; use perm_source='dependency'"]
    if src == "none" and "admin" in guards:
        return [f"{spec.id}: route requires admin; use perm_source='role'"]
    return []


# ── RT-14: page table = sidebar ──────────────────────────────────────────────

def test_page_table_equals_the_sidebar():
    sidebar = set(re.findall(r'class="nav-link[^"]*"[^>]*data-page="([a-z]+)"', HTML))
    assert set(PAGES) == sidebar


def test_every_page_has_a_section_go_can_open():
    sections = set(re.findall(r'id="pg-([a-z]+)"', HTML))
    assert set(PAGES) <= sections


# ── registry contents ─────────────────────────────────────────────────────────

def test_v1_skeleton_actions_are_registered():
    assert REGISTRY["nav.open"].risk == "R0"
    assert REGISTRY["help.howto"].risk == "R0"


@pytest.mark.parametrize("spec", list(REGISTRY.values()), ids=list(REGISTRY))
def test_every_registered_action_is_valid_and_matches_its_route(spec):
    validate(spec)
    assert drift(spec) == []


def test_handoff_fields_exist_on_the_page():
    for spec in (s for s in REGISTRY.values() if s.handoff):
        for element_id, _ in spec.handoff.fields:
            assert f'id="{element_id}"' in HTML, f"{spec.id}: #{element_id} missing"


# ── the drift checker itself catches drift (real routes) ─────────────────────

def _spec(**kw):
    base = dict(id="t.x", module="t", summary="t", risk="R2", call=_noop, verify=_noop)
    return ActionSpec(**{**base, **kw})


@pytest.mark.parametrize("spec", [
    _spec(endpoint=("POST", "/admin/leaves"), permission="DOCTOR_LEAVES_CREATE", perm_source="dependency"),
    _spec(endpoint=("PUT", "/admin/lab-tests/{test_id}"), permission="LAB_TESTS_MANAGE", perm_source="dependency"),
    _spec(endpoint=("POST", "/admin/appointments/{appointment_id}/check-in"), risk="R1"),
    _spec(endpoint=("POST", "/admin/bookings/{booking_id}/refund"), risk="R3", perm_source="role",
          roles=ADMIN_ROLES, max_items=1),
    # leads: registry spec §1.1 says "handler", but _LEADS is a require_permission dependency
    _spec(endpoint=("POST", "/admin/leads/message"), permission="LEADS_MANAGE", perm_source="dependency"),
], ids=["leave", "labtest", "checkin", "refund", "leads"])
def test_checker_accepts_real_routes_described_correctly(spec):
    validate(spec)
    assert drift(spec) == []


@pytest.mark.parametrize("spec, needle", [
    (_spec(endpoint=("POST", "/admin/leaves"), permission="DOCTOR_LEAVES_DELETE", perm_source="dependency"),
     "spec says DOCTOR_LEAVES_DELETE"),
    (_spec(endpoint=("POST", "/admin/leaves")), "use perm_source='dependency'"),
    (_spec(endpoint=("POST", "/admin/leads/message"), permission="LEADS_MANAGE", perm_source="handler"),
     "use perm_source='dependency'"),
    (_spec(endpoint=("POST", "/admin/bookings/{booking_id}/refund")), "use perm_source='role'"),
    (_spec(endpoint=("POST", "/admin/appointments/{appointment_id}/check-in"), perm_source="role",
           roles=ADMIN_ROLES), "no require_admin"),
    (_spec(endpoint=("POST", "/admin/no-such-route")), "no route"),
], ids=["wrong-perm", "missing-perm", "leads-handler", "missing-role", "phantom-role", "no-route"])
def test_checker_reports_drift(spec, needle):
    problems = drift(spec)
    assert problems and needle in problems[0]


# ── registration rules (§1.1, §2) ────────────────────────────────────────────

@pytest.mark.parametrize("kw, needle", [
    (dict(risk="R9"), "risk"),
    (dict(perm_source="magic"), "perm_source"),
    (dict(permission="NOT_A_PERMISSION", perm_source="dependency", endpoint=("POST", "/admin/leaves")),
     "unknown permission"),
    (dict(permission="LEADS_MANAGE", perm_source="none", endpoint=("POST", "/admin/leaves")), "must be None"),
    (dict(perm_source="dependency", endpoint=("POST", "/admin/leaves")), "needs a permission"),
    (dict(perm_source="role", endpoint=("POST", "/admin/leaves")), "admin roles"),
    (dict(verify=None, endpoint=("POST", "/admin/leaves")), "verify"),
    (dict(call=None, endpoint=("POST", "/admin/leaves")), "call"),
    (dict(endpoint=None), "endpoint"),
    (dict(risk="R3", endpoint=("POST", "/admin/leaves"), max_items=5), "admin-only"),
    (dict(endpoint=("POST", "/platform/clinics/{clinic_id}/features")), "prohibited"),
    (dict(endpoint=("POST", "/admin/assistant/requests")), "prohibited"),
    (dict(endpoint=("POST", "/webhook")), "prohibited"),
    (dict(page="nowhere", endpoint=("POST", "/admin/leaves")), "page"),
    (dict(risk="H", call=None, verify=None), "handoff"),
    (dict(risk="H", handoff=Handoff(page="staff")), "never executable"),
    (dict(risk="H", call=None, verify=None, handoff=Handoff(page="nowhere")), "page"),
])
def test_validate_refuses_bad_specs(kw, needle):
    with pytest.raises(ValueError, match=needle):
        validate(_spec(**kw))


def test_r3_with_a_stated_permission_may_be_staff_runnable():
    validate(_spec(risk="R3", endpoint=("PUT", "/admin/lab-tests/{test_id}"), permission="LAB_TESTS_MANAGE",
                   perm_source="dependency", max_items=200))


def test_h_tier_is_valid_only_as_a_page_handoff():
    validate(_spec(risk="H", call=None, verify=None, handoff=Handoff(page="staff")))


def test_specs_are_immutable():
    with pytest.raises(Exception):
        REGISTRY["nav.open"].risk = "R3"


# ── help index (KK-FR-015) ────────────────────────────────────────────────────

@pytest.mark.parametrize("entry", HELP, ids=lambda e: e.page)
def test_help_points_at_a_real_button_on_its_page(entry):
    assert entry.page in PAGES
    start = HTML.index(f'id="pg-{entry.page}"')
    nxt = HTML.find('id="pg-', start + 1)
    section = HTML[start: nxt if nxt != -1 else len(HTML)]
    assert re.search(r">[^<]*" + re.escape(entry.button), section), f"{entry.button!r} not on {entry.page}"
    assert entry.keywords and entry.answer
