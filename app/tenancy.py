"""Tenancy primitives: which tables belong to a clinic, and what counts as a
valid clinic scope.

This module is deliberately tiny and imports nothing but typing. It is the
single source of truth for both facts, and it lives outside app/database.py on
purpose:

  * Three divergent copies of the tenant-table list existed (app/database.py
    had 26 entries, app/services/tenant_scoped_client.py had 15, and
    tests/test_lint_unscoped_queries.py had 17). The linter — the only
    automated tenant-isolation guard — held the shortest list, so queries
    against admin_notifications, broadcasts, outbound_message_ledger and five
    other tenant tables were never checked by anything.

  * app/database.py is heavy (it builds the Supabase client at import) and
    several tests install a fake `app.database` into sys.modules to avoid
    that. Anything importing the table list *from there* silently receives a
    MagicMock under those tests, and `"appointments" in MagicMock()` is False
    — a guard that quietly stops guarding. A module with no dependencies is
    one nobody needs to fake.

app/database.py re-exports both names, so existing
`from app.database import TENANT_OWNED_TABLES, is_valid_clinic_scope` imports
keep working.
"""

from typing import Optional

#: Tables whose rows belong to exactly one clinic. Querying one of these
#: without a clinic_id predicate is always a bug: the application connects to
#: Supabase as `service_role`, which holds BYPASSRLS, so the database will
#: happily return every tenant's rows. Migration 049 defines RLS policies, but
#: nothing ever connects as a role they apply to — application code is the only
#: enforcement boundary there is.
#:
#: Every entry here has been verified to actually have a clinic_id column.
#: doctor_branches is deliberately ABSENT: it is a pure junction table
#: (id, doctor_id, branch_id, session) with no clinic_id — see
#: migrations/010_branches.sql. Listing it made scoped_query() emit a predicate
#: on a column that does not exist, i.e. a guaranteed 500 for the first caller
#: to reach for the safe helper. Its isolation is transitive: doctor_id and
#: branch_id are both resolved under clinic-scoped queries.
TENANT_OWNED_TABLES = frozenset({
    "appointments", "patients", "lab_reports", "lab_tests", "doctors",
    "branches", "doctor_leaves", "hospital_holidays",
    "clinic_admins", "integration_connectors", "connector_failed_reports",
    "conversations", "inbound_messages", "processed_messages",
    "family_members", "payment_events", "failed_messages",
    "prescriptions", "prescription_reminder_sends", "broadcasts",
    "admin_notifications", "outbound_message_ledger", "connector_audit_log",
    "integration_processed_reports", "analytics_events",
})

#: Values that are NOT a clinic. "default" is the historical sentinel meaning
#: "no clinic specified"; it is never a real clinics.id. Treating it as one
#: caused the 2026-09-01 cross-tenant incident, where `if clinic_id !=
#: "default"` skipped the tenant predicate entirely and a super_admin read —
#: and deleted — every tenant's doctors from one clinic's admin panel.
_NON_SCOPES = ("default", "none", "null", "")


def is_valid_clinic_scope(clinic_id: Optional[str]) -> bool:
    """True only if clinic_id names one specific tenant.

    Use this before putting a value into `.eq("clinic_id", ...)`. If it returns
    False, refuse the operation — do not fall back to an unfiltered query.
    """
    return bool(clinic_id and str(clinic_id).strip().lower() not in _NON_SCOPES)
