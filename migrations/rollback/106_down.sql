-- Rollback Migration 106. Pending learning suggestions and harvested phrases are lost;
-- approved entries already live in voice_lexicon_entries / voice_knowledge_entries stay.
DROP TABLE IF EXISTS ai_learning_suggestions;
DROP TABLE IF EXISTS ai_reception_harvest_events;
