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

-- Enable RLS (even though this table has no patient data, enforce good habits)
ALTER TABLE rate_limits ENABLE ROW LEVEL SECURITY;

-- Auto-cleanup: delete rate limit entries older than 1 hour (optional cron)
-- You can set this up in Supabase Dashboard → Database → Extensions → pg_cron


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

-- Index for querying pending messages
CREATE INDEX IF NOT EXISTS idx_failed_messages_status ON failed_messages(status);

-- ═══════════════════════════════════════════════════════════════════
-- VERIFICATION: Run this to confirm tables were created
-- ═══════════════════════════════════════════════════════════════════
-- SELECT table_name FROM information_schema.tables 
-- WHERE table_schema = 'public' 
-- AND table_name IN ('rate_limits', 'failed_messages');
