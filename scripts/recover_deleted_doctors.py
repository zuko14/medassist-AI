"""Rebuild a clinic's doctor roster after an accidental delete.

Why this exists
---------------
DELETE /admin/doctors/{id} is a hard DELETE and doctor_branches cascades off
it, so on 2026-09-01 a live clinic's roster was destroyed with nothing to
restore from: the audit trail recorded only the doctor's UUID. There are two
recovery sources, and this script uses whichever is available:

  1. admin_audit_logs.details.deleted_row - a full snapshot of the row. The
     delete handler now writes this, so any delete from today forward is a
     complete, faithful restore.

  2. appointments.doctor_name - for deletes that predate the snapshot, the
     denormalised doctor name on past appointments is the only surviving
     record. That reconstructs the NAME and DEPARTMENT only; slot timings,
     consultation fee and branch assignments are gone and fall back to schema
     defaults, which the clinic must then correct in the panel.

Source 1 is exact. Source 2 is a starting point, not a restore - say so to the
clinic rather than letting them believe the roster is back as it was.

Prefer a Supabase point-in-time restore over this script if the deletion is
recent enough and the plan supports it. That returns the real rows.

Usage
-----
    python scripts/recover_deleted_doctors.py --clinic-id <uuid>            # dry run
    python scripts/recover_deleted_doctors.py --clinic-id <uuid> --apply    # write

Dry run is the default and prints exactly what would be inserted. Nothing is
written without --apply.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import supabase  # noqa: E402
from app.tenancy import is_valid_clinic_scope  # noqa: E402


def _existing_doctor_names(clinic_id: str) -> set:
    res = (
        supabase.table("doctors")
        .select("name")
        .eq("clinic_id", clinic_id)
        .limit(5000)
        .execute()
    )
    return {(r.get("name") or "").strip() for r in (res.data or [])}


def _from_audit_log(clinic_id: str) -> dict:
    """Full row snapshots written by delete_doctor, newest wins."""
    res = (
        supabase.table("admin_audit_logs")
        .select("created_at, details")
        .eq("clinic_id", clinic_id)
        .eq("action", "delete_doctor")
        .order("created_at")
        .limit(2000)
        .execute()
    )
    found = {}
    for row in res.data or []:
        details = row.get("details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                continue
        snapshot = details.get("deleted_row")
        if isinstance(snapshot, dict) and snapshot.get("name"):
            found[snapshot["name"].strip()] = snapshot
    return found


def _from_appointments(clinic_id: str) -> dict:
    """{doctor_name: department} seen on this clinic's past appointments."""
    res = (
        supabase.table("appointments")
        .select("doctor_name, department")
        .eq("clinic_id", clinic_id)
        .limit(5000)
        .execute()
    )
    seen = {}
    for row in res.data or []:
        name = (row.get("doctor_name") or "").strip()
        if name:
            seen.setdefault(name, (row.get("department") or "General").strip())
    return seen


def _restore_payload(clinic_id: str, snapshot: dict) -> dict:
    """Strip identity/immutable columns so the row is re-insertable."""
    payload = {
        k: v
        for k, v in snapshot.items()
        if k not in ("id", "created_at", "updated_at") and v is not None
    }
    payload["clinic_id"] = clinic_id
    payload.setdefault("is_active", True)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clinic-id", required=True, help="Target clinic UUID")
    ap.add_argument(
        "--apply", action="store_true", help="Actually insert (default: dry run)"
    )
    args = ap.parse_args()

    clinic_id = args.clinic_id.strip()
    if not is_valid_clinic_scope(clinic_id):
        # Same rule as the rest of the platform: an unscoped operation on a
        # tenant-owned table is refused, never widened.
        print("ERROR: --clinic-id must be a real clinic id.", file=sys.stderr)
        return 2

    existing = _existing_doctor_names(clinic_id)
    snapshots = _from_audit_log(clinic_id)
    from_appts = _from_appointments(clinic_id)

    exact, partial = [], []
    for name, snapshot in snapshots.items():
        if name not in existing:
            exact.append(_restore_payload(clinic_id, snapshot))
    exact_names = {p["name"] for p in exact}
    for name, department in sorted(from_appts.items()):
        if name in existing or name in exact_names:
            continue
        partial.append(
            {
                "clinic_id": clinic_id,
                "name": name,
                "department": department,
                "specialization": department,
                "is_active": True,
            }
        )

    print(f"clinic_id            : {clinic_id}")
    print(f"doctors currently    : {len(existing)}")
    print(f"exact restores       : {len(exact)}  (full row from audit snapshot)")
    print(f"partial rebuilds     : {len(partial)}  (name + department only)")
    print()
    for p in exact:
        print(f"  [exact]   {p['name']}  ({p.get('department', '?')})")
    for p in partial:
        print(f"  [partial] {p['name']}  ({p['department']})")

    if not exact and not partial:
        print("\nNothing to recover.")
        return 0

    if not args.apply:
        print("\nDry run. Re-run with --apply to insert these rows.")
        return 0

    inserted = 0
    for payload in exact + partial:
        try:
            supabase.table("doctors").insert(payload).execute()
            inserted += 1
        except Exception as e:  # keep going; one bad row must not stop recovery
            print(f"  FAILED {payload['name']}: {e}", file=sys.stderr)

    print(f"\nInserted {inserted} of {len(exact) + len(partial)}.")
    if partial:
        print(
            "\nPartial rebuilds carry schema-default slot timings, fees and no "
            "branch assignments. Have the clinic correct those in the admin "
            "panel before telling them the roster is restored."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
