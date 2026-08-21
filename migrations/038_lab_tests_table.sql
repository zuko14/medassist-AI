-- Migration 038: Lab test catalog table
--
-- Diagnostic centers configure their test menu here (name, sample type,
-- price, turnaround, prep instructions). branch_id is nullable — a NULL
-- branch_id means the test is offered clinic-wide across all branches.

CREATE TABLE IF NOT EXISTS lab_tests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    branch_id UUID REFERENCES branches(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    sample_type TEXT,
    prep_instructions TEXT,
    fasting_required BOOLEAN NOT NULL DEFAULT false,
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    turnaround_hours INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_tests_clinic_active ON lab_tests(clinic_id, is_active);
CREATE INDEX IF NOT EXISTS idx_lab_tests_branch ON lab_tests(branch_id) WHERE branch_id IS NOT NULL;

-- Enable RLS and grant service_role full access (standard repo defense-in-depth pattern)
ALTER TABLE lab_tests ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all_lab_tests" ON lab_tests
    FOR ALL TO service_role USING (true) WITH CHECK (true);

