"""Migration 110 against a real PostgreSQL schema (Kriya Ketu bug register D-13).

kriya_config_apply() writes only the keys a save changed, under the row lock, so two saves that change
different keys from the same starting config both survive (the whole-object write lost one of them).
Runs on the embedded server the real-Postgres fixture migrates to head.
"""

import json
from pathlib import Path

import psycopg2
import pytest

from app.services.config_store import config_diff
from tests.conftest_db import get_default_clinic_id
from tests.test_migration_107_doctor_leaves import _second_clinic

_MIG = Path(__file__).resolve().parent.parent / "migrations"
UP, DOWN = _MIG / "110_config_atomic_apply.sql", _MIG / "rollback" / "110_down.sql"


def _apply(cur, table, row_id, clinic_id, before, after):
    sets, removes = config_diff(before, after)
    cur.execute("SELECT kriya_config_apply(%s, %s, %s, %s::jsonb, %s::jsonb);",
                (table, row_id, clinic_id, json.dumps(sets), json.dumps(removes)))
    return cur.fetchone()[0]


def _set_config(cur, clinic_id, cfg):
    cur.execute("UPDATE clinics SET config = %s::jsonb WHERE id = %s;", (json.dumps(cfg), clinic_id))


def _config(cur, clinic_id):
    cur.execute("SELECT config FROM clinics WHERE id = %s;", (clinic_id,))
    return cur.fetchone()[0]


@pytest.fixture
def clinic(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = get_default_clinic_id(cur)
    saved = _config(cur, cid)
    yield cur, cid
    cur.execute("UPDATE clinics SET config = %s::jsonb WHERE id = %s;", (json.dumps(saved), cid))


BASE = {"address": "Old Road", "voice": {"assistant_name": "Kriya", "monthly_budget_paise": 100000,
                                         "outbound": {"auto_leads": False, "daily_cap": 30}},
        "lab_collection": {"start": "07:00", "end": "11:00"}}


def _copy(cfg):
    return json.loads(json.dumps(cfg))


def test_two_saves_from_the_same_config_both_survive(clinic):
    cur, cid = clinic
    _set_config(cur, cid, BASE)
    profile = _copy(BASE); profile["address"] = "New Road"                       # admin A
    voice = _copy(BASE); voice["voice"]["outbound"]["daily_cap"] = 50            # admin B, same starting copy
    _apply(cur, "clinics", cid, cid, BASE, profile)
    stored = _apply(cur, "clinics", cid, cid, BASE, voice)
    assert stored["address"] == "New Road" and stored["voice"]["outbound"]["daily_cap"] == 50
    assert _config(cur, cid) == stored


def test_a_nested_change_keeps_its_siblings(clinic):
    # The voice budget is set by the platform; a voice-settings save must not reset it.
    cur, cid = clinic
    _set_config(cur, cid, BASE)
    budget = _copy(BASE); budget["voice"]["monthly_budget_paise"] = 250000
    _apply(cur, "clinics", cid, cid, BASE, budget)
    settings = _copy(BASE); settings["voice"]["assistant_name"] = "Asha"
    stored = _apply(cur, "clinics", cid, cid, BASE, settings)
    assert stored["voice"]["monthly_budget_paise"] == 250000 and stored["voice"]["assistant_name"] == "Asha"


def test_removing_a_key_and_creating_a_deep_one(clinic):
    cur, cid = clinic
    _set_config(cur, cid, BASE)
    after = _copy(BASE)
    after.pop("lab_collection")
    after["home_collection"] = {"enabled": True, "windows": [{"start": "07:00", "end": "09:00"}]}
    stored = _apply(cur, "clinics", cid, cid, BASE, after)
    assert "lab_collection" not in stored and stored["home_collection"]["windows"][0]["start"] == "07:00"
    assert stored["address"] == "Old Road"


def test_a_scalar_parent_is_replaced_by_an_object(clinic):
    cur, cid = clinic
    _set_config(cur, cid, {"voice": "legacy"})
    assert _apply(cur, "clinics", cid, cid, {}, {"voice": {"pace": 1.0}}) == {"voice": {"pace": 1.0}}


def test_another_clinics_row_is_never_touched(clinic):
    cur, cid = clinic
    other = _second_clinic(cur)
    before = _config(cur, other)
    assert _apply(cur, "clinics", other, cid, {}, {"hijack": True}) is None         # clinic ≠ session clinic
    cur.execute("INSERT INTO branches (clinic_id, name) VALUES (%s, 'Other branch') RETURNING id;", (other,))
    branch = cur.fetchone()[0]
    try:
        assert _apply(cur, "branches", branch, cid, {}, {"hijack": True}) is None    # another clinic's branch
        cur.execute("SELECT config FROM branches WHERE id = %s;", (branch,))
        assert cur.fetchone()[0] == {}
        assert _apply(cur, "branches", branch, other, {}, {"lab_collection": {"start": "08:00"}}) == \
            {"lab_collection": {"start": "08:00"}}
    finally:
        cur.execute("DELETE FROM branches WHERE id = %s;", (branch,))
    assert _config(cur, other) == before


def test_only_clinics_and_branches_are_accepted(clinic):
    cur, cid = clinic
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("SELECT kriya_config_apply('appointments', %s, %s, '[]', '[]');", (cid, cid))


def test_110_is_idempotent_and_its_rollback_drops_only_the_function(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN;")
    try:
        cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'kriya_config_apply';")
        assert cur.fetchone()[0] == 0
        cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'kriya_config_apply';")
        assert cur.fetchone()[0] == 1
    finally:
        cur.execute("ROLLBACK;")
