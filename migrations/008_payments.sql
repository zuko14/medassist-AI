-- ============================================================================
-- Migration 008: Payment Module — Razorpay Integration
-- ============================================================================
-- Extends the existing `appointments` table with payment-gating columns
-- and creates the `payment_events` append-only audit table.
--
-- CRITICAL: The partial UNIQUE constraint on (doctor_name, appointment_date,
-- appointment_time) WHERE status IN ('pending_payment','confirmed') is what
-- prevents double-booking at the database level. Do NOT remove this.
-- ============================================================================

-- ── Step 1: Add payment columns to existing appointments table ──

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS razorpay_order_id TEXT,
    ADD COLUMN IF NOT EXISTS payment_id TEXT,
    ADD COLUMN IF NOT EXISTS amount_paise INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hold_expires_at TIMESTAMPTZ;

-- Update status CHECK constraint to include new statuses
-- First drop the old one, then add the new one
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_status_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_status_check
    CHECK (status IN (
        'confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show',
        'pending_payment', 'expired', 'refunded', 'pending_review'
    ));

-- ── Step 2: THE critical anti-double-booking constraint ──
-- Only ONE active booking (pending_payment or confirmed) can exist for a
-- given doctor + date + time combination. This is enforced at the Postgres
-- engine level — no race condition can bypass it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_slot
    ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('pending_payment', 'confirmed');

-- ── Step 3: Payment indexes ──
CREATE INDEX IF NOT EXISTS idx_appointments_razorpay_order
    ON appointments (razorpay_order_id)
    WHERE razorpay_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_appointments_payment_id
    ON appointments (payment_id)
    WHERE payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_appointments_hold_expires
    ON appointments (hold_expires_at)
    WHERE status = 'pending_payment';

CREATE INDEX IF NOT EXISTS idx_appointments_pending_review
    ON appointments (status)
    WHERE status = 'pending_review';

-- ── Step 4: Payment events — append-only audit table ──
-- NEVER update or delete rows from this table. It is the audit trail
-- for all payment-related state changes.
CREATE TABLE IF NOT EXISTS payment_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id UUID NOT NULL REFERENCES appointments(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    -- e.g.: order_created, webhook_received, signature_verified,
    --        signature_failed, confirmed, refund_initiated,
    --        refund_completed, mismatch_flagged, expired,
    --        hold_expired, recovery_confirmed
    raw_payload JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_events_booking
    ON payment_events (booking_id);

CREATE INDEX IF NOT EXISTS idx_payment_events_type
    ON payment_events (event_type);

CREATE INDEX IF NOT EXISTS idx_payment_events_created
    ON payment_events (created_at);

-- ── Step 5: RLS on payment_events ──
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;

-- Allow service_role full access (our backend uses service_role key).
-- anon and authenticated have NO access to this audit table.
CREATE POLICY payment_events_service_role_all ON payment_events
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ── Step 6: Protect payment_events from UPDATE/DELETE ──
-- This trigger prevents any UPDATE or DELETE on the audit table.
-- Uses SECURITY INVOKER (not DEFINER) because triggers run with
-- the privileges of the table owner, not the calling user.
CREATE OR REPLACE FUNCTION prevent_payment_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'payment_events table is append-only. UPDATE and DELETE are not permitted.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public;

-- Revoke EXECUTE from public-facing roles so it cannot be called
-- via /rest/v1/rpc/prevent_payment_event_mutation
REVOKE EXECUTE ON FUNCTION prevent_payment_event_mutation() FROM anon, authenticated, public;

DROP TRIGGER IF EXISTS trg_payment_events_no_update ON payment_events;
CREATE TRIGGER trg_payment_events_no_update
    BEFORE UPDATE OR DELETE ON payment_events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_payment_event_mutation();
