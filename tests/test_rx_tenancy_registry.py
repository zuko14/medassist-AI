"""Every rx_* table created by a Pharmacy OS migration is tenant-owned (AGENTS.md §1.1),
and nothing is registered that no migration creates (docs/pharmacy-os/DATA-MODEL.md §12)."""

import pathlib
import re

from app.tenancy import TENANT_OWNED_TABLES

CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (rx_[a-z_]+)", re.I)


def _created() -> set[str]:
    out = set()
    for path in sorted(pathlib.Path("migrations").glob("1[3-9][0-9]_rx_*.sql")):
        out.update(CREATE.findall(path.read_text(encoding="utf-8")))
    return out


def test_rx_migrations_exist():
    assert pathlib.Path("migrations/134_rx_foundation.sql").exists()
    assert "rx_stores" in _created()


def test_registry_equals_created_tables():
    registered = {t for t in TENANT_OWNED_TABLES if t.startswith("rx_")}
    assert registered == _created(), {"unregistered": _created() - registered, "stale": registered - _created()}
