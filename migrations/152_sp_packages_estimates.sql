-- ============================================================================
-- Migration 152: Specialty OS shared blocks S3, S5, S6, S9 (lane SP; docs/specialty-os/GAP-ANALYSIS.md §2)
--   S3 OPD packages: sp_packages (a course = one OPD service-catalogue code × N sessions, validity) → sp_package_sales
--      (the sale IS an ordinary OPD invoice; nothing new in GSTR-1) → sp_package_consumptions (one ledger row per
--      session used — never a zero-value invoice line, VERIFICATION D15); sp_instalments (due schedule over one
--      ordinary invoice; paid status is derived from opd_invoices.paid_paise, so receipts stay the only money path).
--   S5 estimates: sp_estimates (itemised options, validity, WhatsApp accept / decline — no money moves on accept).
--   S6 imaging links: sp_imaging_links (per-encounter https viewer link with modality and device id).
--   S9 camps: sp_camps + sp_camp_registrations (registration → screening (S2 form) → referral → booked visit →
--      verified only by a check-in or a signed encounter, never appointments.status).
--   (S4 consent templates are code-defined text over clinical_consents, migration 119: no table.)
-- Existing objects touched: none. Old-table references use kriya_guard_refs (144), never a FOREIGN KEY.
-- Rollback: rollback/152_rollback_sp_packages_estimates.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sp_packages (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code           TEXT NOT NULL CHECK (code ~ '^[A-Z0-9_-]{2,20}$'),
    name           TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    specialty      TEXT NOT NULL CHECK (specialty IN ('derma', 'eye', 'dental', 'ivf', 'womenchild', 'general')),
    service_code   TEXT NOT NULL CHECK (char_length(btrim(service_code)) BETWEEN 1 AND 40),   -- OPD catalogue code (price, HSN, GST)
    sessions_total SMALLINT NOT NULL CHECK (sessions_total BETWEEN 1 AND 100),
    validity_days  SMALLINT NOT NULL DEFAULT 365 CHECK (validity_days BETWEEN 1 AND 1095),
    is_active      BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_packages_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_packages_code_key UNIQUE (clinic_id, code)
);

CREATE TABLE IF NOT EXISTS sp_package_sales (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    package_id       UUID NOT NULL,
    patient_id       UUID NOT NULL,
    family_member_id UUID NULL,
    invoice_id       UUID NOT NULL,                         -- the ordinary OPD invoice of the sale
    sessions_total   SMALLINT NOT NULL CHECK (sessions_total BETWEEN 1 AND 100),
    price_paise      INTEGER NOT NULL CHECK (price_paise > 0),
    expires_on       DATE NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'expired', 'cancelled')),
    cancel_reason    TEXT NULL CHECK (char_length(btrim(cancel_reason)) BETWEEN 3 AND 300),
    sold_by_name     TEXT NOT NULL CHECK (char_length(btrim(sold_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_package_sales_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_package_sales_invoice_key UNIQUE (clinic_id, invoice_id),
    CONSTRAINT sp_package_sales_package_fk FOREIGN KEY (clinic_id, package_id) REFERENCES sp_packages (clinic_id, id),
    CONSTRAINT sp_package_sales_cancel CHECK ((status = 'cancelled') = (cancel_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_sp_package_sales_patient ON sp_package_sales (clinic_id, patient_id, status);

CREATE TABLE IF NOT EXISTS sp_package_consumptions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sale_id          UUID NOT NULL,
    session_no       SMALLINT NOT NULL CHECK (session_no BETWEEN 1 AND 100),
    appointment_id   UUID NULL,
    encounter_id     UUID NULL,
    consumed_by_name TEXT NOT NULL CHECK (char_length(btrim(consumed_by_name)) BETWEEN 2 AND 100),
    consumed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at      TIMESTAMPTZ NULL,
    reverse_reason   TEXT NULL CHECK (char_length(btrim(reverse_reason)) BETWEEN 3 AND 300),
    CONSTRAINT sp_package_consumptions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_package_consumptions_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES sp_package_sales (clinic_id, id),
    CONSTRAINT sp_package_consumptions_reversed CHECK ((reversed_at IS NULL) = (reverse_reason IS NULL))
);
-- one live consumption per session number: two staff marking "session 3" at once store it once
CREATE UNIQUE INDEX IF NOT EXISTS uq_sp_package_consumption_live ON sp_package_consumptions (clinic_id, sale_id, session_no)
    WHERE reversed_at IS NULL;

-- A session can be consumed only on an active, unexpired sale and only within its session count.
CREATE OR REPLACE FUNCTION sp_package_consume_guard() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s RECORD;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.sale_id <> OLD.sale_id OR NEW.session_no <> OLD.session_no OR OLD.reversed_at IS NOT NULL THEN
            RAISE EXCEPTION 'sp_consumption_immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    SELECT status, sessions_total, expires_on INTO s FROM sp_package_sales WHERE clinic_id = NEW.clinic_id AND id = NEW.sale_id;
    IF s.status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'sp_package_not_active' USING ERRCODE = '23514';
    END IF;
    IF NEW.session_no > s.sessions_total THEN
        RAISE EXCEPTION 'sp_package_sessions_exhausted' USING ERRCODE = '23514';
    END IF;
    IF s.expires_on < (now() AT TIME ZONE 'Asia/Kolkata')::date THEN
        RAISE EXCEPTION 'sp_package_expired' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;

CREATE TABLE IF NOT EXISTS sp_instalments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id      UUID NOT NULL,
    seq             SMALLINT NOT NULL CHECK (seq BETWEEN 1 AND 60),
    due_on          DATE NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (amount_paise > 0),
    reminded_at     TIMESTAMPTZ NULL,
    created_by_name TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_instalments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_instalments_seq_key UNIQUE (clinic_id, invoice_id, seq)
);

CREATE TABLE IF NOT EXISTS sp_estimates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    specialty           TEXT NOT NULL CHECK (specialty IN ('derma', 'eye', 'dental', 'ivf', 'womenchild', 'general')),
    title               TEXT NOT NULL CHECK (char_length(btrim(title)) BETWEEN 3 AND 120),
    options             JSONB NOT NULL CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) BETWEEN 1 AND 4),
    valid_until         DATE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'accepted', 'declined', 'expired',
                                                                       'converted')),
    sent_at             TIMESTAMPTZ NULL,
    sent_message_id     TEXT NULL CHECK (char_length(sent_message_id) <= 200),
    accepted_option     TEXT NULL CHECK (accepted_option ~ '^[a-z0-9_]{1,20}$'),
    responded_at        TIMESTAMPTZ NULL,
    response_message_id TEXT NULL CHECK (char_length(response_message_id) <= 200),
    dental_plan_id      UUID NULL,
    converted_sale_id   UUID NULL,
    created_by_name     TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_estimates_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_estimates_sale_fk FOREIGN KEY (clinic_id, converted_sale_id) REFERENCES sp_package_sales (clinic_id, id),
    CONSTRAINT sp_estimates_accepted CHECK (status NOT IN ('accepted', 'converted') OR accepted_option IS NOT NULL),
    CONSTRAINT sp_estimates_sent CHECK (status = 'draft' OR sent_at IS NOT NULL),
    CONSTRAINT sp_estimates_converted CHECK ((status = 'converted') = (converted_sale_id IS NOT NULL))
);
-- a WhatsApp reply is recorded once (duplicate webhook delivery)
CREATE UNIQUE INDEX IF NOT EXISTS uq_sp_estimates_response ON sp_estimates (clinic_id, response_message_id)
    WHERE response_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sp_estimates_patient ON sp_estimates (clinic_id, patient_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sp_imaging_links (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    encounter_id  UUID NOT NULL,
    patient_id    UUID NOT NULL,
    modality      TEXT NOT NULL CHECK (modality IN ('oct', 'fundus', 'perimetry', 'biometry', 'topography', 'rvg', 'opg',
                                                    'cbct', 'usg', 'dermoscopy', 'other')),
    device_id     TEXT NULL CHECK (char_length(btrim(device_id)) BETWEEN 1 AND 60),
    viewer_url    TEXT NOT NULL CHECK (viewer_url ~ '^https://[^\s]{4,}$' AND char_length(viewer_url) <= 1000),
    label         TEXT NULL CHECK (char_length(label) <= 120),
    added_by_name TEXT NOT NULL CHECK (char_length(btrim(added_by_name)) BETWEEN 2 AND 100),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_imaging_links_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_sp_imaging_links_encounter ON sp_imaging_links (clinic_id, encounter_id);

CREATE TABLE IF NOT EXISTS sp_camps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name            TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 3 AND 120),
    kind            TEXT NOT NULL CHECK (kind IN ('cataract', 'dental', 'skin', 'health', 'immunisation', 'other')),
    camp_date       DATE NOT NULL,
    location        TEXT NOT NULL CHECK (char_length(btrim(location)) BETWEEN 3 AND 200),
    branch_id       UUID NULL,
    status          TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'held', 'closed')),
    created_by_name TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_camps_clinic_id_id_key UNIQUE (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS sp_camp_registrations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    camp_id           UUID NOT NULL,
    name              TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 100),
    phone             TEXT NULL CHECK (phone ~ '^\+?[0-9]{10,15}$'),
    age_years         SMALLINT NULL CHECK (age_years BETWEEN 0 AND 120),
    patient_id        UUID NULL,
    screening         JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(screening) = 'object'),
    outcome           TEXT NOT NULL DEFAULT 'registered' CHECK (outcome IN ('registered', 'screened_ok', 'referred',
                                                                           'not_referred')),
    referral_reason   TEXT NULL CHECK (char_length(btrim(referral_reason)) BETWEEN 3 AND 300),
    appointment_id    UUID NULL,
    verified_at       TIMESTAMPTZ NULL,
    verified_evidence TEXT NULL CHECK (verified_evidence IN ('check_in', 'signed_encounter')),
    created_by_name   TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_camp_registrations_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_camp_registrations_camp_fk FOREIGN KEY (clinic_id, camp_id) REFERENCES sp_camps (clinic_id, id),
    CONSTRAINT sp_camp_registrations_referral CHECK ((outcome = 'referred') = (referral_reason IS NOT NULL)),
    CONSTRAINT sp_camp_registrations_verified CHECK ((verified_at IS NULL) = (verified_evidence IS NULL)),
    CONSTRAINT sp_camp_registrations_verified_needs_visit CHECK (verified_at IS NULL OR appointment_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_sp_camp_registrations_camp ON sp_camp_registrations (clinic_id, camp_id, outcome);

-- the S1 photo of a package session points at its sale (both SP tables: a real FK)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sp_media_package_sale_fk') THEN
        ALTER TABLE sp_media ADD CONSTRAINT sp_media_package_sale_fk FOREIGN KEY (clinic_id, package_sale_id)
            REFERENCES sp_package_sales (clinic_id, id);
    END IF;
END $$;

-- ── same-clinic guards into earlier migrations' tables (never a FOREIGN KEY) ─
DROP TRIGGER IF EXISTS trg_sp_package_sales_refs ON sp_package_sales;
CREATE TRIGGER trg_sp_package_sales_refs BEFORE INSERT OR UPDATE ON sp_package_sales FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members',
                                      'invoice_id', 'opd_invoices');
DROP TRIGGER IF EXISTS trg_sp_package_consumptions_refs ON sp_package_consumptions;
CREATE TRIGGER trg_sp_package_consumptions_refs BEFORE INSERT OR UPDATE ON sp_package_consumptions FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('appointment_id', 'appointments', 'encounter_id', 'opd_encounters');
DROP TRIGGER IF EXISTS trg_sp_package_consumptions_guard ON sp_package_consumptions;
CREATE TRIGGER trg_sp_package_consumptions_guard BEFORE INSERT OR UPDATE ON sp_package_consumptions FOR EACH ROW
    EXECUTE FUNCTION sp_package_consume_guard();
DROP TRIGGER IF EXISTS trg_sp_instalments_refs ON sp_instalments;
CREATE TRIGGER trg_sp_instalments_refs BEFORE INSERT OR UPDATE ON sp_instalments FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('invoice_id', 'opd_invoices');
DROP TRIGGER IF EXISTS trg_sp_estimates_refs ON sp_estimates;
CREATE TRIGGER trg_sp_estimates_refs BEFORE INSERT OR UPDATE ON sp_estimates FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members',
                                      'dental_plan_id', 'dental_treatment_plans');
DROP TRIGGER IF EXISTS trg_sp_imaging_links_refs ON sp_imaging_links;
CREATE TRIGGER trg_sp_imaging_links_refs BEFORE INSERT OR UPDATE ON sp_imaging_links FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('encounter_id', 'opd_encounters', 'patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_sp_camps_refs ON sp_camps;
CREATE TRIGGER trg_sp_camps_refs BEFORE INSERT OR UPDATE ON sp_camps FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('branch_id', 'branches');
DROP TRIGGER IF EXISTS trg_sp_camp_registrations_refs ON sp_camp_registrations;
CREATE TRIGGER trg_sp_camp_registrations_refs BEFORE INSERT OR UPDATE ON sp_camp_registrations FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'appointment_id', 'appointments');

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sp_packages', 'sp_package_sales', 'sp_package_consumptions', 'sp_instalments',
                             'sp_estimates', 'sp_imaging_links', 'sp_camps', 'sp_camp_registrations']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;

-- ── 152 verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sp_packages', 'sp_package_sales', 'sp_package_consumptions', 'sp_instalments',
                             'sp_estimates', 'sp_imaging_links', 'sp_camps', 'sp_camp_registrations']
    LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '152 verify: % missing', t;
        END IF;
    END LOOP;
    IF to_regprocedure('sp_package_consume_guard()') IS NULL THEN
        RAISE EXCEPTION '152 verify: sp_package_consume_guard missing';
    END IF;
END $$;
