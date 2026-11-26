"""Revenue Guard zero-impact matrix (RI-08; docs/revenue-integrity/SPEC.md §8, VERIFICATION zero-impact rows).

Read-only over existing tables, no trigger, OFF by default: a clinic without the opt-in never sends an ri_* query,
every /admin/ri route refuses it before reading, the nightly sweep and digest skip it, and the existing OPD / IPD /
RX / DX / dashboard routes send no ri_* query even for a clinic that has Revenue Guard on.
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_ri_route_matrix import RI_ROUTES
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def _ri(paths):
    return [p for p in paths if p.rsplit("/", 1)[-1].startswith("ri_")]


def test_every_ri_route_refuses_a_clinic_without_the_opt_in_before_any_ri_read(api, spy):  # noqa: F811
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    for method, path in sorted(RI_ROUTES):
        before = len(spy)
        body = {"reason": "Testing refusal"} if path.endswith("dismiss") else {}
        r = api.call(method, path.replace("{finding_id}", str(uuid.uuid4())), clinic,
                     body if method in ("POST", "PATCH") else None)
        assert r.status_code == 403 and "not enabled" in r.text, (method, path, r.status_code, r.text)
        assert not _ri(spy[before:]), (method, path)


@pytest.mark.parametrize("ri_on", [False, True])
def test_existing_routes_send_no_ri_query(api, spy, ri_on):  # noqa: F811
    clinic = _clinic(api.conn)
    if ri_on:
        assert api.owner_toggle(clinic, "revenue_guard", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    del spy[:]
    for path in ("/admin/me", "/admin/opd/collections/summary", "/admin/opd/setup", "/admin/ipd/setup",
                 "/admin/billing/gstr1"):
        api.call("GET", path, clinic)
    assert not _ri(spy)


def test_sweep_and_digest_skip_clinics_without_the_opt_in(api, monkeypatch):  # noqa: F811
    from app.services import revenue_guard as ri
    plain = _clinic(api.conn)
    seen = []
    monkeypatch.setattr(ri, "scan_clinic", lambda c, day=None, now=None: seen.append(c["id"]) or asyncio.sleep(0))
    asyncio.run(ri.run_nightly())
    assert plain not in seen
    monkeypatch.setattr(ri, "digest_text", lambda cid, today=None: seen.append(("d", cid)) or asyncio.sleep(0))
    asyncio.run(ri.run_digest())
    assert ("d", plain) not in seen


def test_no_trigger_and_read_only_sql():
    sql = (ROOT / "migrations" / "143_revenue_guard.sql").read_text(encoding="utf-8")
    assert "CREATE TRIGGER" not in sql
    import re
    writes = set(re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", sql))
    assert writes <= {"ri_cand", "ri_findings", "ri_runs"}, writes              # never writes a billing table
    assert set(re.findall(r"ALTER TABLE\s+([a-z_]+)", sql)) == {"clinics"}       # + RLS on ri_* through format(%I)
    assert "ADD COLUMN IF NOT EXISTS ri_settings" in sql


def test_ui_edits_are_additive_and_gated():
    html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    css = html[html.index("/* RI:START"):html.index("/* RI:END */")]
    import re
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), "UI-G1: tokens only in the RI CSS block"
    assert 'data-page="riguard" data-ri="nav RI_VIEW" style="display:none"' in html         # UI-G11 hidden by default
    assert '<div class="stats" id="riDashTile" hidden>' in html
    theme = html[html.index("function toggleTheme()"):html.index("// ═══════ ICONS")]
    assert "ri." not in theme                                   # no chart on the RI page, so no repaint line
    assert html.count("/* RI:page:START */, riguard: () => ri.load() /* RI:page:END */") == 1
