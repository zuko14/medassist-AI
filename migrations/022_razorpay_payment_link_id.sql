-- Migration 022: Track Razorpay Payment Link ID for reconciliation
-- Run in Supabase SQL Editor
--
-- create_booking_with_payment() previously built a raw
-- api.razorpay.com/v1/checkout/embedded URL, which is an API endpoint meant
-- for checkout.js embedding, not a browsable hosted page — patients tapping
-- this link in WhatsApp could not complete payment. Switched to Razorpay's
-- Payment Links API, which returns a real rzp.io short URL. Payment Links
-- attach captured payments to a payment_link_id (not order_id), so we need
-- a column to correlate incoming webhooks back to the booking.

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS razorpay_payment_link_id TEXT NULL;
CREATE INDEX IF NOT EXISTS idx_appointments_payment_link_id
    ON appointments (razorpay_payment_link_id) WHERE razorpay_payment_link_id IS NOT NULL;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name = 'razorpay_payment_link_id';
