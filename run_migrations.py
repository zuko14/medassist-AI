"""Run SQL migrations against Supabase via the REST API."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import supabase


def run_migration(filepath: str):
    """Execute a SQL migration file via Supabase RPC."""
    print(f"\n{'='*60}")
    print(f"Running migration: {filepath}")
    print(f"{'='*60}")

    with open(filepath, "r", encoding="utf-8") as f:
        sql = f.read()

    # Split into individual statements (skip empty and comments-only)
    statements = []
    current = []
    for line in sql.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--") or stripped == "":
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []

    # Handle multi-line statements that don't end with ;
    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            statements.append(stmt)

    success = 0
    skipped = 0
    failed = 0

    for i, stmt in enumerate(statements, 1):
        # Skip comments-only statements
        if all(line.strip().startswith("--") or line.strip() == "" for line in stmt.split("\n")):
            continue

        try:
            result = supabase.rpc("exec_sql", {"query": stmt}).execute()
            print(f"  [OK] Statement {i}/{len(statements)} executed")
            success += 1
        except Exception as e:
            err_str = str(e)
            if "already exists" in err_str.lower() or "duplicate" in err_str.lower():
                print(f"  [SKIP]  Statement {i}/{len(statements)} skipped (already exists)")
                skipped += 1
            elif "does not exist" in err_str.lower() and "function" in err_str.lower():
                # exec_sql function doesn't exist, try postgrest raw
                print(f"  [WARN]  exec_sql RPC not available. Printing SQL for manual execution.")
                print(f"\n--- SQL to run manually in Supabase SQL Editor ---")
                print(sql)
                print(f"--- End SQL ---\n")
                return False
            else:
                print(f"  [FAIL] Statement {i}/{len(statements)} failed: {e}")
                print(f"     SQL: {stmt[:100]}...")
                failed += 1

    print(f"\nResults: [OK] {success} succeeded, [SKIP] {skipped} skipped, [FAIL] {failed} failed")
    return failed == 0


if __name__ == "__main__":
    migrations = [
        "migrations/003_multi_tenant.sql",
        "migrations/004_seed_first_clinic.sql",
    ]

    if len(sys.argv) > 1:
        migrations = sys.argv[1:]

    all_ok = True
    for m in migrations:
        if not os.path.exists(m):
            print(f"[FAIL] Migration file not found: {m}")
            all_ok = False
            continue
        if not run_migration(m):
            all_ok = False

    if all_ok:
        print("\n[OK] All migrations completed successfully!")
    else:
        print("\n[WARN]  Some migrations need manual execution. See above for SQL.")
        print("   Copy the SQL and run it in: Supabase Dashboard → SQL Editor")
