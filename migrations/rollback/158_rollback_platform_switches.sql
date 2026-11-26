-- ============================================================================
-- Rollback 158: drop platform_switches. Code that reads it fails closed (CallMedex treated as OFF) when it is missing.
-- ============================================================================
DROP TABLE IF EXISTS platform_switches;
