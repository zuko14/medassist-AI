-- Rollback Migration 094. Storage objects under */dental-sittings/* are NOT removed
-- by this; delete them from the lab-reports bucket separately if required.
DROP TABLE IF EXISTS dental_sitting_photos;
