-- ============================================================
-- Migration 013: Fix RLS Policies on branches and doctor_branches
-- Replaces world-readable USING (true) policies with service_role-restricted access.
-- ============================================================

-- 1. Drop overly permissive world-readable policies on branches
DROP POLICY IF EXISTS "Branches are viewable by everyone" ON branches;
DROP POLICY IF EXISTS "Branches insertable by admins only" ON branches;
DROP POLICY IF EXISTS "Branches updatable by admins only" ON branches;
DROP POLICY IF EXISTS "Branches deletable by admins only" ON branches;

-- 2. Drop overly permissive world-readable policies on doctor_branches
DROP POLICY IF EXISTS "Doctor branches are viewable by everyone" ON doctor_branches;
DROP POLICY IF EXISTS "Doctor branches insertable by admins only" ON doctor_branches;
DROP POLICY IF EXISTS "Doctor branches deletable by admins only" ON doctor_branches;

-- 3. Restrict branches table access to service_role
DROP POLICY IF EXISTS "Service role access for branches" ON branches;
CREATE POLICY "Service role access for branches" ON branches
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- 4. Restrict doctor_branches table access to service_role
DROP POLICY IF EXISTS "Service role access for doctor_branches" ON doctor_branches;
CREATE POLICY "Service role access for doctor_branches" ON doctor_branches
    FOR ALL TO service_role USING (true) WITH CHECK (true);
