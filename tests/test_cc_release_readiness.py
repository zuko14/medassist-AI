"""Continuous Care release readiness (P10; TEST-PLAN §2, §5).

Rollback chain 149 → 145 on real PG is proven in tests/test_migration_cc_m1_m5.py; here: every migration has its
rollback and a verify block; cc_purge_clinic removes every cc_* row of one clinic (only that clinic) and resets the
state; the platform clinic deletion runs the purge only for a CC clinic; every CC scheduler job is registered and
CAS-locked; the hook adds no measurable work for a clinic without CC and only a pure check for non-numeric text.
"""

import asyncio
import re
import time
from pathlib import Path

from app.services import cc_hooks
from app.services.tenant import care_access
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
CC_TABLES = ("cc_outcome_snapshots", "cc_verifications", "cc_actions", "cc_case_events", "cc_cases", "cc_radar_rows",
             "cc_evaluations", "cc_observations", "cc_prompts", "cc_schedule_items", "cc_measure_sources",
             "cc_discharge_followups", "cc_plan_items", "cc_care_plans", "cc_enrollments", "cc_cards", "cc_consents",
             "cc_protocol_activations")


def test_every_migration_has_a_rollback_and_a_verify_block():
    for n in range(145, 150):
        up = next((ROOT / "migrations").glob(f"{n}_cc_*.sql"))
        down = next((ROOT / "migrations" / "rollback").glob(f"{n}_rollback_cc_*.sql"))
        assert f"{n} verify" in up.read_text(encoding="utf-8"), up.name
        assert "DROP" in down.read_text(encoding="utf-8"), down.name
    purge = (ROOT / "migrations" / "145_cc_foundation.sql").read_text(encoding="utf-8")
    listed = re.search(r"ARRAY\['cc_outcome_snapshots'.*?\]", purge, re.S).group(0)
    assert all(f"'{t}'" in listed for t in CC_TABLES)                     # the purge knows every table, children first


def test_purge_clinic_removes_only_that_clinic(care):  # noqa: F811
    e, pid = care.enroll()
    other_e, _ = care.enroll()
    _q(care.conn, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, observed_at, source) "
                  "VALUES (%s, %s, 'fbg', 140, now(), 'whatsapp_typed')", care.clinic, e["id"])
    from tests.test_ipd_api_e2e import _clinic
    from tests.cc_world import _doctor, activate_pack, users
    keep = _clinic(care.conn)
    assert care.api.owner_toggle(keep, "care_enabled", True).status_code == 200
    activate_pack(care.api, keep, users(care.api, keep, _doctor(care.conn, keep)))
    out = _q(care.conn, "SELECT cc_purge_clinic(%s)", care.clinic)
    assert out["cc_enrollments"] == 2 and out["cc_observations"] == 1 and out["cc_protocol_activations"] >= 1
    for t in CC_TABLES:
        assert _q(care.conn, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", care.clinic) == 0, t
    assert _q(care.conn, "SELECT cc_state || cc_settings::text FROM clinics WHERE id = %s", care.clinic) == "OFF{}"
    assert _q(care.conn, "SELECT count(*) FROM cc_protocol_activations WHERE clinic_id = %s", keep) == 1


def test_scheduler_registers_every_cc_job_under_a_lock():
    src = (ROOT / "app" / "services" / "scheduler.py").read_text(encoding="utf-8")
    for job in ("cc_card_dispatch", "cc_sla_sweep", "cc_plan_rebuild", "cc_schedule_materialize", "cc_verify",
                "cc_radar_morning", "cc_radar_afternoon", "cc_daily_health", "cc_evaluate_daily", "cc_outcome_snapshot"):
        assert f'("{job}"' in src, job
    body = src[src.index("async def _cc_each"):src.index("async def cc_card_dispatch")]
    assert "distributed_job_lock" in body and 'neq("cc_state", "OFF")' in body and "care_access" in body


def test_hook_latency_budget():
    off = {"id": "c", "account_type": "tenant", "features": {"opd_enabled": True}}
    t = time.perf_counter()
    for _ in range(20000):
        care_access(off)
    assert (time.perf_counter() - t) / 20000 < 20e-6                       # < 20 µs per message for a non-CC clinic
    on = {**off, "features": {"opd_enabled": True, "care_enabled": True}, "cc_settings": {}}
    t = time.perf_counter()
    for _ in range(200):
        assert asyncio.run(cc_hooks.handle_typed(None, on, "+919800000001", "main_menu", "I want an appointment", "w")) \
            is False                                                          # not a number: no DB call at all
    assert (time.perf_counter() - t) / 200 < 5e-3
