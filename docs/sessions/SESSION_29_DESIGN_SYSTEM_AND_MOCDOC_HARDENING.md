# Session 29 — Design System Specification & MocDoc Worker Hardening

**Date:** 2026-10-01 → 2026-10-04  
**Commits:** `812287a..b88bc7b` (2 commits on `main`)  
**Scope:** Design system documentation, admin panel refinements, MocDoc connector hardening, provider report routing  
**Migration:** None (documentation + code refinement session)

---

## 1. Summary

This session had two tracks:

1. **Design System Specification** — Extracted the complete "Clinical Depth" UI/UX design language from the live admin and owner panels into a standalone, exportable reference document. This serves as the canonical visual standard for all current and future Kriya AI products.

2. **MocDoc Connector & Admin Panel Hardening** — Selector updates for the MocDoc diagnostic sync worker, provider report routing fixes, and admin panel UI refinements.

---

## 2. Documents Created

### 2.1 Design System Specification (NEW)

**File:** [`docs/design/KRIYA_AI_DESIGN_SYSTEM.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/design/KRIYA_AI_DESIGN_SYSTEM.md)  
**Lines:** 1,043  
**Purpose:** Complete UI/UX design system specification for the entire Kriya AI product suite.

**12 sections covering:**

| Section | Content |
|:--|:--|
| **1. Brand Identity** | Kriya Mark SVG anatomy (hexagon + K letterform), 4 gradient definitions, size specs, "Clinical Depth" philosophy |
| **2. Colour System** | 40+ CSS custom property tokens for dark theme (`#040A11` navy) and light theme (`#F2F7F7` mint-white) |
| **3. Typography** | IBM Plex Sans at 5 weights, 14-element type scale with size/weight/tracking/line-height per element |
| **4. Spatial System** | 4px base grid, 3-tier radius hierarchy (14/18/22px), layout constants (sidebar 260px, modal 520px) |
| **5. Surface & Glass Model** | `backdrop-filter: blur(20px) saturate(155%)` + mint rim recipe, `@supports` fallback for non-blur browsers |
| **6. Background & Aura System** | Emerald + sapphire radial gradient aura definitions for login, main content, and owner panel |
| **7. Component Library** | 12 component patterns: buttons (accent/ghost/semantic), inputs, checkboxes, toggles, cards, tables, badges, modals, toasts, navigation, search bars, progress bars |
| **8. Iconography** | 22 stroke SVG icons at 1.75px weight, symbol sprite system, usage rules |
| **9. Motion & Animation** | 7 animation keyframes + 3 timing tokens + reduced-motion support |
| **10. Responsive Framework** | 6 breakpoints (480–1441px), sidebar collapse behaviour, hamburger button |
| **11. Accessibility** | Focus rings, WCAG contrast ratios (up to AAA), colour-scheme declaration, checkbox contrast compliance |
| **12. Product Extension** | Step-by-step guide for applying the system to any new Kriya AI panel |

### 2.2 Taiyo Labs Corporate Health Proposal (previous session, part of same conversation)

**File:** [`docs/proposals/KRIYA_AI_TAIYO_LABS_CORPORATE_HEALTH_PROPOSAL.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/proposals/KRIYA_AI_TAIYO_LABS_CORPORATE_HEALTH_PROPOSAL.md)  
**Lines:** 260  
**Purpose:** Enterprise partnership proposal for Taiyo Labs corporate health program operated by Kriya AI.

### 2.3 Master Platform Specification (previous session, part of same conversation)

**File:** [`docs/architecture/KRIYA_AI_MASTER_PLATFORM_SPECIFICATION.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/architecture/KRIYA_AI_MASTER_PLATFORM_SPECIFICATION.md)  
**Lines:** 363  
**Purpose:** End-to-end platform specification covering what Kriya AI is, how it works, and how it differs from conventional chatbots.

---

## 3. Files Changed — Pin to Pin

### 3.1 Commit `812287a` — Home Sample Collection

> This was the tail-end of Session 28 work being pushed. Full details in [`SESSION_28_HOME_SAMPLE_COLLECTION.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/sessions/SESSION_28_HOME_SAMPLE_COLLECTION.md).

**New files (15):**

| File | Lines | Purpose |
|:--|:--|:--|
| `app/routers/home_collection.py` | 415 | HTTP endpoints for phlebotomist portal, dispatch, status updates |
| `app/services/home_collection.py` | 595 | Core home collection service: assignment, dispatch, status transitions |
| `app/services/home_collection_flow.py` | 585 | WhatsApp conversation flow: location intake, address, slot selection |
| `migrations/097_home_sample_collection.sql` | 81 | Schema: 12 new columns on `appointments`, `home_collection_fee_paise`, `phlebotomist_id` |
| `migrations/rollback/097_down.sql` | 22 | Rollback script for migration 097 |
| `tests/test_home_collection.py` | 650 | 60+ unit tests for service layer |
| `tests/test_home_collection_conversation.py` | 264 | 30+ conversation flow tests |
| `docs/sessions/SESSION_28_HOME_SAMPLE_COLLECTION.md` | 156 | Session documentation |
| `docs/architecture/KRIYA_AI_MASTER_PLATFORM_SPECIFICATION.md` | 363 | Master platform spec |
| `docs/proposals/KRIYA_AI_TAIYO_LABS_CORPORATE_HEALTH_PROPOSAL.md` | 260 | Taiyo Labs proposal |

**Modified files (13):**

| File | Change |
|:--|:--|
| `admin/index.html` | +544 lines — Phlebotomist portal UI, dispatch dashboard, home collection visit cards, mobile-first layout |
| `app/main.py` | Mounted `home_collection` router |
| `app/models/message.py` | Added `location` field to message model for GPS pin intake |
| `app/routers/admin.py` | +108 lines — Admin endpoints for phlebotomist management, dispatch views |
| `app/routers/webhook.py` | +11 lines — Location message parsing in webhook handler |
| `app/services/conversation.py` | +89 lines — Home collection flow integration into FSM |
| `app/services/data_retention.py` | +13 lines — DPDP Act erasure paths for GPS coordinates and addresses |
| `app/services/payment.py` | +83 lines — Home collection fee calculation, payment hold integration |
| `app/services/permissions.py` | +10 lines — `phlebotomist` staff role, endpoint confinement |
| `app/services/scheduler.py` | +27 lines — Overdue home collection auto-reassignment job |
| `app/services/tenant.py` | +19 lines — Home collection config in tenant settings |
| `app/services/whatsapp.py` | +39 lines — Location request message, interactive location buttons |
| `docs/agent-context/*` | 6 agent context files updated with home collection references |

### 3.2 Commit `b88bc7b` — Design System + MocDoc Updates

**New files (1):**

| File | Lines | Purpose |
|:--|:--|:--|
| `docs/design/KRIYA_AI_DESIGN_SYSTEM.md` | 1,043 | Complete UI/UX design system specification |

**Modified files (5):**

| File | Delta | Change Description |
|:--|:--|:--|
| `admin/index.html` | +338 / -77 | Premium surface layer refinements: glass compositing, radius hierarchy, typography tightening, stat card layout (grid→flex), brand gradient rail on active nav, removed decorative glow blobs, tabular figure enforcement |
| `connectors/mocdoc/selectors.py` | +6 | New CSS selectors for updated MocDoc portal page structure |
| `connectors/mocdoc/worker.py` | +76 / -23 | Hardened report download flow: retry logic, selector fallbacks, error classification for intermittent page loads |
| `tests/test_mocdoc_worker.py` | +40 / -17 | Updated test fixtures to match new selector patterns and retry behaviour |
| `tests/test_provider_report_routing.py` | +1 / -1 | Provider routing assertion fix for updated mapping |

---

## 4. Total Impact

| Metric | Value |
|:--|:--|
| **Total files changed** | 33 |
| **New files created** | 16 |
| **Lines added** | 5,780 |
| **Lines removed** | 99 |
| **Net lines** | +5,681 |
| **New tests** | 90+ (home collection) |
| **New migration** | 097 (home sample collection) |
| **New documentation** | 4 documents (design system, platform spec, proposal, session doc) |

---

## 5. What Was NOT Changed

- **No changes to the clinical firewall** (`app/services/clinical_firewall.py`)
- **No changes to the AI system** (`app/services/ai_*.py`)
- **No changes to multi-tenancy core** (`app/tenancy.py`)
- **No database RLS policy changes**
- **No WhatsApp credential handling changes**
- **No scheduler lock mechanism changes**

---

## 6. Deployment Notes

- Migration `097_home_sample_collection.sql` must be applied before deployment (verified applied to Supabase — checksum `fb683e1e50d4ee109b311a3f8775d5ab2f6848ad2f1d91c9929918ed735dfb89`)
- Home sample collection is **disabled by default** — centres must opt in via their config
- The phlebotomist role is confined to the `/admin/home-collection/*` endpoints only
- MocDoc selector changes are backward-compatible; old selectors remain as fallbacks
