-- ============================================================================
-- Migration 081: care pathway on the specialty treatments catalogue
-- ============================================================================
-- Raised by an eye hospital evaluating the plan: "our doctor examines the
-- patient and THEN decides the treatment". True for eye, dental and IVF, and
-- only partly true for dermatology, where a patient really does arrive saying
-- "acne" or "hair fall".
--
-- The catalogue could not express the difference. "Comprehensive Eye Check-up"
-- and "Cataract Surgery" were the same kind of row: one tap that books a
-- consultation. A patient tapping Cataract Surgery believed they were booking
-- surgery, and the admin had no way to say otherwise.
--
--   entry            -- the first visit. A new patient starts here, and this
--                       is where "I'm not sure what I need" leads.
--   direct           -- the patient can ask for this by name (teeth cleaning,
--                       LASIK evaluation, acne treatment). TODAY'S BEHAVIOUR,
--                       and the default, so no existing row changes.
--   assessment_first -- the doctor decides this after examining the patient
--                       (cataract surgery, root canal, an IVF cycle). Shown as
--                       information; its button books an EXAMINATION, never
--                       the procedure.
--
-- The bot reads this column and nothing else: a clinic that classifies nothing
-- behaves exactly as it does today. No plan is hardcoded, because an eye
-- hospital does sell directly-bookable items (routine check-up, contact lens
-- fitting) and a derma clinic does have doctor-decided ones (excisions).
--
-- PURELY ADDITIVE except the starter backfill below, which touches only rows
-- this codebase authored (source = 'starter') and which no admin can yet have
-- classified, because the column does not exist until this migration runs.
-- Re-runnable.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

ALTER TABLE specialty_treatments
    ADD COLUMN IF NOT EXISTS care_pathway TEXT NOT NULL DEFAULT 'direct';

-- DROP-then-ADD rather than a DO block: idempotent, and it survives the naive
-- statement splitter in run_migrations.py.
ALTER TABLE specialty_treatments
    DROP CONSTRAINT IF EXISTS specialty_treatments_care_pathway_check;

ALTER TABLE specialty_treatments
    ADD CONSTRAINT specialty_treatments_care_pathway_check
    CHECK (care_pathway IN ('entry', 'direct', 'assessment_first'));

-- ── Starter backfill ────────────────────────────────────────────────────────
-- Rows seeded from app/services/specialty_catalog.py, matched on the exact
-- name that file wrote. Scoped to source = 'starter' so a clinic's own
-- treatment that happens to share a name is never touched. Every other row --
-- and every name not listed here -- keeps the 'direct' default.
UPDATE specialty_treatments t
SET care_pathway = v.pathway, updated_at = now()
FROM (VALUES
    -- Ophthalmology: the examination is the product; surgery follows it.
    ('Comprehensive Eye Check-up',            'entry'),
    ('Cataract Surgery',                      'assessment_first'),
    ('Anti-VEGF Injection',                   'assessment_first'),
    -- Dental: check-up first; RCT, extraction or a crown is the dentist's call.
    ('Dental Check-up',                       'entry'),
    ('Root Canal Treatment',                  'assessment_first'),
    ('Crowns & Bridges',                      'assessment_first'),
    ('Dentures',                              'assessment_first'),
    ('Clear Aligners',                        'assessment_first'),
    ('Veneers & Smile Design',                'assessment_first'),
    ('Wisdom Tooth Removal',                  'assessment_first'),
    -- Fertility: nobody books an IVF cycle off a menu.
    ('Fertility Consultation',                'entry'),
    ('IUI (Intrauterine Insemination)',       'assessment_first'),
    ('IVF (In Vitro Fertilisation)',          'assessment_first'),
    ('ICSI',                                  'assessment_first'),
    ('Frozen Embryo Transfer',                'assessment_first'),
    ('Egg Freezing',                          'assessment_first'),
    ('Hysteroscopy',                          'assessment_first'),
    ('Laparoscopy for Fertility',             'assessment_first'),
    -- Dermatology keeps every row 'direct': a patient arrives already saying
    -- "acne" or "hair fall", which is the whole reason that plan works today.
    ('Skin Biopsy',                           'assessment_first')
) AS v(name, pathway)
WHERE t.source = 'starter'
  AND lower(btrim(t.name)) = lower(v.name);

-- Verification
SELECT care_pathway, count(*) FROM specialty_treatments GROUP BY care_pathway ORDER BY 1;
