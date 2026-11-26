"""Owner console additions (OC-02…OC-09; docs/specialty-os/OWNER-CONSOLE.md §2, §4) on real Postgres.

Every plan sits in exactly one segment (guards the registry like the OC-01 drift test); the plan catalogue lists every
plan with its segment and labels; the modules API explains eligibility (reason + dependencies) for every module incl.
Continuous Care and the Specialty OS packs; the segment KPI strip counts clinics and pharmacy KPIs; the value view and
the go-live checklist answer; the console markup has the segment tabs, the modules card and the typed confirmation.
"""

import uuid
from pathlib import Path

from app.services.tenant import PLAN_FEATURES, SEGMENT_BY_PLAN, SEGMENT_LABELS
from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def test_every_plan_has_one_segment():
    assert set(SEGMENT_BY_PLAN) == set(PLAN_FEATURES)
    assert set(SEGMENT_BY_PLAN.values()) == set(SEGMENT_LABELS)
    assert SEGMENT_BY_PLAN["pharmacy"] == "pharmacies" and SEGMENT_BY_PLAN["eye"] == "specialty"


def test_plan_catalogue_and_segments_summary(api):  # noqa: F811
    cat = api.client.get("/platform/plan-catalogue").json()
    assert {p["slug"] for p in cat["plans"]} == set(PLAN_FEATURES)
    ent = next(p for p in cat["plans"] if p["slug"] == "enterprise")
    assert ent["features"] == ["All features"] and ent["segment_label"] == "Hospitals & clinics"
    w = sp_world(api, plan="dental", packs=("dental",))
    summary = {s["segment"]: s for s in api.client.get("/platform/segments/summary").json()["segments"]}
    assert set(summary) == set(SEGMENT_LABELS)
    assert summary["specialty"]["clinics"] >= 1 and summary["specialty"]["modules"].get("specialty_os", 0) >= 1
    assert _q(w.conn, "SELECT plan FROM clinics WHERE id = %s", w.clinic) == "dental"


def test_modules_explain_eligibility(api):  # noqa: F811
    w = sp_world(api, plan="eye", packs=("eye",))
    mods = {m["key"]: m for m in api.client.get(f"/platform/clinics/{w.clinic}/modules").json()["modules"]}
    assert mods["specialty_os"]["on"] and mods["optical_enabled"]["on"] and mods["optical_enabled"]["eligible"]
    assert mods["ivf_lab_enabled"]["eligible"] is False and "Available on plans" in mods["ivf_lab_enabled"]["reason"]
    assert mods["care_enabled"]["eligible"] is True and mods["care_enabled"]["depends"] == ["opd_enabled"]
    plain = str(_q(w.conn, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES ('D', %s, "
                           "'diagstream', true, '{}') RETURNING id", "+9192" + str(uuid.uuid4().int)[:8]))
    mods = {m["key"]: m for m in api.client.get(f"/platform/clinics/{plain}/modules").json()["modules"]}
    assert mods["specialty_os"]["eligible"] is False and mods["care_enabled"]["reason"] == "Enable OPD OS first."
    assert api.client.get(f"/platform/clinics/{uuid.uuid4()}/modules").status_code == 404


def test_value_and_onboarding(api):  # noqa: F811
    w = sp_world(api, plan="derma", packs=("derma",))
    v = api.client.get(f"/platform/clinics/{w.clinic}/value").json()
    assert v["ri"] is None and v["cc"] is None and v["messages_sent"] == 0 and len(v["month"]) == 7
    assert api.client.get(f"/platform/clinics/{w.clinic}/value", params={"month": "2026-13"}).status_code == 422
    ob = api.client.get(f"/platform/clinics/{w.clinic}/onboarding").json()
    keys = [s["key"] for s in ob["steps"]]
    assert keys[0] == "whatsapp" and "opd" in keys and "specialty" in keys and ob["done"] <= ob["total"]


def test_console_markup():
    html = (ROOT / "admin" / "platform.html").read_text(encoding="utf-8")
    seg = html[html.index("<!-- OC:segments:START"):html.index("<!-- OC:segments:END -->")]
    assert [s for s in ("ALL", "hospitals", "specialty", "diagnostics", "pharmacies") if f'data-seg="{s}"' in seg] == \
        ["ALL", "hospitals", "specialty", "diagnostics", "pharmacies"]
    assert 'class="filter-select oc-seg"' in seg and 'role="tablist"' in seg
    assert "<!-- OC:modules:START" in html and "OC_TYPED = ['care_enabled', 'ai_receptionist']" in html
    css = html[html.index("/* OC:START — owner console segments"):html.index("/* OC:END */")]
    assert "#" not in css.replace("#ocSegKpis", "") and "rgb" not in css
