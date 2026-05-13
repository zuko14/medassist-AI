-- ═══════════════════════════════════════════════════════════════════
-- MediAssist AI — Security Tables Migration
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New Query)
-- ═══════════════════════════════════════════════════════════════════

-- 1. Rate Limits table (persistent brute-force protection)
-- Stores login attempt counts per IP, survives service restarts
CREATE TABLE IF NOT EXISTS rate_limits (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    key TEXT NOT NULL,                              -- IP address or identifier
    attempts INT DEFAULT 1,
    window_start TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Unique index so we upsert per key instead of creating duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_rate_limits_key ON rate_limits(key);

-- Enable RLS
ALTER TABLE rate_limits ENABLE ROW LEVEL SECURITY;

-- RLS Policy: ONLY the service_role can read/write this table.
-- The anon key (used by frontend) gets zero access.
-- Your FastAPI backend uses supabase_service_role_key, so it bypasses RLS.
-- This policy exists as a safety net — if someone ever gets the anon key,
-- they still can't read or write rate limit data.
CREATE POLICY "Service role full access on rate_limits"
    ON rate_limits
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');


-- 2. Failed Messages table (dead-letter queue for dropped webhooks)
-- If a message fails to process (e.g. server restart mid-task),
-- the raw payload is saved here for manual retry or investigation
CREATE TABLE IF NOT EXISTS failed_messages (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    phone TEXT,                                     -- sender phone
    display_phone TEXT,                             -- clinic WhatsApp number
    payload TEXT,                                   -- raw JSON payload
    error TEXT,                                     -- error message
    status TEXT DEFAULT 'pending',                  -- pending | retried | resolved
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- Enable RLS
ALTER TABLE failed_messages ENABLE ROW LEVEL SECURITY;

-- RLS Policy: ONLY the service_role can access failed messages.
-- These contain raw webhook payloads — patient data lives here.
CREATE POLICY "Service role full access on failed_messages"
    ON failed_messages
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- Index for querying pending messages
CREATE INDEX IF NOT EXISTS idx_failed_messages_status ON failed_messages(status);

-- ═══════════════════════════════════════════════════════════════════
-- VERIFICATION: Run this AFTER the migration to confirm everything
-- ═══════════════════════════════════════════════════════════════════
-- SELECT tablename, rowsecurity 
-- FROM pg_tables 
-- WHERE tablename IN ('rate_limits', 'failed_messages');
--
-- Expected: both rows show rowsecurity = true
-- ═══════════════════════════════════════════════════════════════════
