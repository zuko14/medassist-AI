-- ============================================================================
-- Migration 092: WhatsApp Leads (admin panel "Leads" section)
-- ============================================================================
-- Every patient who messages a clinic's WhatsApp number already gets a
-- `patients` row on their very first message (ConversationManager
-- ._handle_message_locked -> create_patient; nothing else inserts into
-- `patients`, and CSV imports go to a separate table, migration 088). So the
-- contact itself needs no new storage.
--
-- What was missing is WHAT they asked about. From this release the
-- conversation handler writes one analytics_events row per meaningful request:
--   event_type = 'lead_interest', intent = <bounded label>, department = <dept>
-- Labels only, never message text: no chat transcript is stored (DPDP; see the
-- deferred staff-inbox spec). Rows ride the existing 12-month analytics purge
-- and the existing per-phone erasure in delete_patient_data().
--
-- This migration adds:
--   1. a partial index for those rows (tiny: only the new event type), and
--   2. admin_whatsapp_leads(): one round trip that returns the page, the totals
--      and the interest breakdown for ONE clinic. It runs in SQL because the
--      aggregation spans patients x conversations x appointments x events;
--      doing it through PostgREST would silently truncate at max_rows.
--
-- Tenant safety: every table access is predicated on p_clinic_id, which the
-- API derives from the authenticated admin (enforce_clinic_access). The
-- function is SECURITY INVOKER and callable by service_role only.
-- Safe with the current build running: purely additive.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_analytics_lead_interest
    ON analytics_events (clinic_id, phone, created_at DESC)
    WHERE event_type = 'lead_interest';

CREATE OR REPLACE FUNCTION public.admin_whatsapp_leads(
    p_clinic_id UUID,
    p_segment   TEXT    DEFAULT 'all',   -- all | hot | open | booked | dnc
    p_interest  TEXT    DEFAULT NULL,    -- a lead_interest label
    p_search    TEXT    DEFAULT NULL,    -- name or phone fragment
    p_days      INTEGER DEFAULT 30,      -- activity window; 0 = all time
    p_limit     INTEGER DEFAULT 50,
    p_offset    INTEGER DEFAULT 0
)
RETURNS JSONB
LANGUAGE sql
STABLE
SET search_path = public
AS $$
WITH params AS (
    SELECT
        CASE WHEN COALESCE(p_days, 0) <= 0 THEN '-infinity'::timestamptz
             ELSE now() - make_interval(days => LEAST(p_days, 3650)) END AS since,
        LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)  AS lim,
        GREATEST(COALESCE(p_offset, 0), 0)              AS off,
        NULLIF(btrim(COALESCE(p_search, '')), '')       AS q
),
appt AS (
    SELECT a.patient_phone AS phone,
           count(*) FILTER (WHERE a.status NOT IN ('cancelled', 'pending_payment')) AS bookings,
           count(*) FILTER (WHERE a.status = 'pending_payment')                    AS unpaid,
           max(a.appointment_date) FILTER (WHERE a.status NOT IN ('cancelled', 'pending_payment')) AS last_booking_date
    FROM appointments a
    WHERE a.clinic_id = p_clinic_id
    GROUP BY a.patient_phone
),
-- A data-deletion request purges conversations + analytics but keeps the
-- patients row, and nothing marks it. The handler logs 'data_deleted' AFTER
-- the purge, so that is the erasure signal. (patients.anonymized_at from
-- migration 007 is never written by the app and is absent in production.)
erased AS (
    SELECT e.phone, max(e.created_at) AS deleted_at
    FROM analytics_events e
    WHERE e.clinic_id = p_clinic_id AND e.event_type = 'data_deleted'
    GROUP BY e.phone
),
ev AS (
    SELECT e.phone, count(*) AS interactions, max(e.created_at) AS last_interest_at
    FROM analytics_events e, params
    WHERE e.clinic_id = p_clinic_id
      AND e.event_type = 'lead_interest'
      AND e.created_at >= params.since
    GROUP BY e.phone
),
base AS (
    SELECT
        p.phone,
        p.name,
        p.language,
        p.created_at AS first_contact_at,
        -- session_expires_at is pushed to now()+24h on every inbound message,
        -- so minus 24h it is the time of the patient's last message.
        GREATEST(p.created_at, c.session_expires_at - interval '24 hours') AS last_active_at,
        c.session_expires_at,
        c.state,
        (p.opted_in IS FALSE OR p.data_consent IS FALSE) AS dnc,
        COALESCE(a.bookings, 0)     AS bookings,
        COALESCE(a.unpaid, 0)       AS unpaid,
        a.last_booking_date,
        COALESCE(ev.interactions, 0) AS interactions,
        ev.last_interest_at
    FROM patients p
    LEFT JOIN conversations c ON c.clinic_id = p.clinic_id AND c.phone = p.phone
    LEFT JOIN appt a          ON a.phone = p.phone
    LEFT JOIN ev              ON ev.phone = p.phone
    LEFT JOIN erased d        ON d.phone = p.phone
    WHERE p.clinic_id = p_clinic_id
      -- Erased contacts stay out until they message the clinic again.
      AND (d.deleted_at IS NULL OR c.session_expires_at - interval '24 hours' > d.deleted_at)
),
staged AS (
    SELECT b.*,
           CASE
               WHEN b.dnc                                        THEN 'dnc'
               WHEN b.bookings > 0                               THEN 'booked'
               WHEN b.session_expires_at > now()                 THEN 'hot'
               WHEN b.last_active_at > now() - interval '7 days' THEN 'warm'
               ELSE 'cold'
           END AS stage
    FROM base b
),
in_period AS (
    SELECT s.* FROM staged s, params
    WHERE COALESCE(s.last_active_at, s.first_contact_at) >= params.since
),
filtered AS (
    SELECT s.* FROM in_period s, params
    WHERE (
            COALESCE(p_segment, 'all') = 'all'
         OR (p_segment = 'hot'    AND s.stage = 'hot')
         OR (p_segment = 'open'   AND s.stage IN ('hot', 'warm', 'cold'))
         OR (p_segment = 'booked' AND s.stage = 'booked')
         OR (p_segment = 'dnc'    AND s.stage = 'dnc')
          )
      AND (params.q IS NULL
           OR s.name ILIKE '%' || replace(replace(replace(params.q, '\', '\\'), '%', '\%'), '_', '\_') || '%'
           OR s.phone LIKE '%' || NULLIF(regexp_replace(params.q, '\D', '', 'g'), '') || '%')
      AND (p_interest IS NULL OR EXISTS (
            SELECT 1 FROM analytics_events x
            WHERE x.clinic_id = p_clinic_id AND x.phone = s.phone
              AND x.event_type = 'lead_interest' AND x.intent = p_interest
              AND x.created_at >= params.since))
),
page AS (
    SELECT f.* FROM filtered f
    ORDER BY f.last_active_at DESC NULLS LAST, f.phone
    LIMIT (SELECT lim FROM params) OFFSET (SELECT off FROM params)
)
SELECT jsonb_build_object(
    'total', (SELECT count(*) FROM filtered),
    'summary', (
        SELECT jsonb_build_object(
            'contacts_all_time', (SELECT count(*) FROM staged),
            'active',            count(*),
            'new',               count(*) FILTER (WHERE first_contact_at >= (SELECT since FROM params)),
            'hot',               count(*) FILTER (WHERE stage = 'hot'),
            'open',              count(*) FILTER (WHERE stage IN ('hot', 'warm', 'cold')),
            'booked',            count(*) FILTER (WHERE stage = 'booked'),
            'dnc',               count(*) FILTER (WHERE stage = 'dnc'),
            'unpaid',            count(*) FILTER (WHERE unpaid > 0 AND bookings = 0 AND NOT dnc)
        ) FROM in_period
    ),
    'interests', COALESCE((
        SELECT jsonb_agg(jsonb_build_object('label', intent, 'patients', patients, 'requests', requests)
                         ORDER BY patients DESC, requests DESC)
        FROM (
            SELECT e.intent, count(DISTINCT e.phone) AS patients, count(*) AS requests
            FROM analytics_events e, params
            WHERE e.clinic_id = p_clinic_id AND e.event_type = 'lead_interest'
              AND e.created_at >= params.since AND e.intent IS NOT NULL
            GROUP BY e.intent
            ORDER BY 2 DESC, 3 DESC
            LIMIT 12
        ) t
    ), '[]'::jsonb),
    'departments', COALESCE((
        SELECT jsonb_agg(jsonb_build_object('label', department, 'patients', patients) ORDER BY patients DESC)
        FROM (
            SELECT e.department, count(DISTINCT e.phone) AS patients
            FROM analytics_events e, params
            WHERE e.clinic_id = p_clinic_id AND e.event_type = 'lead_interest'
              AND e.created_at >= params.since AND e.department IS NOT NULL
            GROUP BY e.department
            ORDER BY 2 DESC
            LIMIT 8
        ) t
    ), '[]'::jsonb),
    'rows', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            -- A do-not-contact patient's number is not handed to the panel.
            'phone',            CASE WHEN pg.dnc
                                     THEN left(pg.phone, 3) || repeat('•', GREATEST(length(pg.phone) - 7, 0)) || right(pg.phone, 4)
                                     ELSE pg.phone END,
            'name',             pg.name,
            'language',         pg.language,
            'stage',            pg.stage,
            'first_contact_at', pg.first_contact_at,
            'last_active_at',   pg.last_active_at,
            'window_open',      COALESCE(pg.session_expires_at > now(), false),
            'window_closes_at', CASE WHEN pg.session_expires_at > now() THEN pg.session_expires_at END,
            -- The API turns this into `mid_booking` with MID_BOOKING_STATES and drops it.
            'state',            pg.state,
            'bookings',         pg.bookings,
            'unpaid',           pg.unpaid,
            'last_booking_date', pg.last_booking_date,
            'interactions',     pg.interactions,
            'interests', COALESCE((
                SELECT jsonb_agg(i.intent ORDER BY i.n DESC, i.last_at DESC)
                FROM (
                    SELECT x.intent, count(*) AS n, max(x.created_at) AS last_at
                    FROM analytics_events x, params
                    WHERE x.clinic_id = p_clinic_id AND x.phone = pg.phone
                      AND x.event_type = 'lead_interest' AND x.created_at >= params.since
                      AND x.intent IS NOT NULL
                    GROUP BY x.intent
                    ORDER BY 2 DESC, 3 DESC
                    LIMIT 4
                ) i
            ), '[]'::jsonb),
            'departments', COALESCE((
                SELECT jsonb_agg(DISTINCT x.department)
                FROM analytics_events x, params
                WHERE x.clinic_id = p_clinic_id AND x.phone = pg.phone
                  AND x.event_type = 'lead_interest' AND x.created_at >= params.since
                  AND x.department IS NOT NULL
            ), '[]'::jsonb)
        ) ORDER BY pg.last_active_at DESC NULLS LAST, pg.phone)
        FROM page pg
    ), '[]'::jsonb)
);
$$;

-- Backend (service_role) only. Supabase grants EXECUTE on new public functions
-- to anon/authenticated by default (see migration 091), so revoke explicitly.
REVOKE ALL ON FUNCTION public.admin_whatsapp_leads(UUID, TEXT, TEXT, TEXT, INTEGER, INTEGER, INTEGER) FROM PUBLIC;

DO $$
DECLARE r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION public.admin_whatsapp_leads(UUID, TEXT, TEXT, TEXT, INTEGER, INTEGER, INTEGER) FROM %I', r);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION public.admin_whatsapp_leads(UUID, TEXT, TEXT, TEXT, INTEGER, INTEGER, INTEGER) TO service_role;
    END IF;
END $$;
