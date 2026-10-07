-- ============================================================================
-- Migration 100: per-booking payment gateway (Razorpay | PhonePe)
-- ============================================================================
-- A clinic can now charge through Razorpay or PhonePe (clinics.config
-- .payment_gateway). The gateway a booking was charged through must stay with
-- the BOOKING: a clinic that switches gateways still has pending holds and
-- refundable payments on the old one, and confirming or refunding those
-- through the new gateway would fail or hit the wrong account.
--
-- payment_gateway   NULL = razorpay (every row older than this migration;
--                   Razorpay bookings keep writing NULL). 'phonepe' otherwise.
-- gateway_order_id  PhonePe's orderId, written once the order exists. Its
--                   presence is what lets the expiry/poll sweeps ask PhonePe
--                   about a hold (the role razorpay_payment_link_id plays).
--
-- Additive, nullable, no backfill; metadata-only ALTERs.
-- ============================================================================

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS payment_gateway TEXT
        CONSTRAINT chk_appointments_payment_gateway
        CHECK (payment_gateway IS NULL OR payment_gateway IN ('razorpay', 'phonepe'));

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS gateway_order_id TEXT;
