-- ============================================================================
-- Migration 131: Kriya Diagnostic OS reports, templates and delivery (DX-M7)
-- + P6b adoption of OPD / IPD lab orders onto the DX bench (DX-41).
--
-- Report templates (immutable once published), visit-level releases with a
-- frozen template / letterhead / routing snapshot and a public verify code,
-- one delivery row per release x target ('dx-<release>-p|r|a'), audited dues
-- overrides, a desk print log, share links, and the re-evaluation outbox fed by
-- triggers on opd_receipts / opd_invoices (no payment code is edited).
--
-- dx_delivery_decision() is the single rulebook (DELIVERY-POLICY.md §3):
--   NO PAYMENT, NO REPORT (rule 7) and PAYER-ROUTED VISITS NEVER GO TO THE
--   PATIENT (rule 3) are enforced here, re-checked by dx_claim_delivery() right
--   before the only sender (app/services/dx_reports.send_delivery) runs.
--   Rule 9 (plan has WhatsApp lab reports) is a Python plan lookup, checked by
--   the sender after the claim (status.md §7).
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §8, DELIVERY-POLICY.md, ADR-DX-07/08/15/19/20, W10/W11/W15.
-- Additive: eight new tables, two AFTER triggers on OPD money tables that only
-- write to dx_outbox for DX bills (accession_id IS NOT NULL), new functions,
-- dx_purge_clinic v7. Existing functions changed: opd_guard_order (two guarded
-- DX exemptions), and the DX bench checks "is a DX order" now mean "has a DX
-- visit" (adopted OPD/IPD orders keep source 'opd' / 'ipd').
-- ============================================================================

-- ── 1. Templates ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_report_templates (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('clinic', 'branch', 'b2b_account')),
    owner_id     UUID NULL,
    name         TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    version      SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 999),
    layout       JSONB NOT NULL CHECK (jsonb_typeof(layout) = 'object'),
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'retired')),
    published_at TIMESTAMPTZ NULL,
    published_by UUID NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_report_templates_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_report_templates_version_key UNIQUE NULLS NOT DISTINCT (clinic_id, owner_kind, owner_id, name, version),
    CONSTRAINT dx_report_templates_owner_shape CHECK ((owner_kind = 'clinic') = (owner_id IS NULL)),
    CONSTRAINT dx_report_templates_published_shape CHECK (status = 'draft' OR published_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_dx_report_templates_owner
    ON dx_report_templates (clinic_id, owner_kind, owner_id, status, published_at DESC);

-- ADR-DX-08: a published template never changes; the only move is published -> retired.
CREATE OR REPLACE FUNCTION dx_guard_report_template() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) OR OLD.status = 'draft' THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_template_published');
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN PERFORM dx_raise('dx_template_publish_via_function'); END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'retired' THEN PERFORM dx_raise('dx_template_not_published'); END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'published' AND NEW.status = 'retired'
       AND (to_jsonb(NEW) - ARRAY['status', 'updated_at']) = (to_jsonb(OLD) - ARRAY['status', 'updated_at']) THEN
        RETURN NEW;
    END IF;
    PERFORM dx_raise('dx_template_published');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_report_templates_guard ON dx_report_templates;
CREATE TRIGGER trg_dx_report_templates_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_report_templates
    FOR EACH ROW EXECUTE FUNCTION dx_guard_report_template();
DROP TRIGGER IF EXISTS trg_dx_report_templates_touch ON dx_report_templates;
CREATE TRIGGER trg_dx_report_templates_touch BEFORE UPDATE ON dx_report_templates
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Publish = the only way a draft becomes published (stamped by the database).
CREATE OR REPLACE FUNCTION dx_publish_template(p_clinic_id UUID, p_template_id UUID, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE t dx_report_templates;
BEGIN
    SELECT * INTO t FROM dx_report_templates WHERE clinic_id = p_clinic_id AND id = p_template_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_template_not_found'); END IF;
    IF t.status <> 'draft' THEN PERFORM dx_raise('dx_template_published'); END IF;
    UPDATE dx_report_templates SET status = 'published', published_at = now(), published_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND id = p_template_id RETURNING * INTO t;
    RETURN to_jsonb(t);
END $$;

-- the template links promised by DX-M3 (visit override, B2B account letterhead)
ALTER TABLE dx_accessions DROP CONSTRAINT IF EXISTS dx_accessions_template_fk;
ALTER TABLE dx_accessions ADD CONSTRAINT dx_accessions_template_fk FOREIGN KEY (clinic_id, template_override_id)
    REFERENCES dx_report_templates (clinic_id, id);
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_accounts_template_fk;
ALTER TABLE dx_b2b_accounts ADD CONSTRAINT dx_b2b_accounts_template_fk FOREIGN KEY (clinic_id, template_id)
    REFERENCES dx_report_templates (clinic_id, id);

-- ── 2. Releases, their results, deliveries ─────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_report_releases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id        UUID NOT NULL,
    release_no          SMALLINT NOT NULL CHECK (release_no > 0),
    kind                TEXT NOT NULL CHECK (kind IN ('partial', 'final', 'amended')),
    template_snapshot   JSONB NOT NULL,
    letterhead_snapshot JSONB NOT NULL,
    routing_snapshot    JSONB NOT NULL,
    pdf_storage_path    TEXT NULL,
    pdf_sha256          TEXT NULL CHECK (pdf_sha256 ~ '^[0-9a-f]{64}$'),
    page_count          SMALLINT NULL CHECK (page_count > 0),
    verify_code         TEXT NOT NULL CHECK (verify_code ~ '^[A-Z2-7]{10}$'),
    status              TEXT NOT NULL DEFAULT 'rendering'
                        CHECK (status IN ('rendering', 'rendered', 'render_failed', 'superseded')),
    superseded_by       UUID NULL,
    created_by          UUID NULL,
    created_by_name     TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 1 AND 100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_report_releases_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_report_releases_no_key UNIQUE (clinic_id, accession_id, release_no),
    CONSTRAINT dx_report_releases_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_report_releases_superseded_fk FOREIGN KEY (clinic_id, superseded_by)
        REFERENCES dx_report_releases (clinic_id, id),
    CONSTRAINT dx_report_releases_rendered_shape CHECK (status NOT IN ('rendered')
        OR (pdf_storage_path IS NOT NULL AND pdf_sha256 IS NOT NULL AND page_count IS NOT NULL)),
    CONSTRAINT dx_report_releases_superseded_shape CHECK ((status = 'superseded') = (superseded_by IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_release_verify_code ON dx_report_releases (verify_code);
CREATE INDEX IF NOT EXISTS idx_dx_report_releases_visit ON dx_report_releases (clinic_id, accession_id, release_no);

CREATE TABLE IF NOT EXISTS dx_release_results (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    release_id UUID NOT NULL,
    result_id  UUID NOT NULL,
    order_id   UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_release_results_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_release_results_key UNIQUE (clinic_id, release_id, result_id),
    CONSTRAINT dx_release_results_release_fk FOREIGN KEY (clinic_id, release_id)
        REFERENCES dx_report_releases (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_release_results_result_fk FOREIGN KEY (clinic_id, result_id) REFERENCES lab_results (clinic_id, id),
    CONSTRAINT dx_release_results_order_fk FOREIGN KEY (clinic_id, order_id) REFERENCES opd_orders (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_release_results_result ON dx_release_results (clinic_id, result_id);
CREATE INDEX IF NOT EXISTS idx_dx_release_results_order ON dx_release_results (clinic_id, order_id);

CREATE TABLE IF NOT EXISTS dx_delivery_overrides (       -- append-only (DELIVERY-POLICY §6)
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id     UUID NOT NULL,
    rule             TEXT NOT NULL CHECK (rule = 'dues'),
    reason           TEXT NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 10 AND 300),
    approved_by      UUID NOT NULL,
    approved_by_name TEXT NOT NULL CHECK (char_length(btrim(approved_by_name)) BETWEEN 1 AND 100),
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_delivery_overrides_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_delivery_overrides_accession_fk FOREIGN KEY (clinic_id, accession_id)
        REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_delivery_overrides_expiry CHECK (expires_at > created_at AND expires_at <= created_at + interval '31 days')
);
CREATE INDEX IF NOT EXISTS idx_dx_delivery_overrides_visit ON dx_delivery_overrides (clinic_id, accession_id, expires_at);

CREATE TABLE IF NOT EXISTS dx_release_deliveries (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    release_id         UUID NOT NULL,
    accession_id       UUID NOT NULL,
    target             TEXT NOT NULL CHECK (target IN ('patient', 'referrer', 'account')),
    recipient_phone    TEXT NULL CHECK (char_length(recipient_phone) <= 20),   -- the registry's phone, as stored
    external_report_id TEXT NOT NULL CHECK (external_report_id ~ '^dx-[0-9a-f-]{36}-[pra]$'),
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'held_dues', 'held_shadow', 'blocked', 'queued', 'sending', 'sent',
                                         'failed', 'deferred')),
    reason             TEXT NULL CHECK (char_length(reason) <= 300),
    override_id        UUID NULL,
    lab_report_id      UUID NULL,          -- reference only: the pipeline's lab_reports row
    attempts           SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    decided_at         TIMESTAMPTZ NULL,
    sent_at            TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_release_deliveries_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_release_deliveries_target_key UNIQUE (clinic_id, release_id, target),
    CONSTRAINT dx_release_deliveries_external_key UNIQUE (clinic_id, external_report_id),
    CONSTRAINT dx_release_deliveries_release_fk FOREIGN KEY (clinic_id, release_id)
        REFERENCES dx_report_releases (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_release_deliveries_accession_fk FOREIGN KEY (clinic_id, accession_id)
        REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_release_deliveries_override_fk FOREIGN KEY (clinic_id, override_id)
        REFERENCES dx_delivery_overrides (clinic_id, id),
    -- AUDIT A3: one id per recipient, matching its target letter
    CONSTRAINT dx_release_deliveries_id_shape CHECK (
        external_report_id = 'dx-' || release_id::TEXT || '-' || left(target, 1)),
    CONSTRAINT dx_release_deliveries_sent_shape CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_dx_release_deliveries_status ON dx_release_deliveries (clinic_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_release_deliveries_visit ON dx_release_deliveries (clinic_id, accession_id);

CREATE TABLE IF NOT EXISTS dx_outbox (
    id           BIGSERIAL PRIMARY KEY,
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('reevaluate')),
    accession_id UUID NOT NULL,          -- reference only: the queue must never block a payment
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    processed_at TIMESTAMPTZ NULL,
    CONSTRAINT dx_outbox_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_outbox_pending ON dx_outbox (id) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS dx_print_log (               -- append-only
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    release_id      UUID NOT NULL,
    accession_id    UUID NOT NULL,
    printed_by      UUID NULL,
    printed_by_name TEXT NOT NULL CHECK (char_length(btrim(printed_by_name)) BETWEEN 1 AND 100),
    watermark       TEXT NULL CHECK (char_length(watermark) <= 60),
    override_id     UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_print_log_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_print_log_release_fk FOREIGN KEY (clinic_id, release_id) REFERENCES dx_report_releases (clinic_id, id),
    CONSTRAINT dx_print_log_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_print_log_release ON dx_print_log (clinic_id, release_id, created_at DESC);

CREATE TABLE IF NOT EXISTS dx_share_links (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    release_id   UUID NOT NULL,
    accession_id UUID NOT NULL,
    token_hash   TEXT NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),   -- SHA-256 of the 128-bit token
    expires_at   TIMESTAMPTZ NOT NULL,
    max_attempts SMALLINT NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    attempts     SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_by   UUID NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_share_links_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_share_links_token_key UNIQUE (clinic_id, token_hash),
    CONSTRAINT dx_share_links_release_fk FOREIGN KEY (clinic_id, release_id) REFERENCES dx_report_releases (clinic_id, id),
    CONSTRAINT dx_share_links_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_share_links_attempts_cap CHECK (attempts <= max_attempts)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_share_links_token ON dx_share_links (token_hash);

-- ── 3. Guards ──────────────────────────────────────────────────────────────
-- releases, their results, deliveries, overrides, print log and share links are
-- written only inside dx_* functions (127 guard); overrides and prints never change.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_report_releases', 'dx_release_results', 'dx_release_deliveries',
                             'dx_delivery_overrides', 'dx_print_log', 'dx_share_links'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_tx()',
                       'trg_' || t || '_guard', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['dx_delivery_overrides', 'dx_print_log', 'dx_release_results'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_append_only()',
                       'trg_' || t || '_append_only', t);
    END LOOP;
END $$;
DROP TRIGGER IF EXISTS trg_dx_release_deliveries_touch ON dx_release_deliveries;
CREATE TRIGGER trg_dx_release_deliveries_touch BEFORE UPDATE ON dx_release_deliveries
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- SECURITY §10.6: a release's content never changes; only its render state moves
-- (rendering -> rendered | render_failed, render_failed -> rendering, any -> superseded).
CREATE OR REPLACE FUNCTION dx_guard_release() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_state TEXT[] := ARRAY['status', 'pdf_storage_path', 'pdf_sha256', 'page_count', 'superseded_by', 'updated_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_append_only:dx_report_releases');
    END IF;
    IF (to_jsonb(NEW) - v_state) IS DISTINCT FROM (to_jsonb(OLD) - v_state) THEN
        PERFORM dx_raise('dx_release_immutable');
    END IF;
    IF OLD.status = 'superseded'
       OR (OLD.status = 'rendered' AND NEW.status NOT IN ('rendered', 'superseded'))
       OR (OLD.status = 'rendered' AND (NEW.pdf_storage_path, NEW.pdf_sha256, NEW.page_count)
                                       IS DISTINCT FROM (OLD.pdf_storage_path, OLD.pdf_sha256, OLD.page_count)) THEN
        PERFORM dx_raise('dx_release_immutable');
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_report_releases_immutable ON dx_report_releases;
CREATE TRIGGER trg_dx_report_releases_immutable BEFORE UPDATE OR DELETE ON dx_report_releases
    FOR EACH ROW EXECUTE FUNCTION dx_guard_release();

-- ── 4. The decision (DELIVERY-POLICY §3, evaluated top to bottom) ───────────
-- Dues (rule 7): an active item with a patient share not on a live bill, a live
-- bill not paid (zero-total issued counts as paid; a draft is unpaid), or doorstep
-- money not yet handed over.
CREATE OR REPLACE FUNCTION dx_visit_has_dues(p_clinic_id UUID, p_accession_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM dx_accession_items i
                    WHERE i.clinic_id = p_clinic_id AND i.accession_id = p_accession_id AND i.status = 'active'
                      AND i.patient_share_paise > 0
                      AND NOT EXISTS (SELECT 1 FROM opd_invoice_items l
                                        JOIN opd_invoices v ON v.clinic_id = l.clinic_id AND v.id = l.invoice_id
                                       WHERE l.clinic_id = i.clinic_id AND l.dx_accession_item_id = i.id
                                         AND v.status <> 'void'))
        OR EXISTS (SELECT 1 FROM opd_invoices v
                    WHERE v.clinic_id = p_clinic_id AND v.accession_id = p_accession_id AND v.status <> 'void'
                      AND NOT (v.status = 'paid' OR (v.status = 'issued' AND v.total_paise = 0)))
        OR EXISTS (SELECT 1 FROM dx_field_collections f
                    WHERE f.clinic_id = p_clinic_id AND f.accession_id = p_accession_id
                      AND f.status = 'pending_handover')
$$;

CREATE OR REPLACE FUNCTION dx_active_override(p_clinic_id UUID, p_accession_id UUID) RETURNS UUID
LANGUAGE sql STABLE AS $$
    SELECT id FROM dx_delivery_overrides
     WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id AND rule = 'dues' AND expires_at > now()
     ORDER BY created_at DESC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION dx_delivery_decision(p_clinic_id UUID, p_release_id UUID, p_target TEXT)
RETURNS TABLE (allowed BOOLEAN, reason TEXT) LANGUAGE plpgsql STABLE AS $$
DECLARE r dx_report_releases; a dx_accessions; acc dx_b2b_accounts; ref dx_referrers; v_owner TEXT;
        v_phone TEXT; v_dep_account UUID; v_patient_side BOOLEAN;
BEGIN
    IF p_target IS NULL OR p_target NOT IN ('patient', 'referrer', 'account', 'print_patient', 'share_link',
                                            'partner_portal') THEN
        PERFORM dx_raise('dx_target_invalid');
    END IF;
    v_patient_side := p_target IN ('patient', 'print_patient', 'share_link');
    -- 1. release exists here and is current
    SELECT * INTO r FROM dx_report_releases WHERE clinic_id = p_clinic_id AND id = p_release_id;
    IF NOT FOUND THEN RETURN QUERY SELECT false, 'release_not_found'::TEXT; RETURN; END IF;
    IF r.status = 'superseded' THEN RETURN QUERY SELECT false, 'release_superseded'::TEXT; RETURN; END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = r.accession_id;
    -- 2. who owns delivery for the registration branch (ADR-DX-15)
    v_owner := COALESCE((SELECT dx_settings -> 'delivery_owner' ->> a.branch_id::TEXT FROM clinics WHERE id = p_clinic_id),
                        'connector');
    IF v_owner NOT IN ('shadow', 'dx') THEN RETURN QUERY SELECT false, 'owner_connector'::TEXT; RETURN; END IF;
    IF v_owner = 'shadow' AND p_target <> 'print_patient' THEN
        RETURN QUERY SELECT false, 'held_shadow'::TEXT; RETURN;
    END IF;
    IF a.b2b_account_id IS NOT NULL THEN
        SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = a.b2b_account_id;
    END IF;
    -- 3. payer-routed accounts: never the patient (nor a referrer copy) without the recorded authorisation
    IF acc.kind IN ('insurance_tpa', 'corporate') AND acc.patient_copy_authorised_at IS NULL
       AND (v_patient_side OR p_target = 'referrer') THEN
        RETURN QUERY SELECT false, 'payer_routed'::TEXT; RETURN;
    END IF;
    -- 4. the account's delivery rule
    IF acc.id IS NULL THEN
        IF p_target IN ('account', 'partner_portal') THEN RETURN QUERY SELECT false, 'account_rule'::TEXT; RETURN; END IF;
    ELSIF (p_target = 'account' AND acc.report_delivery NOT IN ('account', 'both'))
       OR (v_patient_side AND acc.report_delivery NOT IN ('patient', 'both')) THEN
        RETURN QUERY SELECT false, 'account_rule'::TEXT; RETURN;
    END IF;
    -- 5. the patient's own phone (a B2B dependant or the account's desk number is not one)
    IF p_target = 'patient' THEN
        SELECT p.phone INTO v_phone FROM patients p WHERE p.clinic_id = p_clinic_id AND p.id = a.patient_id;
        IF a.family_member_id IS NOT NULL THEN
            SELECT f.dx_b2b_account_id INTO v_dep_account FROM family_members f
             WHERE f.clinic_id = p_clinic_id AND f.id = a.family_member_id;
        END IF;
        IF v_phone IS NULL OR v_dep_account IS NOT NULL
           OR v_phone IN (COALESCE(acc.desk_whatsapp, ''), COALESCE(acc.contact_phone, '')) THEN
            RETURN QUERY SELECT false, 'no_patient_phone'::TEXT; RETURN;
        END IF;
    END IF;
    -- 6. a referrer copy needs WhatsApp copies on, a phone and the patient's consent on the visit
    IF p_target = 'referrer' THEN
        SELECT * INTO ref FROM dx_referrers WHERE clinic_id = p_clinic_id AND id = a.referrer_id;
        IF ref.id IS NULL OR ref.report_copy <> 'whatsapp' OR ref.phone IS NULL
           OR NULLIF(a.delivery_prefs ->> 'consent_at', '') IS NULL THEN
            RETURN QUERY SELECT false, 'no_referrer_consent'::TEXT; RETURN;
        END IF;
    END IF;
    -- 7. NO PAYMENT, NO REPORT (8: an account copy of a B2B-billed visit is credit control, not dues)
    IF ((a.billing_mode IN ('patient', 'b2b_split') AND p_target <> 'account')
        OR (p_target = 'account' AND a.billing_mode = 'patient'))
       AND dx_visit_has_dues(p_clinic_id, a.id) AND dx_active_override(p_clinic_id, a.id) IS NULL THEN
        RETURN QUERY SELECT false, 'held_dues'::TEXT; RETURN;
    END IF;
    -- 9. plan capability is checked by the sender (Python plan table); 10. allowed
    RETURN QUERY SELECT true, NULL::TEXT;
END $$;

-- refusal reason -> delivery status
CREATE OR REPLACE FUNCTION dx_delivery_status_for(p_allowed BOOLEAN, p_reason TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_allowed THEN 'queued' WHEN p_reason IN ('held_dues', 'held_shadow') THEN p_reason
                ELSE 'blocked' END
$$;

-- (re)decide one open delivery; returns its new status
CREATE OR REPLACE FUNCTION dx_decide_delivery(p_clinic_id UUID, d dx_release_deliveries) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE v_ok BOOLEAN; v_reason TEXT; v_status TEXT; v_override UUID;
BEGIN
    SELECT x.allowed, x.reason INTO v_ok, v_reason FROM dx_delivery_decision(p_clinic_id, d.release_id, d.target) x;
    v_status := dx_delivery_status_for(v_ok, v_reason);
    IF v_ok AND dx_visit_has_dues(p_clinic_id, d.accession_id) THEN
        v_override := dx_active_override(p_clinic_id, d.accession_id);    -- released on override (audit trail)
    END IF;
    IF (v_status, v_reason, v_override) IS DISTINCT FROM (d.status, d.reason, d.override_id) THEN
        UPDATE dx_release_deliveries SET status = v_status, reason = v_reason, override_id = v_override,
                                         decided_at = now()
         WHERE clinic_id = p_clinic_id AND id = d.id;
    END IF;
    RETURN v_status;
END $$;

-- ── 5. Create a release (W10 step 1, W11) ──────────────────────────────────
-- p_result_ids NULL = every current signed result of the visit. p_kind NULL = derived:
-- amended (a result replaces one already released), final (every active test signed
-- and included), else partial. A given p_kind must match the derived one.
CREATE OR REPLACE FUNCTION dx_create_release(p_clinic_id UUID, p_accession_id UUID, p_result_ids UUID[], p_kind TEXT,
                                             p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; acc dx_b2b_accounts; ref dx_referrers; v_ids UUID[]; v_kind TEXT; v_no SMALLINT;
        v_tpl dx_report_templates; v_src TEXT; v_code TEXT; v_bytes BYTEA; v_rid UUID; v_site dx_sites;
        v_letter JSONB; v_route JSONB; v_owner TEXT; v_phone TEXT; d dx_release_deliveries; v_out JSONB := '[]'::jsonb;
        v_rel dx_report_releases; v_tgt TEXT; v_payer BOOLEAN; i INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    IF a.status = 'cancelled' THEN PERFORM dx_raise('dx_visit_cancelled'); END IF;
    IF p_kind IS NOT NULL AND p_kind NOT IN ('partial', 'final', 'amended') THEN PERFORM dx_raise('dx_release_kind_invalid'); END IF;

    -- the results: current signed versions of this visit's live tests
    SELECT array_agg(r.id ORDER BY r.created_at, r.id) INTO v_ids
      FROM lab_results r JOIN opd_orders o ON o.clinic_id = r.clinic_id AND o.id = r.order_id
     WHERE r.clinic_id = p_clinic_id AND o.accession_id = a.id AND o.status <> 'cancelled' AND r.status = 'validated'
       AND (p_result_ids IS NULL OR r.id = ANY (p_result_ids));
    IF v_ids IS NULL THEN PERFORM dx_raise('dx_release_empty'); END IF;
    IF p_result_ids IS NOT NULL AND cardinality(v_ids) <> (SELECT count(DISTINCT x) FROM unnest(p_result_ids) x) THEN
        PERFORM dx_raise('dx_release_result_invalid');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM unnest(v_ids) x
                    WHERE NOT EXISTS (SELECT 1 FROM dx_release_results rr
                                        JOIN dx_report_releases rl ON rl.clinic_id = rr.clinic_id AND rl.id = rr.release_id
                                       WHERE rr.clinic_id = p_clinic_id AND rr.result_id = x
                                         AND rl.status <> 'superseded')) THEN
        PERFORM dx_raise('dx_release_nothing_new');
    END IF;
    v_kind := CASE
        WHEN EXISTS (SELECT 1 FROM unnest(v_ids) x JOIN lab_results r ON r.clinic_id = p_clinic_id AND r.id = x
                       JOIN dx_release_results rr ON rr.clinic_id = p_clinic_id AND rr.order_id = r.order_id
                                                 AND rr.result_id <> r.id
                       JOIN dx_report_releases rl ON rl.clinic_id = rr.clinic_id AND rl.id = rr.release_id
                      WHERE rl.status <> 'superseded') THEN 'amended'
        WHEN NOT EXISTS (SELECT 1 FROM opd_orders o
                          WHERE o.clinic_id = p_clinic_id AND o.accession_id = a.id AND o.status <> 'cancelled'
                            AND NOT EXISTS (SELECT 1 FROM lab_results r WHERE r.clinic_id = p_clinic_id
                                              AND r.order_id = o.id AND r.id = ANY (v_ids))) THEN 'final'
        ELSE 'partial' END;
    IF p_kind IS NOT NULL AND p_kind <> v_kind THEN PERFORM dx_raise('dx_release_kind_mismatch:' || v_kind); END IF;

    -- template cascade (ADR-DX-08): visit override -> white-label account -> branch -> clinic -> built-in
    IF a.b2b_account_id IS NOT NULL THEN
        SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = a.b2b_account_id;
    END IF;
    SELECT * INTO v_tpl FROM dx_report_templates
     WHERE clinic_id = p_clinic_id AND id = a.template_override_id AND status = 'published';
    v_src := CASE WHEN v_tpl.id IS NOT NULL THEN 'visit' END;
    IF v_tpl.id IS NULL AND acc.white_label THEN
        SELECT * INTO v_tpl FROM dx_report_templates
         WHERE clinic_id = p_clinic_id AND status = 'published'
           AND (id = acc.template_id OR (acc.template_id IS NULL AND owner_kind = 'b2b_account' AND owner_id = acc.id))
         ORDER BY (id = acc.template_id) DESC NULLS LAST, published_at DESC LIMIT 1;
        v_src := CASE WHEN v_tpl.id IS NOT NULL THEN 'account' END;
    END IF;
    IF v_tpl.id IS NULL THEN
        SELECT * INTO v_tpl FROM dx_report_templates
         WHERE clinic_id = p_clinic_id AND status = 'published' AND owner_kind = 'branch' AND owner_id = a.branch_id
         ORDER BY published_at DESC LIMIT 1;
        v_src := CASE WHEN v_tpl.id IS NOT NULL THEN 'branch' END;
    END IF;
    IF v_tpl.id IS NULL THEN
        SELECT * INTO v_tpl FROM dx_report_templates
         WHERE clinic_id = p_clinic_id AND status = 'published' AND owner_kind = 'clinic'
         ORDER BY published_at DESC LIMIT 1;
        v_src := CASE WHEN v_tpl.id IS NOT NULL THEN 'clinic' ELSE 'builtin' END;
    END IF;

    -- letterhead: the processing site of the first tube (else the registration site), the clinic, partners
    SELECT st.* INTO v_site FROM unnest(v_ids) x
      JOIN lab_results r ON r.clinic_id = p_clinic_id AND r.id = x
      JOIN opd_orders o ON o.clinic_id = p_clinic_id AND o.id = r.order_id
      JOIN dx_samples s ON s.clinic_id = p_clinic_id AND s.id = o.dx_sample_id
      JOIN dx_sites st ON st.clinic_id = p_clinic_id AND st.id = COALESCE(s.received_site_id, s.processing_site_id)
     LIMIT 1;
    IF v_site.id IS NULL THEN
        SELECT * INTO v_site FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = a.branch_id;
    END IF;
    SELECT jsonb_build_object(
        'clinic', jsonb_build_object('name', c.name, 'address', to_jsonb(c) ->> 'address', 'phone', to_jsonb(c) ->> 'phone'),
        'site', jsonb_build_object('id', v_site.id, 'display_name', v_site.display_name, 'legal_name', v_site.legal_name,
                                   'registration_no', v_site.registration_no,
                                   'nabl_certificate_no', v_site.nabl_certificate_no,
                                   'address', b.address, 'phone', b.phone),
        'partners', COALESCE((SELECT jsonb_object_agg(p.id::TEXT, jsonb_build_object(
                        'name', p.name, 'legal_name', p.legal_name, 'address', p.address,
                        'registration_no', p.registration_no, 'nabl_certificate_no', p.nabl_certificate_no))
                       FROM dx_outsource_partners p
                      WHERE p.clinic_id = p_clinic_id AND p.id IN (
                            SELECT o.outsource_partner_id FROM unnest(v_ids) x
                              JOIN lab_results r ON r.clinic_id = p_clinic_id AND r.id = x
                              JOIN opd_orders o ON o.clinic_id = p_clinic_id AND o.id = r.order_id)), '{}'::jsonb))
      INTO v_letter
      FROM clinics c LEFT JOIN branches b ON b.clinic_id = c.id AND b.id = v_site.branch_id
     WHERE c.id = p_clinic_id;

    v_owner := COALESCE((SELECT dx_settings -> 'delivery_owner' ->> a.branch_id::TEXT FROM clinics WHERE id = p_clinic_id),
                        'connector');
    IF a.referrer_id IS NOT NULL THEN
        SELECT * INTO ref FROM dx_referrers WHERE clinic_id = p_clinic_id AND id = a.referrer_id;
    END IF;
    v_payer := acc.kind IN ('insurance_tpa', 'corporate') AND acc.patient_copy_authorised_at IS NULL;
    v_route := jsonb_build_object(
        'billing_mode', a.billing_mode, 'delivery_owner', v_owner, 'branch_id', a.branch_id,
        'white_label', COALESCE(acc.white_label, false) OR v_src = 'account',
        'account', CASE WHEN acc.id IS NULL THEN NULL ELSE jsonb_build_object(
            'id', acc.id, 'name', acc.name, 'kind', acc.kind, 'report_delivery', acc.report_delivery,
            'patient_copy_authorised', acc.patient_copy_authorised_at IS NOT NULL) END,
        'referrer', CASE WHEN ref.id IS NULL THEN NULL ELSE jsonb_build_object(
            'id', ref.id, 'name', ref.name, 'report_copy', ref.report_copy) END);

    -- release number and a public verify code (50 bits from the CSPRNG behind gen_random_uuid)
    v_no := COALESCE((SELECT max(release_no) FROM dx_report_releases
                       WHERE clinic_id = p_clinic_id AND accession_id = a.id), 0) + 1;
    LOOP
        v_bytes := uuid_send(gen_random_uuid());
        v_code := '';
        FOREACH i IN ARRAY ARRAY[0, 1, 2, 3, 4, 5, 9, 10, 11, 12] LOOP     -- bytes 6 and 8 carry version / variant bits
            v_code := v_code || substr('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567', (get_byte(v_bytes, i) & 31) + 1, 1);
        END LOOP;
        EXIT WHEN NOT EXISTS (SELECT 1 FROM dx_report_releases WHERE verify_code = v_code);
    END LOOP;

    INSERT INTO dx_report_releases (clinic_id, accession_id, release_no, kind, template_snapshot, letterhead_snapshot,
                                    routing_snapshot, verify_code, created_by, created_by_name)
    VALUES (p_clinic_id, a.id, v_no, v_kind,
            jsonb_build_object('source', v_src, 'template_id', v_tpl.id, 'name', v_tpl.name, 'version', v_tpl.version,
                               'owner_kind', v_tpl.owner_kind, 'layout', v_tpl.layout),
            v_letter, v_route, v_code, p_actor_id, left(COALESCE(NULLIF(btrim(p_actor_name), ''), 'system'), 100))
    RETURNING * INTO v_rel;
    INSERT INTO dx_release_results (clinic_id, release_id, result_id, order_id)
    SELECT p_clinic_id, v_rel.id, r.id, r.order_id FROM lab_results r WHERE r.clinic_id = p_clinic_id AND r.id = ANY (v_ids);

    -- an earlier current release sharing a test is replaced (W11); its open deliveries are refused from now on
    UPDATE dx_report_releases rl SET status = 'superseded', superseded_by = v_rel.id
     WHERE rl.clinic_id = p_clinic_id AND rl.accession_id = a.id AND rl.id <> v_rel.id AND rl.status <> 'superseded'
       AND EXISTS (SELECT 1 FROM dx_release_results x JOIN dx_release_results y
                                 ON y.clinic_id = x.clinic_id AND y.order_id = x.order_id AND y.release_id = v_rel.id
                    WHERE x.clinic_id = p_clinic_id AND x.release_id = rl.id);
    FOR d IN SELECT dd.* FROM dx_release_deliveries dd JOIN dx_report_releases rl
                          ON rl.clinic_id = dd.clinic_id AND rl.id = dd.release_id
              WHERE dd.clinic_id = p_clinic_id AND rl.superseded_by = v_rel.id
                AND dd.status IN ('pending', 'held_dues', 'held_shadow', 'queued', 'failed', 'deferred', 'blocked') LOOP
        PERFORM dx_decide_delivery(p_clinic_id, d);
    END LOOP;

    -- one delivery per target (AUDIT A3). A payer-routed visit never gets a patient row (SECURITY §10.10).
    FOREACH v_tgt IN ARRAY ARRAY['patient', 'referrer', 'account'] LOOP
        CONTINUE WHEN (a.delivery_prefs ->> v_tgt) = 'false';
        CONTINUE WHEN v_tgt = 'patient' AND v_payer;
        CONTINUE WHEN v_tgt = 'referrer' AND ref.id IS NULL;
        CONTINUE WHEN v_tgt = 'account' AND acc.id IS NULL;
        v_phone := CASE v_tgt
            WHEN 'patient' THEN (SELECT phone FROM patients WHERE clinic_id = p_clinic_id AND id = a.patient_id)
            WHEN 'referrer' THEN ref.phone
            ELSE COALESCE(acc.desk_whatsapp, acc.contact_phone) END;
        INSERT INTO dx_release_deliveries (clinic_id, release_id, accession_id, target, recipient_phone, external_report_id)
        VALUES (p_clinic_id, v_rel.id, a.id, v_tgt, v_phone, 'dx-' || v_rel.id::TEXT || '-' || left(v_tgt, 1))
        RETURNING * INTO d;
        PERFORM dx_decide_delivery(p_clinic_id, d);
    END LOOP;

    UPDATE dx_accessions SET status = CASE WHEN v_kind = 'partial' THEN 'partially_reported' ELSE 'reported' END
     WHERE clinic_id = p_clinic_id AND id = a.id AND status IN ('registered', 'in_process', 'partially_reported', 'reported');
    PERFORM dx_log(p_clinic_id, a.id, 'released', p_actor_id, p_actor_name,
                   jsonb_build_object('release_id', v_rel.id, 'release_no', v_no, 'kind', v_kind,
                                      'results', cardinality(v_ids)));
    SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY x.target), '[]'::jsonb) INTO v_out
      FROM dx_release_deliveries x WHERE x.clinic_id = p_clinic_id AND x.release_id = v_rel.id;
    RETURN to_jsonb(v_rel) || jsonb_build_object('result_ids', to_jsonb(v_ids), 'deliveries', v_out);
END $$;

-- render outcome (the service renders from the snapshots and stores the PDF)
CREATE OR REPLACE FUNCTION dx_set_release_render(p_clinic_id UUID, p_release_id UUID, p_ok BOOLEAN, p_path TEXT,
                                                 p_sha256 TEXT, p_pages INTEGER) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE r dx_report_releases;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO r FROM dx_report_releases WHERE clinic_id = p_clinic_id AND id = p_release_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_release_not_found'); END IF;
    IF r.status = 'rendered' THEN RETURN to_jsonb(r); END IF;             -- a retry after success replays
    IF r.status = 'superseded' THEN RETURN to_jsonb(r); END IF;           -- replaced while it was rendering
    UPDATE dx_report_releases SET status = CASE WHEN p_ok THEN 'rendered' ELSE 'render_failed' END,
                                  pdf_storage_path = CASE WHEN p_ok THEN p_path END,
                                  pdf_sha256 = CASE WHEN p_ok THEN p_sha256 END,
                                  page_count = CASE WHEN p_ok THEN p_pages END
     WHERE clinic_id = p_clinic_id AND id = p_release_id RETURNING * INTO r;
    RETURN to_jsonb(r);
END $$;

-- ── 6. Sending: claim (CAS), outcome, re-evaluation, manual resend ──────────
-- DELIVERY-POLICY §4: queued -> sending only if the decision still allows it (else the
-- row moves to its hold / block state). The release must be rendered first.
CREATE OR REPLACE FUNCTION dx_claim_delivery(p_clinic_id UUID, p_delivery_id UUID) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE d dx_release_deliveries; v_status TEXT;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO d FROM dx_release_deliveries WHERE clinic_id = p_clinic_id AND id = p_delivery_id FOR UPDATE;
    IF NOT FOUND OR d.status <> 'queued' THEN RETURN false; END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_report_releases WHERE clinic_id = p_clinic_id AND id = d.release_id
                                                      AND status = 'rendered') THEN
        v_status := dx_decide_delivery(p_clinic_id, d);       -- a superseded release blocks; rendering waits
        RETURN false;
    END IF;
    v_status := dx_decide_delivery(p_clinic_id, d);
    IF v_status <> 'queued' THEN RETURN false; END IF;
    UPDATE dx_release_deliveries SET status = 'sending', attempts = attempts + 1, decided_at = now()
     WHERE clinic_id = p_clinic_id AND id = p_delivery_id AND status = 'queued';
    RETURN FOUND;
END $$;

-- p_error: NULL on success; 'deferred:<why>' (pipeline policy hold), 'blocked:<why>' (e.g. plan), else a failure.
CREATE OR REPLACE FUNCTION dx_mark_delivery_result(p_clinic_id UUID, p_delivery_id UUID, p_success BOOLEAN,
                                                   p_lab_report_id UUID, p_error TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE d dx_release_deliveries;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO d FROM dx_release_deliveries WHERE clinic_id = p_clinic_id AND id = p_delivery_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_delivery_not_found'); END IF;
    IF d.status <> 'sending' THEN PERFORM dx_raise('dx_delivery_not_sending'); END IF;
    UPDATE dx_release_deliveries
       SET status = CASE WHEN p_success THEN 'sent' WHEN p_error LIKE 'deferred:%' THEN 'deferred'
                         WHEN p_error LIKE 'blocked:%' THEN 'blocked' ELSE 'failed' END,
           reason = CASE WHEN p_success THEN NULL
                         WHEN p_error LIKE 'blocked:%' THEN left(substr(p_error, 9), 300)
                         ELSE left(COALESCE(p_error, 'send_failed'), 300) END,
           sent_at = CASE WHEN p_success THEN now() END,
           lab_report_id = COALESCE(p_lab_report_id, lab_report_id)
     WHERE clinic_id = p_clinic_id AND id = p_delivery_id RETURNING * INTO d;
    PERFORM dx_log(p_clinic_id, d.accession_id, 'delivery_' || d.status, NULL, 'system',
                   jsonb_build_object('delivery_id', d.id, 'target', d.target, 'reason', d.reason));
    RETURN to_jsonb(d);
END $$;

-- After money moves (outbox) or an override: re-decide the visit's open deliveries.
-- Sent / sending / failed / deferred rows belong to the pipeline and are left alone.
-- Returns the ids that may be sent now (queued, release rendered).
CREATE OR REPLACE FUNCTION dx_reevaluate_deliveries(p_clinic_id UUID, p_accession_id UUID) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE d dx_release_deliveries;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    FOR d IN SELECT * FROM dx_release_deliveries
              WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id
                AND status IN ('pending', 'held_dues', 'queued') ORDER BY created_at, id FOR UPDATE LOOP
        PERFORM dx_decide_delivery(p_clinic_id, d);
    END LOOP;
    RETURN COALESCE((SELECT jsonb_agg(dd.id ORDER BY dd.created_at, dd.id) FROM dx_release_deliveries dd
                       JOIN dx_report_releases rl ON rl.clinic_id = dd.clinic_id AND rl.id = dd.release_id
                      WHERE dd.clinic_id = p_clinic_id AND dd.accession_id = p_accession_id AND dd.status = 'queued'
                        AND rl.status = 'rendered'), '[]'::jsonb);
END $$;

-- Manual resend / "send now" (Reports & Dispatch): re-checks the table. A failed row whose
-- report already reached the pipeline stays with the pipeline's retry queue.
CREATE OR REPLACE FUNCTION dx_requeue_delivery(p_clinic_id UUID, p_delivery_id UUID) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE d dx_release_deliveries; v_status TEXT;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO d FROM dx_release_deliveries WHERE clinic_id = p_clinic_id AND id = p_delivery_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_delivery_not_found'); END IF;
    IF d.status IN ('sent', 'sending') THEN PERFORM dx_raise('dx_delivery_already_sent'); END IF;
    v_status := dx_decide_delivery(p_clinic_id, d);
    SELECT * INTO d FROM dx_release_deliveries WHERE clinic_id = p_clinic_id AND id = p_delivery_id;
    IF v_status = 'queued' AND d.lab_report_id IS NOT NULL THEN
        UPDATE dx_release_deliveries SET status = 'failed', reason = 'pipeline_retry'
         WHERE clinic_id = p_clinic_id AND id = d.id RETURNING * INTO d;
        RETURN to_jsonb(d) || jsonb_build_object('pipeline_owned', true);
    END IF;
    RETURN to_jsonb(d) || jsonb_build_object('pipeline_owned', false);
END $$;

-- ── 7. Overrides, desk print, share links ──────────────────────────────────
CREATE OR REPLACE FUNCTION dx_create_delivery_override(p_clinic_id UUID, p_accession_id UUID, p_reason TEXT,
                                                       p_expires_at TIMESTAMPTZ, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o dx_delivery_overrides; v_ids JSONB;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF p_actor_id IS NULL THEN PERFORM dx_raise('dx_actor_required'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 10 THEN PERFORM dx_raise('dx_override_reason'); END IF;
    PERFORM 1 FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    INSERT INTO dx_delivery_overrides (clinic_id, accession_id, rule, reason, approved_by, approved_by_name, expires_at)
    VALUES (p_clinic_id, p_accession_id, 'dues', btrim(p_reason), p_actor_id, left(p_actor_name, 100),
            COALESCE(p_expires_at, clock_timestamp() + interval '7 days'))
    RETURNING * INTO o;
    PERFORM dx_log(p_clinic_id, p_accession_id, 'delivery_override', p_actor_id, p_actor_name,
                   jsonb_build_object('override_id', o.id, 'reason', o.reason, 'expires_at', o.expires_at));
    v_ids := dx_reevaluate_deliveries(p_clinic_id, p_accession_id);
    RETURN to_jsonb(o) || jsonb_build_object('sendable', v_ids);
END $$;

-- Desk print for the patient: decision 'print_patient'; logged only when allowed.
CREATE OR REPLACE FUNCTION dx_log_print(p_clinic_id UUID, p_release_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE r dx_report_releases; v_ok BOOLEAN; v_reason TEXT; v_owner TEXT; v_mark TEXT; v_override UUID; p dx_print_log;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT x.allowed, x.reason INTO v_ok, v_reason FROM dx_delivery_decision(p_clinic_id, p_release_id, 'print_patient') x;
    IF NOT v_ok THEN RETURN jsonb_build_object('allowed', false, 'reason', v_reason); END IF;
    SELECT * INTO r FROM dx_report_releases WHERE clinic_id = p_clinic_id AND id = p_release_id;
    v_owner := (SELECT dx_settings -> 'delivery_owner' ->> (SELECT branch_id::TEXT FROM dx_accessions
                                                          WHERE clinic_id = p_clinic_id AND id = r.accession_id)
                  FROM clinics WHERE id = p_clinic_id);
    v_mark := CASE WHEN v_owner = 'shadow' THEN 'SHADOW — NOT FOR PATIENT USE' END;
    IF dx_visit_has_dues(p_clinic_id, r.accession_id) THEN v_override := dx_active_override(p_clinic_id, r.accession_id); END IF;
    INSERT INTO dx_print_log (clinic_id, release_id, accession_id, printed_by, printed_by_name, watermark, override_id)
    VALUES (p_clinic_id, r.id, r.accession_id, p_actor_id, left(COALESCE(p_actor_name, 'staff'), 100), v_mark, v_override)
    RETURNING * INTO p;
    PERFORM dx_log(p_clinic_id, r.accession_id, 'printed', p_actor_id, p_actor_name,
                   jsonb_build_object('release_id', r.id, 'watermark', v_mark, 'override_id', v_override));
    RETURN jsonb_build_object('allowed', true, 'watermark', v_mark, 'override_id', v_override, 'print_id', p.id);
END $$;

CREATE OR REPLACE FUNCTION dx_create_share_link(p_clinic_id UUID, p_release_id UUID, p_token_hash TEXT,
                                                p_ttl_hours INTEGER, p_actor_id UUID) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE v_ok BOOLEAN; v_reason TEXT; l dx_share_links;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT x.allowed, x.reason INTO v_ok, v_reason FROM dx_delivery_decision(p_clinic_id, p_release_id, 'share_link') x;
    IF NOT v_ok THEN RETURN jsonb_build_object('allowed', false, 'reason', v_reason); END IF;
    INSERT INTO dx_share_links (clinic_id, release_id, accession_id, token_hash, expires_at, created_by)
    SELECT p_clinic_id, r.id, r.accession_id, p_token_hash,
           clock_timestamp() + make_interval(hours => LEAST(GREATEST(COALESCE(p_ttl_hours, 72), 1), 720)), p_actor_id
      FROM dx_report_releases r WHERE r.clinic_id = p_clinic_id AND r.id = p_release_id
    RETURNING * INTO l;
    PERFORM dx_log(p_clinic_id, l.accession_id, 'share_link_created', p_actor_id, NULL,
                   jsonb_build_object('release_id', l.release_id, 'link_id', l.id, 'expires_at', l.expires_at));
    RETURN jsonb_build_object('allowed', true, 'id', l.id, 'expires_at', l.expires_at);
END $$;

-- Public unlock (SECURITY §6): one attempt is consumed BEFORE the caller compares the
-- digits, so parallel guesses cannot exceed max_attempts. Every attempt is on the timeline.
CREATE OR REPLACE FUNCTION dx_share_take_attempt(p_token_hash TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE l dx_share_links; v_phone TEXT;
BEGIN
    SELECT * INTO l FROM dx_share_links WHERE token_hash = p_token_hash FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('state', 'not_found'); END IF;
    IF l.expires_at <= now() THEN RETURN jsonb_build_object('state', 'expired'); END IF;
    IF l.attempts >= l.max_attempts THEN RETURN jsonb_build_object('state', 'locked'); END IF;
    PERFORM set_config('kriya.dx_tx', l.clinic_id::TEXT, true);
    UPDATE dx_share_links SET attempts = attempts + 1 WHERE clinic_id = l.clinic_id AND id = l.id RETURNING * INTO l;
    SELECT p.phone INTO v_phone FROM dx_accessions a JOIN patients p ON p.clinic_id = a.clinic_id AND p.id = a.patient_id
     WHERE a.clinic_id = l.clinic_id AND a.id = l.accession_id;
    PERFORM dx_log(l.clinic_id, l.accession_id, 'share_unlock_attempt', NULL, 'public',
                   jsonb_build_object('link_id', l.id, 'attempt', l.attempts));
    RETURN jsonb_build_object('state', 'ok', 'clinic_id', l.clinic_id, 'link_id', l.id, 'release_id', l.release_id,
                              'accession_id', l.accession_id, 'phone', v_phone,
                              'attempts_left', l.max_attempts - l.attempts);
END $$;

-- ── 8. Re-evaluation outbox (no polling of money paths) ─────────────────────
CREATE OR REPLACE FUNCTION dx_outbox_from_receipt() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO dx_outbox (clinic_id, kind, accession_id)
    SELECT NEW.clinic_id, 'reevaluate', v.accession_id FROM opd_invoices v
     WHERE v.clinic_id = NEW.clinic_id AND v.id = NEW.invoice_id AND v.accession_id IS NOT NULL;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_outbox_receipt ON opd_receipts;
CREATE TRIGGER trg_dx_outbox_receipt AFTER INSERT ON opd_receipts
    FOR EACH ROW WHEN (NEW.invoice_id IS NOT NULL) EXECUTE FUNCTION dx_outbox_from_receipt();

CREATE OR REPLACE FUNCTION dx_outbox_from_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO dx_outbox (clinic_id, kind, accession_id) VALUES (NEW.clinic_id, 'reevaluate', NEW.accession_id);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_outbox_invoice ON opd_invoices;
CREATE TRIGGER trg_dx_outbox_invoice AFTER UPDATE OF status ON opd_invoices
    FOR EACH ROW WHEN (NEW.accession_id IS NOT NULL AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION dx_outbox_from_invoice();

-- dx_outbox_dispatch (scheduler, all clinics): take a batch, distinct visits
CREATE OR REPLACE FUNCTION dx_outbox_claim(p_limit INTEGER) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB;
BEGIN
    WITH c AS (SELECT id FROM dx_outbox WHERE processed_at IS NULL ORDER BY id
                LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 1000) FOR UPDATE SKIP LOCKED),
         u AS (UPDATE dx_outbox o SET processed_at = clock_timestamp() FROM c WHERE o.id = c.id
               RETURNING o.clinic_id, o.accession_id)
    SELECT COALESCE(jsonb_agg(DISTINCT jsonb_build_object('clinic_id', clinic_id, 'accession_id', accession_id)),
                    '[]'::jsonb) INTO v FROM u;
    RETURN v;
END $$;

-- ── 9. P6b: OPD / IPD lab orders on the DX bench (W15, DX-41) ───────────────
-- The order keeps its source, encounter / admission and its OPD bill or IPD charge;
-- it gains a DX visit (billing_mode 'internal', every share 0), an item and a tube.
-- opd_guard_order (latest: 121) + two DX exemptions, each behind its own setting.
CREATE OR REPLACE FUNCTION opd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['status', 'updated_at', 'cancellation_reason', 'cancelled_at', 'cancelled_by',
                              'specimen_barcode', 'sample_collected_at', 'sample_collected_by',
                              'sample_collected_by_name', 'resulted_at', 'resulted_by', 'resulted_by_name',
                              'result_summary'];
    v_dx TEXT[] := ARRAY['accession_id', 'accession_item_id', 'dx_sample_id', 'updated_at'];
    v_ok BOOLEAN;
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    -- 131: the DX purge unlinks an adopted order from its visit, nothing else
    IF TG_OP = 'UPDATE' AND OLD.source <> 'dx' AND dx_purging(OLD.clinic_id)
       AND NEW.accession_id IS NULL AND NEW.accession_item_id IS NULL AND NEW.dx_sample_id IS NULL
       AND (to_jsonb(NEW) - v_dx) = (to_jsonb(OLD) - v_dx) THEN
        RETURN NEW;
    END IF;
    -- 131: dx_adopt_orders links an open lab order to its internal DX visit, once
    IF TG_OP = 'UPDATE' AND OLD.accession_id IS NULL AND OLD.source <> 'dx'
       AND current_setting('kriya.dx_adopt', true) = OLD.clinic_id::TEXT THEN
        v_mutable := v_mutable || ARRAY['accession_id', 'accession_item_id', 'dx_sample_id'];
    END IF;
    IF TG_OP = 'DELETE' THEN
        -- only the owner's clinic purge (cascading from encounters / IPD orders)
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'opd_append_only:opd_orders' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('completed', 'cancelled') THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.sample_collected_at IS NOT NULL
       AND (NEW.specimen_barcode, NEW.sample_collected_at, NEW.sample_collected_by, NEW.sample_collected_by_name)
           IS DISTINCT FROM
           (OLD.specimen_barcode, OLD.sample_collected_at, OLD.sample_collected_by, OLD.sample_collected_by_name) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.resulted_at IS NOT NULL
       AND (NEW.resulted_at, NEW.resulted_by, NEW.resulted_by_name, NEW.result_summary)
           IS DISTINCT FROM (OLD.resulted_at, OLD.resulted_by, OLD.resulted_by_name, OLD.result_summary) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    v_ok := CASE OLD.status
        WHEN 'ordered'          THEN NEW.status IN ('billed', 'sample_collected', 'completed', 'cancelled')
        WHEN 'billed'           THEN NEW.status IN ('ordered', 'sample_collected', 'completed', 'cancelled')
        WHEN 'sample_collected' THEN NEW.status IN ('in_progress', 'resulted', 'completed', 'cancelled')
        WHEN 'in_progress'      THEN NEW.status IN ('resulted', 'completed', 'cancelled')
        WHEN 'resulted'         THEN NEW.status = 'completed'
        ELSE false END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'opd_order_bad_transition:%->%', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    -- billing state follows the bill (opd_invoice_orders_billing), never a direct write
    IF NEW.status IN ('ordered', 'billed') AND OLD.status IN ('ordered', 'billed')
       AND current_setting('kriya.opd_order_billing', true) IS DISTINCT FROM OLD.clinic_id::TEXT THEN
        RAISE EXCEPTION 'opd_order_billing_is_derived' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'completed' AND OLD.status IN ('ordered', 'billed') AND OLD.sample_type IS NOT NULL THEN
        RAISE EXCEPTION 'opd_order_sample_required' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- An internal (adopted) visit's lines are written only by dx_adopt_orders: the money is
-- on the OPD bill / IPD charges, so DX never adds, prices or cancels lines there.
CREATE OR REPLACE FUNCTION dx_guard_internal_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('kriya.dx_adopt', true) = NEW.clinic_id::TEXT THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM dx_accessions WHERE clinic_id = NEW.clinic_id AND id = NEW.accession_id
                                             AND billing_mode = 'internal' AND source IN ('opd', 'ipd')) THEN
        PERFORM dx_raise('dx_internal_visit');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_internal_items ON dx_accession_items;
CREATE TRIGGER trg_dx_internal_items BEFORE INSERT OR UPDATE ON dx_accession_items
    FOR EACH ROW EXECUTE FUNCTION dx_guard_internal_items();

CREATE OR REPLACE FUNCTION dx_adopt_orders(p_clinic_id UUID, p_order_ids UUID[], p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o opd_orders; t lab_tests; g RECORD; v_patient patients; v_member family_members; v_snapshot JSONB;
        v_branch UUID; v_year INTEGER; v_seq INTEGER; v_sseq INTEGER; v_id UUID; v_item UUID; v_sample UUID;
        v_collect UUID; v_process UUID; v_cont dx_containers; v_tseq INTEGER; v_doctor TEXT;
        v_visits JSONB := '[]'::jsonb; v_skipped JSONB := '[]'::jsonb; v_n INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF (SELECT dx_state FROM clinics WHERE id = p_clinic_id) IS DISTINCT FROM 'ACTIVE' THEN
        PERFORM dx_raise('dx_not_active');
    END IF;
    IF p_order_ids IS NULL OR cardinality(p_order_ids) NOT BETWEEN 1 AND 100 THEN
        PERFORM dx_raise('dx_adopt_orders_invalid');
    END IF;
    IF (SELECT count(*) FROM opd_orders WHERE clinic_id = p_clinic_id AND id = ANY (p_order_ids))
       <> (SELECT count(DISTINCT x) FROM unnest(p_order_ids) x) THEN
        PERFORM dx_raise('lis_order_not_found');
    END IF;
    -- lock in one order (patient, then id) so two desks adopting overlapping sets serialise
    FOR o IN SELECT * FROM opd_orders WHERE clinic_id = p_clinic_id AND id = ANY (p_order_ids)
              ORDER BY patient_id, id FOR UPDATE LOOP
        IF o.source NOT IN ('opd', 'ipd') THEN PERFORM dx_raise('dx_order_not_adoptable:' || left(o.item_name, 60)); END IF;
        IF o.accession_id IS NOT NULL THEN CONTINUE; END IF;      -- already on the bench (replay)
        SELECT * INTO t FROM lab_tests WHERE clinic_id = p_clinic_id AND id = o.catalog_reference_id;
        IF o.order_type <> 'investigation' OR o.catalog_type <> 'lab_test'
           OR t.id IS NULL OR t.kind <> 'test' THEN
            PERFORM dx_raise('dx_order_not_adoptable:' || left(o.item_name, 60));
        END IF;
        IF o.status NOT IN ('ordered', 'billed') OR o.sample_collected_at IS NOT NULL THEN
            PERFORM dx_raise('dx_order_not_adoptable:' || left(o.item_name, 60));
        END IF;
        IF t.approved_at IS NULL THEN PERFORM dx_raise('dx_test_not_approved:' || left(t.name, 60)); END IF;
    END LOOP;

    PERFORM set_config('kriya.dx_adopt', p_clinic_id::TEXT, true);
    v_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::INTEGER;
    FOR g IN SELECT o2.patient_id, o2.family_member_id, o2.source, o2.branch_id,
                    array_agg(o2.id ORDER BY o2.created_at, o2.id) AS ids,
                    bool_or(lt.container_id IS NOT NULL) AS needs_tube,
                    (array_agg(o2.urgency ORDER BY array_position(ARRAY['stat', 'urgent', 'routine'], o2.urgency)))[1] AS urgency,
                    (array_agg(o2.doctor_id ORDER BY o2.created_at) FILTER (WHERE o2.doctor_id IS NOT NULL))[1] AS doctor_id
               FROM opd_orders o2 JOIN lab_tests lt ON lt.clinic_id = o2.clinic_id AND lt.id = o2.catalog_reference_id
              WHERE o2.clinic_id = p_clinic_id AND o2.id = ANY (p_order_ids) AND o2.accession_id IS NULL
              GROUP BY o2.patient_id, o2.family_member_id, o2.source, o2.branch_id
              ORDER BY o2.patient_id, o2.source LOOP
        v_branch := g.branch_id;
        IF v_branch IS NULL THEN       -- an order without a branch goes to the clinic's only collection site
            SELECT CASE WHEN count(*) = 1 THEN min(branch_id::TEXT)::UUID END INTO v_branch
              FROM dx_sites WHERE clinic_id = p_clinic_id AND is_active AND kind <> 'processing';
        END IF;
        SELECT id INTO v_collect FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = v_branch AND is_active;
        IF v_collect IS NULL THEN PERFORM dx_raise('dx_no_collection_site'); END IF;
        SELECT id INTO v_process FROM dx_sites WHERE clinic_id = p_clinic_id AND id = v_collect AND kind <> 'collection';
        IF v_process IS NULL THEN
            SELECT CASE WHEN count(*) = 1 THEN min(id::TEXT)::UUID END INTO v_process
              FROM dx_sites WHERE clinic_id = p_clinic_id AND is_active AND kind <> 'collection';
        END IF;
        SELECT * INTO v_patient FROM patients WHERE clinic_id = p_clinic_id AND id = g.patient_id;
        v_snapshot := jsonb_build_object('name', v_patient.name, 'mrn', v_patient.mrn, 'gender', v_patient.gender,
                                         'age_years', v_patient.age_years, 'date_of_birth', v_patient.date_of_birth);
        v_member := NULL;
        IF g.family_member_id IS NOT NULL THEN
            SELECT * INTO v_member FROM family_members WHERE clinic_id = p_clinic_id AND id = g.family_member_id;
            v_snapshot := jsonb_build_object('name', v_member.full_name, 'mrn', v_member.mrn, 'gender', v_member.gender,
                                             'age_years', v_member.age_years, 'date_of_birth', v_member.date_of_birth,
                                             'relationship', v_member.relationship);
        END IF;
        SELECT name INTO v_doctor FROM doctors WHERE clinic_id = p_clinic_id AND id = g.doctor_id;
        v_seq := opd_next_counter(p_clinic_id, 'dx:' || v_year);
        v_sseq := CASE WHEN g.needs_tube THEN dx_next_sample_seq(p_clinic_id, v_year) END;
        INSERT INTO dx_accessions (clinic_id, branch_id, accession_year, accession_seq, patient_id, family_member_id,
                                   source, referrer_snapshot, patient_type, billing_mode, sample_seq, priority,
                                   patient_snapshot, created_by, created_by_name)
        VALUES (p_clinic_id, v_branch, v_year, v_seq, g.patient_id, g.family_member_id, g.source,
                CASE WHEN v_doctor IS NULL THEN NULL ELSE jsonb_build_object('name', v_doctor, 'kind', 'doctor') END,
                CASE WHEN g.source = 'ipd' THEN 'ip' ELSE 'op' END, 'internal', v_sseq, g.urgency, v_snapshot,
                p_actor_id, left(COALESCE(p_actor_name, 'system'), 100))
        RETURNING id INTO v_id;
        FOR o IN SELECT * FROM opd_orders WHERE clinic_id = p_clinic_id AND id = ANY (g.ids)
                  ORDER BY array_position(g.ids, id) LOOP
            SELECT * INTO t FROM lab_tests WHERE clinic_id = p_clinic_id AND id = o.catalog_reference_id;
            INSERT INTO dx_accession_items (clinic_id, accession_id, lab_test_id, kind, item_name, test_code, mrp_paise,
                                            price_paise, discount_paise, patient_share_paise, payer_share_paise,
                                            price_source)
            VALUES (p_clinic_id, v_id, t.id, 'test', left(o.item_name, 200), t.test_code, 0, 0, 0, 0, 0, 'manual')
            RETURNING id INTO v_item;
            v_sample := NULL;
            IF t.container_id IS NOT NULL THEN
                IF v_process IS NULL THEN PERFORM dx_raise('dx_no_processing_site'); END IF;
                SELECT id INTO v_sample FROM dx_samples
                 WHERE clinic_id = p_clinic_id AND accession_id = v_id AND container_id = t.container_id
                   AND processing_site_id = v_process AND status = 'pending_collection'
                 ORDER BY created_at LIMIT 1;
                IF v_sample IS NULL THEN
                    SELECT * INTO v_cont FROM dx_containers WHERE clinic_id = p_clinic_id AND id = t.container_id;
                    -- ADR-DX-05: the visit's shared sequence for the first tube of a container, else a fresh one
                    v_tseq := CASE WHEN EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND accession_id = v_id
                                                                         AND container_id = t.container_id)
                                   THEN dx_next_sample_seq(p_clinic_id, v_year) ELSE v_sseq END;
                    INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source,
                                            tube_seq, collection_site_id, processing_site_id)
                    VALUES (p_clinic_id, v_id, v_cont.id, COALESCE(v_cont.sample_type, 'other'),
                            lpad((v_year % 100)::TEXT, 2, '0') || v_cont.code || lpad(v_tseq::TEXT, 8, '0'),
                            'generated', v_tseq, v_collect, v_process)
                    RETURNING id INTO v_sample;
                END IF;
            END IF;
            UPDATE opd_orders SET accession_id = v_id, accession_item_id = v_item, dx_sample_id = v_sample
             WHERE clinic_id = p_clinic_id AND id = o.id;
            v_n := v_n + 1;
        END LOOP;
        PERFORM dx_log(p_clinic_id, v_id, 'adopted', p_actor_id, p_actor_name,
                       jsonb_build_object('orders', to_jsonb(g.ids), 'source', g.source));
        v_visits := v_visits || dx_visit_json(p_clinic_id, v_id);
    END LOOP;
    PERFORM set_config('kriya.dx_adopt', '', true);
    SELECT COALESCE(jsonb_agg(x), '[]'::jsonb) INTO v_skipped FROM unnest(p_order_ids) x
     WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_visits) v, jsonb_array_elements(v -> 'orders') vo
                        WHERE (vo ->> 'id')::UUID = x);
    RETURN jsonb_build_object('visits', v_visits, 'adopted', v_n, 'skipped', v_skipped);
END $$;

-- The bench functions of 130 asked "is this a source = 'dx' order?"; with adoption the
-- question is "is this order on a DX visit?" (one textual change each, verified below).
DO $$
DECLARE f TEXT; v_def TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
                             'dx_verify_result(uuid,uuid,timestamptz,text,text,uuid,text)',
                             'dx_approve_results(uuid,jsonb,uuid,text,boolean)'] LOOP
        v_def := pg_get_functiondef(f::regprocedure);
        EXECUTE replace(v_def, $s$o.source <> 'dx'$s$, 'o.accession_id IS NULL');
    END LOOP;
    v_def := pg_get_functiondef('dx_lab_results_gate()'::regprocedure);
    EXECUTE replace(v_def, $s$(SELECT source FROM opd_orders WHERE clinic_id = OLD.clinic_id AND id = OLD.order_id) IS DISTINCT FROM 'dx'$s$,
                    '(SELECT accession_id FROM opd_orders WHERE clinic_id = OLD.clinic_id AND id = OLD.order_id) IS NULL');
END $$;

-- ── 10. Purge v7 (adds the report tables; unlinks adopted OPD / IPD orders) ─
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_share_links WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_print_log WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_deliveries WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_delivery_overrides WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_results WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_report_releases WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('releases', n);
    DELETE FROM dx_outbox WHERE clinic_id = p_clinic_id;
    -- adopted OPD / IPD orders stay with their encounter or admission, unlinked from the bench
    UPDATE opd_orders SET accession_id = NULL, accession_item_id = NULL, dx_sample_id = NULL
     WHERE clinic_id = p_clinic_id AND source <> 'dx' AND accession_id IS NOT NULL; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('adopted_orders_unlinked', n);
    DELETE FROM dx_result_attachments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_critical_events WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('critical_events', n);
    DELETE FROM dx_result_verifications WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_result_value_meta WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM dx_batch_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_batches WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('batches', n);
    DELETE FROM opd_orders WHERE clinic_id = p_clinic_id AND source = 'dx'; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('orders', n);
    DELETE FROM dx_events WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_items WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accessions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('visits', n);
    UPDATE family_members SET dx_b2b_account_id = NULL WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL;
    DELETE FROM dx_referrers WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_report_templates WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('templates', n);
    DELETE FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    DELETE FROM dx_routing_rules WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('routing_rules', n);
    DELETE FROM dx_outsource_partners WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_test_components WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('components', n);
    DELETE FROM dx_signatories WHERE clinic_id = p_clinic_id;
    UPDATE lab_tests SET department_id = NULL, container_id = NULL
     WHERE clinic_id = p_clinic_id AND (department_id IS NOT NULL OR container_id IS NOT NULL);
    DELETE FROM dx_containers  WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_departments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 11. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_report_templates', 'dx_report_releases', 'dx_release_results', 'dx_release_deliveries',
                             'dx_delivery_overrides', 'dx_outbox', 'dx_print_log', 'dx_share_links']
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
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND (p.proname LIKE 'dx\_%' OR p.proname = 'opd_guard_order')
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
        END IF;
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 12. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
          AND tablename IN ('dx_report_templates', 'dx_report_releases', 'dx_release_results', 'dx_release_deliveries',
                            'dx_delivery_overrides', 'dx_outbox', 'dx_print_log', 'dx_share_links')) <> 8 THEN
        RAISE EXCEPTION '131 verify: DX report tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_report_templates_guard', 'trg_dx_report_releases_immutable',
                                                         'trg_dx_outbox_receipt', 'trg_dx_outbox_invoice',
                                                         'trg_dx_internal_items')) <> 5 THEN
        RAISE EXCEPTION '131 verify: DX report triggers missing';
    END IF;
    IF to_regprocedure('dx_delivery_decision(uuid,uuid,text)') IS NULL
       OR to_regprocedure('dx_create_release(uuid,uuid,uuid[],text,uuid,text)') IS NULL
       OR to_regprocedure('dx_claim_delivery(uuid,uuid)') IS NULL
       OR to_regprocedure('dx_mark_delivery_result(uuid,uuid,boolean,uuid,text)') IS NULL
       OR to_regprocedure('dx_reevaluate_deliveries(uuid,uuid)') IS NULL
       OR to_regprocedure('dx_adopt_orders(uuid,uuid[],uuid,text)') IS NULL THEN
        RAISE EXCEPTION '131 verify: DX report functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\_%'
                 AND (prosrc LIKE $s$%o.source <> 'dx'%$s$ OR prosrc LIKE $s$%IS DISTINCT FROM 'dx'%$s$)) THEN
        RAISE EXCEPTION '131 verify: a bench function still keys on source = dx';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_report_templates', 'dx_report_releases', 'dx_release_results',
                                                        'dx_release_deliveries', 'dx_delivery_overrides', 'dx_outbox',
                                                        'dx_print_log', 'dx_share_links')
                 AND relkind = 'r' AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '131 verify: RLS not forced on DX report tables';
    END IF;
END $$;
