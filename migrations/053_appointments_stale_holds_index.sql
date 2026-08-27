-- Migration 053: Index for bounded expire_stale_bookings query (T1.3 / KRIYA-014)
-- Optimizes SELECT ... WHERE status = 'pending_payment' AND hold_expires_at < now LIMIT 200

CREATE INDEX IF NOT EXISTS idx_appointments_stale_holds
    ON appointments (hold_expires_at)
    WHERE status = 'pending_payment';
