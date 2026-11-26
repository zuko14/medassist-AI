# Kriya Continuous Care — Admin UI

> **Visual and interaction rules are binding from [docs/design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md)**
> (tokens, components, icons, motion, contrast, blueprints §7.1–§7.6, gates UI-G1…G12). This file lists the CC pages,
> their gates and data, and the CC-specific details. Revised in design review round 2 (VERIFICATION §8).

## 0. Rules
- Code inline in `admin/index.html` inside `<!-- CC:<lane>:START — … -->` / `<!-- CC:<lane>:END -->` and
  `/* CC:<lane>:START */ … /* CC:<lane>:END */`; lanes: `foundation`, `protocols`, `enroll`, `timeline`, `radar`, `queue`,
  `inbox`, `outcomes`, `icons`, `theme`. `admin/admin.js` is never touched (AGENTS.md §1.7).
- Gating: every CC element carries `data-cc="nav PERM…"` and `style="display:none"`; `applyCcGates()` (copy of
  `applyRxGates`) runs after `/admin/me`; `go()` refuses a CC page whose nav link is hidden.
- Existing elements edited: the `loaders` map (one appended line per page, inside `/* CC:loaders */`), `toggleTheme()`
  (one guarded repaint line inside `/* CC:theme */`, UI spec §5), and the additive entry-point blocks in §3. Nothing else.
- Every list: loading, empty, error and wind-down states (UI spec §4.5); every action button disables during its request.

## 1. Navigation (group after Pharmacy)
`<div class="nav-group" data-nav-group="cc">` titled **Care**, group icon Lucide `heart-pulse` (inline SVG, RX markup).
The group count badge (`.nav-group-count`) shows open **urgent** cases; it turns `--accent` when a child page is active
(existing `.has-active` behaviour).

| Page id | Label | Icon (Lucide) | Gate | Loader |
|:-|:-|:-|:-|:-|
| `ccradar` | Care radar | `radar` | CC_VIEW | `ccradar.load()` |
| `ccqueue` | Care queue | `list-checks` | CC_VIEW | `ccqueue.load()` |
| `ccinbox` | Doctor inbox | `inbox` | CC_APPROVE | `ccinbox.load()` |
| `ccpatients` | Patients in care | `user-check` | CC_VIEW | `ccpatients.load()` |
| `ccoutcomes` | Outcomes | `chart-column-increasing` | CC_VIEW | `ccoutcomes.load()` |
| `ccprotocols` | Protocols | `heart-pulse` | CC_ADMIN, CC_APPROVE | `ccprot.load()` |
| `ccsetup` | Care setup | `heart-pulse` (setup pages reuse the group icon, like Pharmacy Setup) | CC_ADMIN | `ccsetup.load()` |

## 2. Pages (blueprints in the UI spec)
| Page | Blueprint | CC-specific content |
|:-|:-|:-|
| Care setup | spec §7.5 | W1 checklist; templates with Meta status chips; consent notice editor en/hi/te with version history; reply hours; emergency number; medication clock times; lab mapping table; **messaging budget card** (tier, quality chip, today's care share on `.usage-bar`, ADR-CC-17); legacy-reminder notice (L2) |
| Protocols | spec §7.5 | parameter rows with default, bounds, unit, source link; diff vs current; "Sign as Dr …" only for signers; activation history |
| Care radar | spec §7.4 | reason tiles; "why" column (e.g. "Follow-up due 21 Nov — no visit since"); Invite / Open case / Dismiss (reason) |
| Care queue | spec §7.1 | tier tiles + `toggle-group`; SLA countdown; drawer timeline; keyboard map (`J K Enter A D N Esc ?`) |
| Doctor inbox | spec §7.2 | reasons with values and dates; chart with target band; 14-day adherence strip; current signed plan link; one-tap actions with slot text; 10-second undo of a booking |
| Patients in care | spec §7.3 | list (status chip, pack, doctor, last reading, next due) → timeline; staff-entered reading and correction forms (reason required) |
| Outcomes | spec §7.6 | stats row, funnel, per-pack table, method note, CSV export |

## 3. Embedded entry points (additive blocks in existing pages)
| Existing page | Addition | Gate |
|:-|:-|:-|
| OPD consultation, signed state | ghost button "Enroll in care" → modal: pack picker (ICD-matched packs first), WhatsApp opt-in checkbox with the exact sentence (W2 step 0), channel, language | `cc_access` + `CC_APPROVE` |
| OPD patient profile | one-line strip in `--surface2`: "In care · T2DM · last FBS 142 (3 Dec) · HbA1c due 12 Dec" + chip | `cc_access` |
| IPD discharge | structured follow-up date + "30-day post-discharge care" check | `cc_access` + IPD |
| Front-desk queue | small `.dx-chip` "Care" on checked-in enrolled patients | `cc_access` |
| Pharmacy refill desk | `.dx-chip` "Care" on refills belonging to an enrollment | `cc_access` + `rx_access` |

## 4. CC-specific visual details
- Tier chips: `.cc-chip.urgent|.watch|.stable` per UI spec §1.3 (watch in light theme: `--text-strong` text, amber dot
  and border); words always visible.
- Source icons on observations (16 px, `icon()` registry, added in `/* CC:icons */`): WhatsApp tap, typed, voice,
  lab, staff-entered — each with an `aria-label`.
- Reading charts: glucose (mg/dL) and BP (systolic/diastolic two lines) with the activation's target band; points
  outside bounds coloured by tier; "Show as table".
- Adherence strip/heatmap: squares 12 px (desktop) / 10 px (mobile), ✓ `--green`, ✕ `--red`, unanswered `--muted-soft`,
  display-only items not shown; each square has an `aria-label` ("3 Dec morning: taken").

## 5. Owner console (`admin/platform.html`, dark-only)
Clinic detail modal: **Continuous Care** switch beside OPD/IPD/DX/RX (`toggleCc(this)`, same 409-preview flow as
`toggleRx`) with an eligibility line; monthly `care` limit field; overview card (active enrollments, prompts this
month, answered %, template share, quality chip). Details: [docs/specialty-os/OWNER-CONSOLE.md](../specialty-os/OWNER-CONSOLE.md).

## 6. Done means
UI-G1…G12 (UI spec §8) pass and the screenshots are linked in status §9.
