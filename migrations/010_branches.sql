-- ============================================================
-- Migration 010: Multi-Branch Support
-- Adds branches table, doctor_branches junction table,
-- and branch_id to appointments.
-- ============================================================

-- 1. Branches table (child of clinics)
CREATE TABLE IF NOT EXISTS branches (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL,            -- "Kukatpally Branch"
    short_name      TEXT,                            -- "KPL" (for booking refs, max 4 chars)
    address         TEXT,                            -- Full street address
    landmark        TEXT,                            -- "Near Metro Station"
    maps_link       TEXT,                            -- Google Maps URL
    phone           TEXT,                            -- Branch-specific contact number
    is_diagnostic   BOOLEAN     NOT NULL DEFAULT false,  -- true = diagnostics-only (no booking)
    is_active       BOOLEAN     NOT NULL DEFAULT true,
    display_order   INTEGER     NOT NULL DEFAULT 0,      -- Controls WhatsApp list ordering
    config          JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- Branch-level overrides
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_branches_clinic ON branches(clinic_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_branches_clinic_name ON branches(clinic_id, name);

-- Function for updating timestamp (if it doesn't exist)
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for updating timestamp
CREATE TRIGGER update_branches_updated_at
    BEFORE UPDATE ON branches
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- 2. Doctor ↔ Branch many-to-many junction table
--    A doctor can work at multiple branches with session control:
--      - 'morning' = only morning slots available at this branch
--      - 'evening' = only evening slots available at this branch
--      - 'both'    = all slots available at this branch
CREATE TABLE IF NOT EXISTS doctor_branches (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doctor_id   UUID NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    branch_id   UUID NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    session     TEXT NOT NULL DEFAULT 'both'
                CHECK (session IN ('morning', 'evening', 'both')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(doctor_id, branch_id)
);

CREATE INDEX IF NOT EXISTS idx_doctor_branches_doctor ON doctor_branches(doctor_id);
CREATE INDEX IF NOT EXISTS idx_doctor_branches_branch ON doctor_branches(branch_id);


-- 3. Add branch_id to appointments (nullable — NULL = single-branch / legacy)
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id);
CREATE INDEX IF NOT EXISTS idx_appointments_branch ON appointments(branch_id);


-- 4. Add branch_name to appointments for denormalized display
--    (Avoids join in reminder/confirmation queries)
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS branch_name TEXT;


-- NOTE: No branch_id on hospital_holidays (holidays are clinic-level)
-- NOTE: No branch_id on lab_reports (reports are patient-scoped)
-- NOTE: No branch_id on doctors directly (use doctor_branches junction instead)


-- ============================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- ============================================================

-- 1. RLS for branches
ALTER TABLE branches ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Branches are viewable by everyone" 
ON branches FOR SELECT USING (true);

CREATE POLICY "Branches insertable by admins only" 
ON branches FOR INSERT WITH CHECK (false); -- Blocked for public, allowed for service_role

CREATE POLICY "Branches updatable by admins only" 
ON branches FOR UPDATE USING (false);

CREATE POLICY "Branches deletable by admins only" 
ON branches FOR DELETE USING (false);

-- 2. RLS for doctor_branches
ALTER TABLE doctor_branches ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Doctor branches are viewable by everyone" 
ON doctor_branches FOR SELECT USING (true);

CREATE POLICY "Doctor branches insertable by admins only" 
ON doctor_branches FOR INSERT WITH CHECK (false);

CREATE POLICY "Doctor branches deletable by admins only" 
ON doctor_branches FOR DELETE USING (false);
