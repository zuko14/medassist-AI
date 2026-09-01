"""Tenant-Scoped Client & Database Isolation Backstop (W2).

Provides an application-level isolation backstop wrapping the PostgREST / Supabase client.
Auto-injects `.eq('clinic_id', tenant_id)` on all operations over tenant-owned tables
and strictly prevents cross-tenant data leakage even if a route-level check were omitted.
"""

import logging
from typing import Any, Dict, List, Optional, Union
from app.database import supabase
from app.tenancy import TENANT_OWNED_TABLES

logger = logging.getLogger(__name__)

# TENANT_OWNED_TABLES comes from app.tenancy: the single source of truth. The
# private copy that used to live here had drifted to 15 of the 25 tables, and
# importing it from app.database instead made this guard silently empty under
# the tests that fake that module in sys.modules.



class TenantIsolationError(RuntimeError):
    """Raised when an operation violates tenant isolation invariants."""
    pass


class TenantScopedQueryBuilder:
    """Wraps PostgREST query builder to enforce tenant boundaries."""

    def __init__(self, raw_builder: Any, table_name: str, clinic_id: str):
        self._builder = raw_builder
        self.table_name = table_name
        self.clinic_id = clinic_id
        self._scoped = False

    def select(self, *args, **kwargs):
        res = self._builder.select(*args, **kwargs)
        if self.table_name in TENANT_OWNED_TABLES:
            res = res.eq("clinic_id", self.clinic_id)
        return TenantScopedQueryBuilder(res, self.table_name, self.clinic_id)

    def insert(self, values: Union[Dict[str, Any], List[Dict[str, Any]]], *args, **kwargs):
        if self.table_name in TENANT_OWNED_TABLES:
            if isinstance(values, dict):
                val_clinic = values.get("clinic_id")
                if val_clinic and str(val_clinic) != str(self.clinic_id):
                    raise TenantIsolationError(
                        f"Cross-tenant INSERT attempted: target clinic {val_clinic} != client clinic {self.clinic_id}"
                    )
                values["clinic_id"] = self.clinic_id
            elif isinstance(values, list):
                for row in values:
                    val_clinic = row.get("clinic_id")
                    if val_clinic and str(val_clinic) != str(self.clinic_id):
                        raise TenantIsolationError(
                            f"Cross-tenant batch INSERT attempted: {val_clinic} != {self.clinic_id}"
                        )
                    row["clinic_id"] = self.clinic_id

        res = self._builder.insert(values, *args, **kwargs)
        return TenantScopedQueryBuilder(res, self.table_name, self.clinic_id)

    def update(self, values: Dict[str, Any], *args, **kwargs):
        if self.table_name in TENANT_OWNED_TABLES:
            val_clinic = values.get("clinic_id")
            if val_clinic and str(val_clinic) != str(self.clinic_id):
                raise TenantIsolationError(
                    f"Cross-tenant UPDATE attempted to change clinic_id: {val_clinic} != {self.clinic_id}"
                )
        res = self._builder.update(values, *args, **kwargs)
        if self.table_name in TENANT_OWNED_TABLES:
            res = res.eq("clinic_id", self.clinic_id)
        return TenantScopedQueryBuilder(res, self.table_name, self.clinic_id)

    def delete(self, *args, **kwargs):
        res = self._builder.delete(*args, **kwargs)
        if self.table_name in TENANT_OWNED_TABLES:
            res = res.eq("clinic_id", self.clinic_id)
        return TenantScopedQueryBuilder(res, self.table_name, self.clinic_id)

    def __getattr__(self, name: str):
        attr = getattr(self._builder, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                res = attr(*args, **kwargs)
                if hasattr(res, "execute") or hasattr(res, "select"):
                    return TenantScopedQueryBuilder(res, self.table_name, self.clinic_id)
                return res
            return wrapper
        return attr

    def execute(self):
        return self._builder.execute()


class TenantScopedClient:
    """PostgREST / Supabase wrapper scoped to a single clinic_id."""

    def __init__(self, clinic_id: str, raw_client: Any = None):
        if not clinic_id or not clinic_id.strip():
            raise TenantIsolationError("TenantScopedClient must be initialized with a non-empty clinic_id")
        self.clinic_id = str(clinic_id).strip()
        self._raw_client = raw_client or supabase

    def table(self, table_name: str) -> TenantScopedQueryBuilder:
        raw_builder = self._raw_client.table(table_name)
        return TenantScopedQueryBuilder(raw_builder, table_name, self.clinic_id)


def get_tenant_scoped_client(clinic_id: str) -> TenantScopedClient:
    """Factory helper to obtain a TenantScopedClient for a clinic."""
    return TenantScopedClient(clinic_id)
