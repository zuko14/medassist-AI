"""Test helper for D-13: settings saves go through app.services.config_store.save_config.

`captured_config_writes()` records each save instead of touching a database. A record holds the table, the
row id, the session clinic, what the route read (`before`) and what it wants stored (`config`), plus the
keys that actually change (`changed`, from config_diff), so a test can check both the stored value and
that nothing else is written.
"""

from contextlib import contextmanager
from unittest.mock import patch

from app.services.config_store import config_diff


@contextmanager
def captured_config_writes(result=None):
    writes: list = []

    async def fake(table, row_id, clinic_id, before, after):
        sets, removes = config_diff(before or {}, after or {})
        writes.append({"table": table, "id": row_id, "clinic_id": clinic_id, "before": before, "config": after,
                       "changed": [s["path"] for s in sets] + removes})
        return after if result is None else result

    with patch("app.services.config_store.save_config", side_effect=fake):
        yield writes
