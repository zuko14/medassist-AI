-- ============================================================================
-- Rollback 157: drop the clinic erase function. Nothing else was added; erased clinics stay erased.
-- ============================================================================
DROP FUNCTION IF EXISTS kriya_erase_clinic(UUID);
