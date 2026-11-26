"""Plan registry drift guard (OC-01; docs/specialty-os/OWNER-CONSOLE.md §2 OC-1).

A plan slug lives in many places; a new plan missed in one of them means 400s on create / update or silent KPI gaps.
Every place below must name exactly the plans in tenant.PLAN_FEATURES (test-only; no runtime change).
"""

import re
from pathlib import Path

from app.services.tenant import FEATURE_LABELS, PLAN_FEATURES

ROOT = Path(__file__).resolve().parents[1]
PLANS = set(PLAN_FEATURES)


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _quoted(block):
    return set(re.findall(r'"([a-z]+)"', block))


def test_clinics_router_create_and_update_lists():
    src = _read("app/routers/clinics.py")
    create = src[src.index("class CreateClinicRequest"):src.index("class CreateClinicRequest") + 600]
    update = src[src.index("class UpdateClinicRequest"):src.index("class UpdateClinicRequest") + 600]
    for block in (create, update):
        lit = re.search(r"Literal\[(.*?)\]", block, re.S).group(1)
        assert _quoted(lit) == PLANS


def test_platform_router_plan_lists():
    src = _read("app/routers/platform.py")
    by_plan = re.search(r"clinics_by_plan = \{(.*?)\}", src, re.S).group(1)
    assert _quoted(by_plan) == PLANS
    valid = re.search(r"valid_plans = \{(.*?)\}", src, re.S).group(1)
    assert _quoted(valid) == PLANS


def test_message_accounting_fallback():
    src = _read("app/services/message_accounting.py")
    block = src[src.index("fallback = {"):src.index("_plan_tiers_cache = fallback")]
    assert set(re.findall(r'^\s+"([a-z]+)": \{"included_messages_month"', block, re.M)) == PLANS


def test_owner_console_selects_and_badges():
    html = _read("admin/platform.html")
    for sel in ("planFilter", "ccPlan"):
        block = re.search(rf'<select[^>]*id="{sel}"[^>]*>(.*?)</select>', html, re.S).group(1)
        assert set(re.findall(r'<option value="([a-z]+)"', block)) == PLANS, sel
    assert set(re.findall(r"\.badge-([a-z]+) \{", html)) >= PLANS


def test_database_checks(real_pg_conn):
    cur = real_pg_conn.cursor()
    for con in ("clinics_plan_check", "plan_tiers_plan_name_check"):
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (con,))
        assert set(re.findall(r"'([a-z]+)'", cur.fetchone()[0])) == PLANS, con
    cur.execute("SELECT plan_name FROM plan_tiers")
    assert {r[0] for r in cur.fetchall()} >= PLANS


def test_every_plan_feature_has_a_label():
    assert {f for feats in PLAN_FEATURES.values() for f in feats if f != "*"} <= set(FEATURE_LABELS)
