-- Rollback Migration 110. Drops kriya_config_apply(). No data changes: configs stay as stored.
-- The app falls back to whole-object writes when the function is missing (config_store.save_config),
-- so rolling back brings D-13 (lost update) back but breaks nothing.
DROP FUNCTION IF EXISTS kriya_config_apply(text, uuid, uuid, jsonb, jsonb);
