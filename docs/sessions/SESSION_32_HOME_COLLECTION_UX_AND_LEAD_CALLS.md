# Session 32 — Home Collection UX & Lead Call Upgrades

**Date:** 2026-10-08  
**Branch:** `main` (uncommitted)  
**Scope:** Lab catalog card view, home-collection toolbar redesign, custom visit hours, phlebotomist leave, lead-call hardening (services pitch, WhatsApp line, one-by-one dialing, bulk import, Exotel error capture)  
**Migration:** `101_phlebotomist_off_dates.sql` — adds `off_dates DATE[]` to `clinic_admins`

---

## 1. Summary

Five interconnected upgrades to the diagnostic home-collection and lead-calling experience:

1. **Lab Tests Catalog** — card-view with per-service-type counts; clicking opens a glass overlay with tabs, search, "40 of 1391 shown" counter, and the full edit/delete/bulk-delete table.
2. **Home Collections Toolbar** — a 7-day strip (Today, Tmrw, …) with ‹ › arrows and a calendar button for any date, plus branch buttons replacing the dropdown. Keeps the selected day visible on mobile.
3. **Custom Home Visit Hours** — new "Same as sample collection window / Custom hours" choice in settings. Custom hours have Morning, Afternoon, Evening, Morning + Evening and Full Day presets, or up to 3 arbitrary time ranges. Centres that don't change behave exactly as today.
4. **Phlebotomist Leave** — a "Leave…" button per phlebotomist with a From–To range. Leave days show as removable tags, the person shows "On leave" on those days. Visits on leave days reassign to a colleague automatically; admins get an alert if nobody is free. Nobody can be assigned to someone on leave. If every phlebotomist for a branch is on leave, WhatsApp offers no home slots that day, and patients get the existing "pick another date" reply. Centres with no phlebotomists at all keep working as-is.
5. **Lead Calls** — five sub-features:
   - **Services pitch**: staff write, in Telugu, Hindi, and English, what Kriya should say about their services. Spoken word for word after the greeting, in the caller's language only. No AI writes what the caller hears.
   - **WhatsApp line**: when someone declines, Kriya tells them they can book any time on the clinic's WhatsApp number, read digit by digit.
   - **One by one**: calls now go out one at a time per clinic by default (settable up to 5).
   - **Bulk import**: paste numbers or load a CSV, up to 500 at a time. Staff must tick "everyone asked us to contact them" and say where the list came from. Anyone who opted out is never called. Duplicates, numbers already queued, and anyone called in the last 7 days are skipped, with a reason shown for each.
   - **Readable status**: the call list now says why a call is waiting, e.g. "Scheduled — waits for your calling hours".

---

## 2. Migration 101

**File:** [`migrations/101_phlebotomist_off_dates.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/101_phlebotomist_off_dates.sql)  
**Rollback:** [`migrations/rollback/101_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/101_down.sql)

```sql
ALTER TABLE clinic_admins
    ADD COLUMN IF NOT EXISTS off_dates DATE[] NOT NULL DEFAULT '{}';
```

- `off_dates`: array of dates a phlebotomist is marked off (leave, sick day). Empty by default.
- Past dates are pruned by the app on every write; the array stays small.
- Additive, `NOT NULL` with constant default: metadata-only `ALTER`, zero backfill.

### Checksum registration (run in Supabase SQL Editor)

```sql
INSERT INTO schema_migrations (name, checksum) VALUES
    ('101_phlebotomist_off_dates.sql', '590b92624170f6fbbb07fab6083529d733868e43e16cc09500efac4652cee2b4')
ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
```

---

## 3. Files Changed

14 modified, 4 new — 851 insertions, 86 deletions.

### 3.1 Backend — New Files

| File | What It Does |
|---|---|
| [`migrations/101_phlebotomist_off_dates.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/101_phlebotomist_off_dates.sql) | **NEW.** Schema migration adding `off_dates DATE[]` to `clinic_admins`. |
| [`migrations/rollback/101_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/101_down.sql) | **NEW.** Rollback script dropping the column. |
| [`tests/test_home_collection_leave_windows.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_home_collection_leave_windows.py) | **NEW.** Tests for custom visit hours, leave, assignment block, and branch staff limits. |
| [`tests/voice/test_lead_calls.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/voice/test_lead_calls.py) | **NEW.** Tests for bulk import rules, one-by-one dialing, Exotel error capture, and services pitch. |

### 3.2 Backend — Modified Files

| File | Lines Changed | What Changed |
|---|---|---|
| [`app/services/home_collection.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/home_collection.py) | +114 −8 | Custom visit hours logic (presets and arbitrary ranges), `off_dates` pruning, leave-aware phlebotomist assignment (skip on-leave staff, auto-reassign existing visits, alert if nobody free), WhatsApp slot suppression when all phlebotomists are on leave for a branch. |
| [`app/routers/home_collection.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/home_collection.py) | +60 −4 | New endpoints: save/load custom visit hours, mark/clear leave dates, list leave-per-phlebotomist. 7-day toolbar data endpoint. |
| [`app/routers/voice_admin.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/voice_admin.py) | +45 | New endpoints: bulk lead import (CSV/paste, up to 500), services pitch CRUD (per-language), max-concurrent-calls setting. |
| [`app/voice/outbound.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/outbound.py) | +155 −6 | One-by-one dialing (configurable up to 5 concurrent per clinic). Bulk import validation (opt-out check, 7-day dedup, queue dedup, consent gate). Exotel error code capture on `VOICE_DIAL_FAILED`. Readable status strings (e.g. "Scheduled — waits for your calling hours"). |
| [`app/voice/dialog.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/dialog.py) | +10 −3 | Services pitch injection: after greeting, speak the clinic's custom pitch in the caller's language. WhatsApp number digit-by-digit readout on decline. |
| [`app/voice/responses.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/responses.py) | +36 −6 | New templates: `services_pitch` (placeholder for clinic-written text), `whatsapp_number_offer` (decline follow-up), `call_status_*` (human-readable status labels). |
| [`app/voice/session.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/session.py) | +4 −2 | Passes services pitch and WhatsApp number to dialog context for lead calls. |
| [`app/voice/store.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/store.py) | +5 −1 | Stores Exotel error code on dial failure; surfaces it in the call list. |

### 3.3 Frontend — Admin Panel

| File | Lines Changed | What Changed |
|---|---|---|
| [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html) | +481 −25 | **Lab Tests tab**: card-per-service-type with count badge, glass overlay with tabs/search/table. **Home Collections tab**: 7-day toolbar strip, branch buttons, ‹ › navigation, calendar picker, mobile-friendly scroll. **Settings → Home Collection**: "Same as collection window / Custom" toggle, preset buttons, arbitrary time-range editor (up to 3 rows). **Phlebotomist section**: "Leave…" button, date-range picker, leave-day tags with ✕ remove, "On leave" badge. **Lead Calls tab**: services pitch editor (per-language textarea), consent checkbox + source field for bulk import, paste/CSV upload area, skip-reason display per row, readable status labels, max-concurrent-calls slider. |

### 3.4 Documentation

| File | What Changed |
|---|---|
| [`docs/CLIENT_ONBOARDING_SOP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/CLIENT_ONBOARDING_SOP.md) | Added phlebotomist leave and custom visit hours setup steps. |
| [`docs/agent-context/02-SYSTEM-FLOWS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/02-SYSTEM-FLOWS.md) | Added leave-aware assignment flow and bulk import flow. |
| [`docs/agent-context/03-DATABASE-MODEL.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/03-DATABASE-MODEL.md) | Documented `off_dates DATE[]` column on `clinic_admins`. |
| [`docs/agent-context/04-API-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/04-API-MAP.md) | Added new home-collection and voice-admin endpoints. |
| [`docs/agent-context/05-FRONTEND-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/05-FRONTEND-MAP.md) | Updated lab tests, home collection, and lead calls tab descriptions. |

---

## 4. Design Decisions

### 4.1 Leave stored on `clinic_admins.off_dates`, not a new table

- Phlebotomists are already rows in `clinic_admins` (with role `phlebotomist`).
- `clinic_admins` is in `TENANT_OWNED_TABLES`, so tenant isolation is inherited — no new table, no new RLS policy, no new tenancy entry.
- A `DATE[]` column stays small because past dates are pruned on every write.

### 4.2 Custom visit hours in `clinics.config` / `branches.config` JSONB

- No migration needed — JSONB keys are self-describing.
- Default = `null` → use the existing sample-collection window (zero behaviour change).
- Presets (Morning, Afternoon, etc.) are evaluated server-side from named constants, not stored as magic hour ranges the frontend defines.

### 4.3 One-by-one dialing as default

- The old behaviour placed up to 5 calls in parallel. Exotel rate limits and staff confusion (two people answering at once) made this the wrong default.
- Default = 1, configurable up to 5 via admin panel slider.

### 4.4 Bulk import: consent-first, not filter-after

- Staff tick a checkbox ("everyone asked us to contact them") and enter a source description before the import is accepted. This prevents accidental cold-calling.
- Opt-out list is checked server-side — cannot be bypassed from the frontend.
- 7-day dedup is also server-side: even if the same number is imported twice, the second import shows "Called 2 days ago — skipped".

### 4.5 Exotel error capture

- The lead-call bug wasn't in our queue — it was an Exotel API rejection. But until now, the error code was only in Render's logs.
- Now `VOICE_DIAL_FAILED` writes the Exotel error code and (masked) phone number to the `lead_calls` / voice store, and the admin panel shows it in the call list.

---

## 5. Exotel Diagnosis

> **The bug is at Exotel, not in the queue.**

The call queued at 8:23 PM (after the 10:00–19:00 calling window), so the system held it until 10:00 the next morning. At 10:00 it tried, and Exotel's API refused the call. The retry at 12:00 IST likely failed the same way.

Likely causes (check the `VOICE_DIAL_FAILED` line in Render logs at 10:00 IST):
- Exotel API key or token expired/wrong
- API host mismatch (`api.exotel.com` vs `api.in.exotel.com`)
- Empty `EXOTEL_OUTBOUND_FLOW_URL`
- Restriction on the Exotel account

Once this session's changes are live, the error code will show in the call list, and the error text will go to logs with the phone number masked.

---

## 6. Test Results

```
32 new tests pass (leave, visit hours, assignment block, bulk import, one-by-one dialing, Exotel error capture, services pitch)
289 voice + home-collection suite tests pass
573 tenant-isolation, permission, clinical-safety, webhook, and conversation tests pass
Full suite (~3,000 tests): running at time of writing
```

---

## 7. Status

| Item | Status |
|---|---|
| Code complete | ✅ All files written |
| Migration 101 applied to production | ❌ Not yet |
| Checksum registered in Supabase | ❌ Not yet (SQL below) |
| Committed to git | ❌ Not yet |
| Deployed to Render | ❌ Not yet |
| UI tested against live backend | ❌ Not yet (Chrome only, sample data) |
| Full test suite | ⏳ Running in background |

### Pre-deploy steps

1. **Run migration 101 in Supabase SQL Editor:**
   ```sql
   ALTER TABLE clinic_admins
       ADD COLUMN IF NOT EXISTS off_dates DATE[] NOT NULL DEFAULT '{}';
   ```

2. **Register checksum in Supabase SQL Editor:**
   ```sql
   INSERT INTO schema_migrations (name, checksum) VALUES
       ('101_phlebotomist_off_dates.sql', '590b92624170f6fbbb07fab6083529d733868e43e16cc09500efac4652cee2b4')
   ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
   ```

3. Commit, push, deploy to Render.

4. Check Render logs for `VOICE_DIAL_FAILED` at 10:00 IST to resolve the Exotel issue.

---

## 8. No New Environment Variables Required

All configuration is in `clinics.config` / `branches.config` JSONB or existing settings. No platform-level env vars needed.
