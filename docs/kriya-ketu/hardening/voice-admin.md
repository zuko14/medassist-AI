# KK-P0-15 hardening checklist — voice receptionist knowledge, lexicon, learning (KK-P4-13)

Audited 2026-10-15 by reading `app/routers/voice_admin.py` (`_scope`, `put_settings`, `LexiconIn`, `add_lexicon`,
`delete_lexicon`, `KnowledgeIn`, `add_knowledge`, `delete_knowledge`, `list_learning`, `approve_learning`,
`dismiss_learning`) and `app/services/reception_learning.py` (`approve`, `dismiss`, `load_catalogue`). Same columns
as [desk.md](desk.md). New gap → **D-36** (open, low).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `POST /admin/voice/knowledge` | `voice.knowledge_add` (R2) | VOICE_MANAGE (handler `_scope`, checked first) | ✔ `clinic_id` from `enforce_clinic_access` in the insert | n/a (clinic-wide) | ✔ `KnowledgeIn` 3-200 / 2-600 — ✘ **D-36**: no clinical screen on the answer | ✘ no unique key; Kriya refuses a question already answered and never retries | ✔ `voice_knowledge_add` | ✔ fixed text | ✔ `sb()` | Registered. Kriya screens question + answer (`screen_message`) before planning |
| `DELETE /admin/voice/knowledge/{id}` | `voice.knowledge_delete` (R2) | VOICE_MANAGE | ✔ `.eq("clinic_id")` + id | n/a | ✔ | ✔ 404 when gone; the plan hash holds the row | ✔ | ✔ | ✔ | Registered |
| `POST /admin/voice/lexicon` | `voice.lexicon_add` (R2) | VOICE_MANAGE | ✔ | n/a | ✔ `LexiconIn` (kind pattern, phrase ≤ 80, canonical ≤ 120) | ✔ unique (clinic, kind, lower phrase) → 409 | ✔ | ✔ | ✔ | Registered. The target must be on the Learning form's own catalogue (doctors / tests / departments); pronunciation entries stay on the page |
| `DELETE /admin/voice/lexicon/{id}` | `voice.lexicon_delete` (R2) | VOICE_MANAGE | ✔ | n/a | ✔ | ✔ 404 when gone | ✔ | ✔ | ✔ | Registered |
| `GET /admin/voice/learning/suggestions` | `voice.learning_review` (R0) + previews | VOICE_VIEW | ✔ | n/a | ✔ status literal | — | — | ✔ | ✔ | Registered; Kriya's card never shows `sample_utterances` (callers' words) |
| `POST …/learning/suggestions/{id}/approve` | `voice.learning_approve` (R2) | VOICE_MANAGE | ✔ | n/a | ✔ `ApproveIn`; target re-checked against the catalogue; clinical screen on phrase and answer | ✔ CAS on `status = pending` (two approvals → one entry) | ✔ | ✔ `ReviewError` fixed text | ✔ | Registered, **as suggested only** (body `{}`); FAQ suggestions (need an answer) and corrections stay on the page |
| `POST …/learning/suggestions/{id}/dismiss` | `voice.learning_dismiss` (R2) | VOICE_MANAGE | ✔ | n/a | ✔ | ✔ CAS on `pending` | ✔ | ✔ | ✔ | Registered |
| `PUT /admin/voice/settings` | `voice.settings_update` | VOICE_MANAGE (checked before the body since D-12) | ✔ | n/a | ✔ window 09-21 | ✔ since **D-13** fix (2026-10-15): only changed keys are written (`config_store`, migration 110) | ✔ | ✔ | ✔ | **Registered** 2026-10-15 as `voice.settings_update` (R2, one setting per request, bounds mirrored in the preview, budget never written) and `voice.auto_leads` (KK-P4-06) |

Observations:
- VOICE_MANAGE does not include VOICE_VIEW; the previews read with VOICE_VIEW, so a manage-only user gets
  "You need the Voice View permission too" (the panel also hides the AI Receptionist page from them).
- `POST /admin/voice/test-turn` (registry `voice.test_turn`, R0) is not registered: it creates a test call row.
