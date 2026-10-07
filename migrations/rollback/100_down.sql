-- Rollback Migration 100. Only safe once no clinic uses PhonePe and no
-- PhonePe booking is pending or refundable: without payment_gateway those rows
-- would be treated as Razorpay bookings.
ALTER TABLE appointments DROP COLUMN IF EXISTS gateway_order_id;
ALTER TABLE appointments DROP COLUMN IF EXISTS payment_gateway;
