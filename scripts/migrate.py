#!/usr/bin/env python3
"""Database migration runner for MediAssist AI / Kriya AI.

Applies all SQL migration files in numerical order against a real PostgreSQL database,
tracking applied migrations in the `schema_migrations` table.
"""

import argparse
import glob
import hashlib
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

import psycopg2

logger = logging.getLogger("migrate")


def bootstrap_database(conn) -> None:
    """Ensure standard roles, schema_migrations, and auth helper schema exist."""
    cur = conn.cursor()
    try:
        # Standard Supabase roles needed for RLS policy compilation
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

        # Auth schema and helpers for RLS policies
        cur.execute("""
        CREATE SCHEMA IF NOT EXISTS auth;
        CREATE OR REPLACE FUNCTION auth.role() RETURNS text AS $$
            SELECT COALESCE(current_setting('request.jwt.claim.role', true), 'service_role');
        $$ LANGUAGE sql STABLE;

        CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid AS $$
            SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid;
        $$ LANGUAGE sql STABLE;
        """)

        # schema_migrations tracking table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum VARCHAR(64) NOT NULL
        );
        """)
        conn.commit()
    finally:
        cur.close()


def get_applied_migrations(conn) -> Dict[str, dict]:
    """Return dictionary of applied migration records mapped by migration file name."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT name, applied_at, checksum FROM schema_migrations ORDER BY id ASC;")
        rows = cur.fetchall()
        return {row[0]: {"applied_at": row[1], "checksum": row[2]} for row in rows}
    finally:
        cur.close()


def apply_migrations(
    conn_or_url,
    migrations_dir: str = "migrations",
    dry_run: bool = False
) -> Tuple[int, int, List[str]]:
    """Apply all pending migration files in lexicographical order.

    Returns:
        (applied_count, skipped_count, failed_files)
    """
    is_url = isinstance(conn_or_url, str)
    conn = psycopg2.connect(conn_or_url) if is_url else conn_or_url

    try:
        bootstrap_database(conn)
        applied_map = get_applied_migrations(conn)

        pattern = os.path.join(migrations_dir, "[0-9]*.sql")
        migration_files = sorted(glob.glob(pattern))

        applied_count = 0
        skipped_count = 0
        failed_files = []

        for mf in migration_files:
            fname = os.path.basename(mf)
            if fname in applied_map:
                skipped_count += 1
                continue

            with open(mf, "r", encoding="utf-8") as f:
                sql = f.read()

            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

            if dry_run:
                print(f"[DRY-RUN] Would apply: {fname}")
                applied_count += 1
                continue

            cur = conn.cursor()
            try:
                # Apply migration inside transaction
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s);",
                    (fname, checksum)
                )
                conn.commit()
                applied_count += 1
                print(f"[OK] Applied: {fname}")
            except Exception as e:
                conn.rollback()
                failed_files.append(fname)
                print(f"[ERROR] Failed applying {fname}: {e}")
                break
            finally:
                cur.close()

        return applied_count, skipped_count, failed_files
    finally:
        if is_url:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="MediAssist AI Migration Runner")
    parser.add_argument("--url", default=os.getenv("DATABASE_URL"), help="PostgreSQL connection URI")
    parser.add_argument("--dir", default="migrations", help="Migrations directory")
    parser.add_argument("--status", action="store_true", help="Print migration status only")
    parser.add_argument("--dry-run", action="store_true", help="Simulate applying migrations")

    args = parser.parse_args()

    if not args.url:
        print("ERROR: DATABASE_URL not set and --url not provided.")
        sys.exit(1)

    conn = psycopg2.connect(args.url)
    try:
        bootstrap_database(conn)
        applied = get_applied_migrations(conn)

        if args.status:
            pattern = os.path.join(args.dir, "[0-9]*.sql")
            all_files = sorted(glob.glob(pattern))
            print(f"\n--- Migration Status for Database ---")
            print(f"Total migration files on disk: {len(all_files)}")
            print(f"Applied migrations in DB:     {len(applied)}\n")
            for mf in all_files:
                fname = os.path.basename(mf)
                if fname in applied:
                    print(f" [X] {fname} (applied at {applied[fname]['applied_at']})")
                else:
                    print(f" [ ] {fname} (PENDING)")
            return

        applied_count, skipped_count, failed = apply_migrations(conn, migrations_dir=args.dir, dry_run=args.dry_run)
        print(f"\nMigration Summary: {applied_count} applied, {skipped_count} skipped, {len(failed)} failed.")
        if failed:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
