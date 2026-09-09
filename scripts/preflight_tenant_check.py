"""Read-only preflight for the tenant-isolation hardening (migrations 074-075).

Run this against PRODUCTION *before* deploying. It writes nothing. It answers
the only question that matters for a live system: would any of the fail-closed
changes stop something that works today?

    python scripts/preflight_tenant_check.py

It reuses the app's own Supabase client, so it needs no extra configuration
beyond the SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY this deployment already
sets. Point it at another environment by exporting those two variables.

Exit code 0 means the deploy is clear. Exit code 1 means at least one live
clinic needs attention first; every finding names exactly what to fix.

Unlike scripts/whatsapp_doctor.py, which probes Meta's live API for one clinic,
this makes no outbound calls beyond reading the database.
"""

import os
import sys

# Running this as `python scripts/preflight_tenant_check.py` puts scripts/ on
# sys.path, not the repo root, so `import app` would fail. Add the root so both
# that form and `python -m scripts.preflight_tenant_check` work.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tables migration 075 makes mandatory. Keep in sync with that file's targets.
CORE_TENANT_TABLES = [
    "appointments",
    "patients",
    "conversations",
    "doctors",
    "doctor_leaves",
    "hospital_holidays",
    "lab_reports",
    "prescriptions",
    "analytics_events",
    "prescription_reminder_sends",
]


def fetch_active_clinics(supabase) -> list[dict]:
    """Every clinic the bot currently serves."""
    res = (
        supabase.table("clinics")
        .select("id, name, config, phone_number_id, is_active, status")
        .eq("is_active", True)
        .execute()
    )
    return [
        c
        for c in (res.data or [])
        if isinstance(c, dict) and (c.get("status") or "") != "DELETED"
    ]


def check_whatsapp_credentials(clinics):
    """Clinics that could not send once the global fallback is gone.

    Mirrors WhatsAppService._get_credentials() exactly. Only ever checks
    whether a value is present; never prints or returns a credential.
    """
    broken = []
    for clinic in clinics:
        config = clinic.get("config") or {}
        token = config.get("meta_access_token")
        phone_id = (
            config.get("meta_phone_number_id")
            or config.get("phone_number_id")
            or clinic.get("phone_number_id")
        )
        missing = [
            label
            for label, value in (("meta_access_token", token), ("phone_number_id", phone_id))
            if not value
        ]
        if missing:
            broken.append((clinic.get("id"), clinic.get("name"), ", ".join(missing)))
    return broken


def check_send_identity(clinics):
    """Verify the one field that actually separates tenants at send time.

    The Meta access token is an APP-level credential and is reused across
    clinics, so it is not a tenant boundary: a token that covers several
    numbers will happily send as any of them, and Meta returns no error for
    the wrong one. phone_number_id is the only discriminator there is.

    Two ways it can go wrong, neither caught by the database:

      * Duplicate. The unique index from migration 043 covers the top-level
        clinics.phone_number_id column, but _get_credentials() reads
        config.meta_phone_number_id FIRST, and nothing constrains the JSON.
        Two clinics carrying the same value there would both send as one
        number, and their patients' replies would land in one inbox.

      * Drift. Inbound routing (resolve_tenant) matches on the column, while
        outbound sending reads the config value. If a manual edit moves one
        and not the other, a clinic sends from one number and is recognised by
        another, so its patients' replies stop resolving. The create and
        update endpoints keep both in step, so only hand-editing causes this.

    Returns (duplicates, drifted).
    """
    by_pid, drifted = {}, []
    for clinic in clinics:
        config = clinic.get("config") or {}
        cfg_pid = config.get("meta_phone_number_id") or config.get("phone_number_id")
        col_pid = clinic.get("phone_number_id")
        effective = cfg_pid or col_pid
        name, cid = clinic.get("name"), clinic.get("id")

        if effective:
            by_pid.setdefault(str(effective), []).append(name)
        if cfg_pid and col_pid and str(cfg_pid) != str(col_pid):
            drifted.append((cid, name, str(cfg_pid), str(col_pid)))

    duplicates = {pid: names for pid, names in by_pid.items() if len(names) > 1}
    return duplicates, drifted


def check_null_tenant_rows(supabase):
    """Rows belonging to no clinic. They keep working, but stay unowned."""
    found, skipped = [], []
    for table in CORE_TENANT_TABLES:
        try:
            res = (
                supabase.table(table)
                # Count on clinic_id, not id: hospital_holidays has no id column.
                .select("clinic_id", count="exact")
                .is_("clinic_id", "null")
                .limit(1)
                .execute()
            )
        except Exception as exc:  # table absent in this deployment
            skipped.append((table, str(exc).split("\n")[0][:80]))
            continue
        count = res.count or 0
        if count:
            found.append((table, count))
    return found, skipped


def check_unpinned_integrations(clinics):
    """Clinics still writable with the shared INTEGRATION_SECRET."""
    return [
        (c.get("id"), c.get("name"))
        for c in clinics
        if not (c.get("config") or {}).get("integration_secret")
    ]


def main():
    try:
        from app.database import supabase
    except Exception as exc:
        print(f"ERROR: could not build the Supabase client: {exc}")
        print("Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
        return 2

    try:
        clinics = fetch_active_clinics(supabase)
    except Exception as exc:
        print(f"ERROR: could not read the clinics table: {exc}")
        return 2

    blocking = 0

    print(f"Active clinics: {len(clinics)}")
    for clinic in clinics:
        print(f"  - {clinic.get('name')} ({clinic.get('id')})")
    if len(clinics) <= 1:
        print("  Single tenant: the refuse-to-guess changes cannot fire here.")
    print()

    print("[1/4] WhatsApp credentials  (BLOCKING - these clinics would stop sending)")
    broken = check_whatsapp_credentials(clinics)
    if not broken:
        print("  OK: every active clinic has its own token and phone number id.")
    else:
        blocking += len(broken)
        for cid, name, missing in broken:
            print(f"  MISSING  {name} ({cid}) -> {missing}")
        print("  Fix: set these in the clinic's config before deploying.")
    print()

    print("[2/4] Send identity - phone_number_id  (BLOCKING)")
    duplicates, drifted = check_send_identity(clinics)
    if not duplicates and not drifted:
        print("  OK: every clinic sends as its own distinct number, and inbound")
        print("      routing agrees with outbound sending.")
    else:
        for pid, names in duplicates.items():
            blocking += 1
            print(f"  DUPLICATE  phone_number_id {pid} is used by: {', '.join(names)}")
            print("             These clinics would send as the same number.")
        for cid, name, cfg_pid, col_pid in drifted:
            blocking += 1
            print(f"  DRIFT      {name} ({cid})")
            print(f"             sends as {cfg_pid} but is recognised as {col_pid}")
            print("             Set both to the same value.")
    print()

    print("[3/4] Rows with no owning clinic  (non-blocking)")
    nulls, skipped = check_null_tenant_rows(supabase)
    for table, reason in skipped:
        print(f"  skipped {table}: {reason}")
    if not nulls:
        print("  OK: no NULL clinic_id rows. Migration 075 validates cleanly.")
    else:
        total = sum(count for _, count in nulls)
        for table, count in nulls:
            print(f"  {table}: {count} row(s) with NULL clinic_id")
        print(
            f"  {total} unowned row(s). Nothing breaks: new writes are blocked\n"
            "  either way, and the constraint simply stays unvalidated until\n"
            "  these rows are assigned an owner."
        )
    print()

    print("[4/4] Clinics writable with the shared integration secret  (non-blocking)")
    unpinned = check_unpinned_integrations(clinics)
    if not unpinned:
        print("  OK: every active clinic is pinned to its own integration secret.")
    else:
        for cid, name in unpinned:
            print(f"  UNPINNED  {name} ({cid})")
        print(
            "  Any holder of INTEGRATION_SECRET can post lab reports into these.\n"
            "  Set config.integration_secret per clinic, then make the check\n"
            "  mandatory in app/routers/integrations.py."
        )
    print()

    if blocking:
        print(f"RESULT: {blocking} blocking issue(s). Fix these before deploying.")
        return 1
    print("RESULT: clear to deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
