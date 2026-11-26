"""Continuous Care table registration (CG1): the cc_* names in TENANT_OWNED_TABLES are exactly the tables migrations
145–149 create, every one carries clinic_id and RLS, and the rollbacks drop each of them."""

import re
from pathlib import Path

from app.tenancy import TENANT_OWNED_TABLES

MIG = Path(__file__).resolve().parents[1] / "migrations"
UPS = sorted(MIG.glob("14[5-9]_cc_*.sql"))
DOWNS = sorted((MIG / "rollback").glob("14[5-9]_rollback_cc_*.sql"))


def _created(paths) -> set:
    return {t for p in paths for t in re.findall(r"CREATE TABLE IF NOT EXISTS\s+(cc_[a-z_]+)", p.read_text(encoding="utf-8"))}


def test_five_migrations_and_five_rollbacks():
    assert [p.name[:3] for p in UPS] == ["145", "146", "147", "148", "149"]
    assert [p.name[:3] for p in DOWNS] == ["145", "146", "147", "148", "149"]


def test_registry_equals_created_tables():
    assert {t for t in TENANT_OWNED_TABLES if t.startswith("cc_")} == _created(UPS)
    assert len(_created(UPS)) == 18


def test_every_cc_table_has_clinic_id_rls_and_is_dropped_on_rollback():
    up = "\n".join(p.read_text(encoding="utf-8") for p in UPS)
    down = "\n".join(p.read_text(encoding="utf-8") for p in DOWNS)
    for t in _created(UPS):
        body = re.search(rf"CREATE TABLE IF NOT EXISTS\s+{t}\s*\((.*?)\n\);", up, re.S)
        assert body and re.search(r"\bclinic_id\s+UUID NOT NULL REFERENCES clinics", body.group(1)), t
        assert re.search(rf"DROP TABLE IF EXISTS\s+{t}\b", down), t
    assert "ENABLE ROW LEVEL SECURITY" in up and "FORCE ROW LEVEL SECURITY" in up
