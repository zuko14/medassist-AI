-- Rollback Migration 098. Deletes all voice call history, numbers, lexicon and outbound jobs.
DROP TABLE IF EXISTS voice_outbound_jobs;
DROP TABLE IF EXISTS voice_call_events;
DROP TABLE IF EXISTS voice_calls;
DROP TABLE IF EXISTS voice_lexicon_entries;
DROP TABLE IF EXISTS voice_numbers;
