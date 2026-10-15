"""Atomic partial writes of clinics.config / branches.config (Kriya Ketu bug register D-13).

Settings saves used to read the whole config JSON, change a few keys and write the whole object back, so
two saves close together, or a save working from the 30-second tenant cache, undid each other's changes.

save_config() keeps each caller's own logic: it is given what the caller read (`before`) and what the caller
wants stored (`after`), works out only the keys that changed, and has the database apply exactly those
under the row lock (kriya_config_apply, migration 110). Two saves that change different keys both survive;
two saves of the same key: the later one wins, as before.

Until migration 110 is applied on a database, the function is missing and save_config falls back to the
old whole-object write, so deploying this code first changes nothing for staff.
"""

import logging
from typing import Optional

from app.database import sb, supabase

logger = logging.getLogger(__name__)

TABLES = ("clinics", "branches")
_MISSING = object()


def config_diff(before: dict, after: dict, path: tuple = ()) -> tuple[list, list]:
    """(sets, removes): [{"path": [...], "value": v}] for every changed leaf (nested objects are walked,
    lists are values), and [[...]] for every key `after` dropped."""
    sets, removes = [], []
    for key in sorted(set(before) - set(after), key=str):
        removes.append([*path, str(key)])
    for key, value in after.items():
        old = before.get(key, _MISSING)
        if isinstance(old, dict) and isinstance(value, dict):
            s, r = config_diff(old, value, (*path, str(key)))
            sets += s
            removes += r
        elif old is _MISSING or old != value or type(old) is not type(value):
            sets.append({"path": [*path, str(key)], "value": value})
    return sets, removes


def _function_missing(error: Exception) -> bool:
    text = str(error)
    return "PGRST202" in text or ("kriya_config_apply" in text and ("does not exist" in text or "Could not find" in text))


async def save_config(table: str, row_id: str, clinic_id: str, before: Optional[dict], after: dict) -> Optional[dict]:
    """Store `after` as the row's config, writing only what changed from `before`. Returns the stored
    config (which also holds anyone else's changes), or None when the row is not there for this clinic."""
    if table not in TABLES:
        raise ValueError(f"save_config: unsupported table {table!r}")
    sets, removes = config_diff(before or {}, after or {})
    if not sets and not removes:
        return after
    try:
        res = await sb(supabase.rpc("kriya_config_apply", {
            "p_table": table, "p_id": str(row_id), "p_clinic_id": str(clinic_id),
            "p_sets": sets, "p_removes": removes}))
    except Exception as e:
        if not _function_missing(e):
            raise
        logger.warning(f"CONFIG_APPLY_FALLBACK table={table}: migration 110 not applied; whole-object write")
        if table == "clinics":
            # unscoped: unique_row_key (clinics.id IS the tenant, resolved by the caller)
            query = supabase.table("clinics").update({"config": after}).eq("id", row_id)
        else:
            query = supabase.table("branches").update({"config": after}).eq("id", row_id).eq("clinic_id", clinic_id)
        res = await sb(query)
        return after if res.data else None
    if res.data is None:  # kriya_config_apply returns NULL when the row is not this clinic's
        return None
    return res.data if isinstance(res.data, dict) else after
