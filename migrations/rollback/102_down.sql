-- Rollback Migration 102. Clinic-written receptionist answers are lost.
DROP TABLE IF EXISTS voice_knowledge_entries;
