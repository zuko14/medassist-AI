# Kriya Continuous Care — API (`/admin/cc/*`, owner toggle)

> Router `app/routers/cc.py`, mounted in `app/main.py`; every route starts with `_cc_scope(user, clinic_id, *perms,
> new_work=…)`: permission → tenant (`enforce_clinic_access`) → `cc_access` → wind-down → branch pin (same order as
> `_rx_scope`, AUDIT A3). Every new route is added to `tests/test_cc_route_matrix.py` in the same commit (role ×
> route × expected status), and to the cross-tenant adversarial matrix. Bodies are Pydantic with `extra="forbid"`.
> POSTs that create work accept `Idempotency-Key` (8–80 chars); a replay returns the first result.
> Errors: `403 care_not_enabled`, `403 missing_permission`, `409 care_wind_down`, `409 cc_conflict` (stale CAS),
> `422 cc_param_out_of_bounds`, `422 cc_no_live_consent`, `404` for anything outside the caller's clinic or branch.

## 1. Setup and settings (CC_ADMIN)
| Method | Path | Notes |
|:-|:-|:-|
| GET | `/admin/cc/setup` | checklist (W1) with each item's state and the fix link; includes legacy-reminder count (L2) |
| GET / PUT | `/admin/cc/settings` | `cc_settings`; CAS on `updated_at`; clinic-wide logins only |
| POST | `/admin/cc/templates/submit` | submit CC templates to the clinic's WABA (dental pattern) |
| GET | `/admin/cc/templates` | statuses from Meta |
| GET / POST / PUT / DELETE | `/admin/cc/measure-sources[/{id}]` | lab parameter → measure mapping |
| GET / PUT | `/admin/cc/cards/{card_key}/{lang}` | approved safety/education texts (versioned; PUT creates a new version) |

## 2. Protocol activations
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/cc/protocols` | CC_VIEW | packs available in code with versions, params, sources |
| POST | `/admin/cc/activations` | CC_ADMIN | draft `{protocol_code, version, params}`; bounds checked |
| POST | `/admin/cc/activations/{id}/sign` | CC_APPROVE (doctor with registration no.) | → `active`, retires the previous one |
| POST | `/admin/cc/activations/{id}/retire` | CC_ADMIN | refused while enrollments use it unless `migrate_to` given |

## 3. Enrollments, consent, plans
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/cc/enrollments?status=&protocol=&doctor_id=&q=` | CC_VIEW | paged, phones masked |
| POST | `/admin/cc/enrollments` | CC_APPROVE (doctor) or CC_COORDINATE (creates `awaiting_doctor`) | `{patient_id, family_member_id?, protocol_code, primary_doctor_id, channel_pref, language}` → `invited` + consent request |
| POST | `/admin/cc/enrollments/{id}/consent/counter` | CC_COORDINATE | counter-signed consent `{notice_version, guardian_relation?}` |
| POST | `/admin/cc/enrollments/{id}/pause` · `/resume` · `/end` | CC_COORDINATE (end with clinical reason: CC_APPROVE) | reason required |
| GET | `/admin/cc/enrollments/{id}` | CC_VIEW | plan, timeline, latest evaluation, open cases |
| GET | `/admin/cc/enrollments/{id}/plan/preview` | CC_VIEW | what a rebuild from the latest signed Rx would produce (read-only) |
| POST | `/admin/cc/enrollments/{id}/plan/rebuild` | CC_COORDINATE | rebuild now (normally automatic, W3) |
| GET | `/admin/cc/enrollments/{id}/observations?measure=&from=&to=` | CC_VIEW | chart data |
| POST | `/admin/cc/enrollments/{id}/observations` | CC_COORDINATE | staff-entered reading (`source=coordinator_entered`) |
| POST | `/admin/cc/observations/{id}/correct` | CC_COORDINATE | new row with `corrects_id` + reason (ADR-CC-12) |

## 4. Radar, cases, doctor inbox, actions
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/cc/radar?reason=&min_score=&doctor_id=` | CC_VIEW | rows with "why" |
| POST | `/admin/cc/radar/{id}/dismiss` | CC_COORDINATE | reason required; audit |
| GET | `/admin/cc/cases?status=&tier=&assigned=me` | CC_VIEW | SLA countdown |
| POST | `/admin/cc/cases/{id}/acknowledge` · `/assign` · `/note` · `/escalate` · `/close` | CC_COORDINATE | CAS on `updated_at` |
| POST | `/admin/cc/cases/{id}/request-doctor` | CC_COORDINATE | → `awaiting_doctor` |
| GET | `/admin/cc/inbox` | CC_APPROVE | the doctor's `awaiting_doctor` cases (own patients first) |
| POST | `/admin/cc/cases/{id}/actions` | CC_APPROVE for clinical codes; CC_COORDINATE for `call_patient_now`, rebook of an existing visit | `{action_code, params}`; creates the booking/order through existing services; idempotent |
| GET | `/admin/cc/actions?status=` | CC_VIEW | booked / verified / not verified |

## 5. Outcomes and export
| Method | Path | Perm |
|:-|:-|:-|
| GET | `/admin/cc/outcomes?protocol=&from=&to=` | CC_VIEW |
| GET | `/admin/cc/outcomes/export.csv` | CC_ADMIN (phones masked; audit row) |

## 6. Conversation and voice (no HTTP surface)
- `conversation.py` hook (one block, after the `rx_refill:` hook, for `button` **and** `interactive` types):
  `cc_hooks.handle_tap(manager, clinic, phone, tap_id)` for ids starting `cc:`;
  `cc_hooks.handle_typed_reading(manager, clinic, phone, session, text)` under ADR-CC-09's four conditions. Both return
  `True` only when they consumed the message; any exception inside is logged and returns `False`, so the message
  continues through the normal pipeline (a CC bug can never swallow a patient's message).
- Voice: `voice/dialog.py` routes `purpose == 'care_checkin'` to `CareCheckinScript`; results posted through
  `cc_observations.record_voice(...)`.

## 7. Platform (owner)
| Method | Path | Notes |
|:-|:-|:-|
| PATCH | `/platform/clinics/{id}/features` `{feature:"care_enabled", enabled, confirm_disable}` | eligibility 400 (`care_requires_opd`); disable with active enrollments → 409 preview; confirm → WIND_DOWN |
| PUT | `/platform/clinics/{id}/config` `care_message_limit` | monthly `care` quota (dental limits pattern) |
| GET | `/platform/cc/overview` | per clinic: state, active enrollments, prompts this month, template share, answered %, verified actions |

## 8. `/admin/me` additions
`cc_access` (`ACTIVE`/`WIND_DOWN`/null), `cc_state`, `cc_perms` (list), `cc_is_doctor_signer` (bool).
