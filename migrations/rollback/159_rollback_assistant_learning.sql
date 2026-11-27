-- ============================================================================
-- Rollback 159: drop assistant_learned_phrases. Kriya Ketu reads it only after its built-in rules found nothing,
-- and treats a missing table as "nothing learned", so routing is unchanged without it.
-- ============================================================================
DROP TABLE IF EXISTS assistant_learned_phrases;
