"""Tests for Database RLS Policy Security (Finding #9)."""

import os
from pathlib import Path


def test_migration_013_remediates_branch_rls():
    """Verify Migration 013 drops world-readable RLS policies and restricts to service_role."""
    migration_path = Path("migrations/013_fix_branch_rls.sql")
    assert migration_path.exists(), "Migration 013 file missing"

    content = migration_path.read_text(encoding="utf-8")

    # Assert dropping of world-readable policies
    assert 'DROP POLICY IF EXISTS "Branches are viewable by everyone" ON branches' in content
    assert 'DROP POLICY IF EXISTS "Doctor branches are viewable by everyone" ON doctor_branches' in content

    # Assert service_role restriction
    assert 'TO service_role USING (true) WITH CHECK (true)' in content
    assert 'CREATE POLICY "Service role access for branches" ON branches' in content
    assert 'CREATE POLICY "Service role access for doctor_branches" ON doctor_branches' in content
