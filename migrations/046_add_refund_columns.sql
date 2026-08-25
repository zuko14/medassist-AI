-- ============================================================================
-- Migration 046: Add refund tracking columns to appointments table
-- ============================================================================
-- Fixes P0-1: Enables recording refund identifiers, refund reasons (e.g.
-- 'late_payment', 'admin_cancelled', 'patient_cancelled'), and timestamps
-- without mutating the canonical status constraint ('refunded').
-- ============================================================================

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS refund_id TEXT,
    ADD COLUMN IF NOT EXISTS refund_reason TEXT,
    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_appointments_refund_id
    ON appointments (refund_id)
    WHERE refund_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_appointments_refund_reason
    ON appointments (refund_reason)
    WHERE refund_reason IS NOT NULL;
