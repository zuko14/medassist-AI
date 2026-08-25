import os
import glob
import hashlib
import psycopg2
import pgserver

def test_migrations():
    pg = pgserver.get_server("tmp_pg_migrate_test")
    uri = pg.get_uri()
    print(f"Connected to PostgreSQL at {uri}")
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    cur = conn.cursor()

    # Ensure Supabase standard roles exist for RLS policies
    cur.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
            CREATE ROLE service_role;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            CREATE ROLE authenticated;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
            CREATE ROLE anon;
        END IF;
    END
    $$;
    """)

    # Ensure Supabase auth schema and functions exist for RLS policies
    cur.execute("""
    CREATE SCHEMA IF NOT EXISTS auth;
    CREATE OR REPLACE FUNCTION auth.role() RETURNS text AS $$
        SELECT COALESCE(current_setting('request.jwt.claim.role', true), 'service_role');
    $$ LANGUAGE sql STABLE;

    CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid AS $$
        SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid;
    $$ LANGUAGE sql STABLE;
    """)

    # gen_random_uuid() is built-in in PostgreSQL 13+
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) UNIQUE NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        checksum VARCHAR(64)
    );
    """)

    cur.execute("SELECT name FROM schema_migrations;")
    applied_set = set(row[0] for row in cur.fetchall())

    migration_files = sorted(glob.glob("migrations/[0-9]*.sql"))
    print(f"Found {len(migration_files)} migration files ({len(applied_set)} already recorded as applied).")

    applied = 0
    failed = 0
    for mf in migration_files:
        fname = os.path.basename(mf)
        if fname in applied_set:
            print(f"  [ALREADY APPLIED] {fname}")
            continue
        print(f"Applying: {fname}...")
        with open(mf, "r", encoding="utf-8") as f:
            sql = f.read()
        try:
            cur.execute(sql)
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            cur.execute("INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)", (fname, checksum))
            print(f"  [OK] {fname}")
            applied += 1
        except Exception as e:
            print(f"  [ERROR] in {fname}: {e}")
            failed += 1
            break

    print(f"\nMigration Run Complete: {applied} applied, {failed} failed.")
    conn.close()
    pg.cleanup()
    return failed == 0

if __name__ == "__main__":
    test_migrations()
