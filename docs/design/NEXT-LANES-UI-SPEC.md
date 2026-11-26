# Kriya — UI & UX Specification for the next lanes (CC · RI · MI · RXR · SP · Owner console)

> **Binding for every screen the new lanes add.** The goal: a new page must be indistinguishable from the best
> existing pages (Pharmacy, Diagnostics, OPD) in colour, type, spacing, icons and motion, and smoother to use.
> **Source of truth is the live code**, not older docs: tokens in `admin/index.html:19-162` (dark `:root`, light
> `:root[data-theme="light"]`), components in the same file, owner console in `admin/platform.html`.
> The general system is described in [KRIYA_AI_DESIGN_SYSTEM.md](KRIYA_AI_DESIGN_SYSTEM.md) (Oct 2026); where it and
> the live code differ, §1.2 lists the drift and the live value wins.

## 1. Foundations

### 1.1 Tokens — use these, invent nothing
| Purpose | Token (dark → light both defined live) | Never |
|:-|:-|:-|
| Page ground | `--bg` | a hex value |
| Glass containers | `--surface` (cards), `--surface2` (inputs, table heads, tiles), `--surface3` (nested) | new rgba surfaces |
| Lines | `--border`, `--border2` (hover/active), `--rim` | custom borders |
| Text | `--text-strong` (titles, numbers), `--text` (body), `--text2` (labels, secondary **and any small text that carries information**), `--text3` (decorative meta only — see §6.2) | grey hex |
| Interactive | `--accent`, `--accent2`, `--accent-glow`, `--accent-soft`, `--accent-soft2`, `--accent-row`, `--accent-border` | |
| Status | `--green/-bg/-border/-border-strong/-soft`, `--red/-bg/-border/-border-strong/-soft`, `--amber/-bg`, `--blue/-bg`, `--cyan`, `--pink`, `--muted-soft` | new status colours |
| Brand | `--brand-grad` (logo, thin accent lines, progress fills — decoration only) | gradients elsewhere |
| Fills that carry white text (from [A11Y-CONTRAST-PLAN.md](A11Y-CONTRAST-PLAN.md)) | `--action-grad` (primary buttons), `--accent-fill` (selected states, counts), `--red-fill` (critical / urgent tags), `--green-fill` (done tags) | white text on `--accent`, `--red`, `--green` or `--brand-grad` |
| Shape | `--radius` 14px (tiles, stats), `--radius-lg` 18px (cards), `--radius-xl` 22px (modals); 10px inputs/buttons; 8px small buttons; 999px chips | new radii |
| Elevation | `--shadow`, `--shadow-lg` | new shadows |
| Motion | `--motion-fast` 150ms, `--motion-base` 250ms, `--ease-out` | other easings, bounce |
Amber has no border token: derive it the way `.dx-chip.off` does (`color-mix(in srgb, var(--amber) 35%, transparent)`).
Typeface is IBM Plex Sans (already loaded); numbers in tables, tiles and charts use `font-variant-numeric: tabular-nums`.

### 1.2 Drift between the Oct design doc and the live panel (live wins)
| Item | Design doc | Live (`admin/index.html`) |
|:-|:-|:-|
| Page title `.page-head h2` | 1.6rem / 600 | **1.5rem / 700** (`:807`) |
| Stat number `.stat .num` | 2rem / 600 | **2.2rem / 800** (`:852`) |
| Icons | "22-icon symbol sprite" | **three mechanisms** (§3): inline Lucide SVG in nav, `ICONS` registry + `icon(name)` (`:10337`, `:10358`), 25-symbol `<symbol id="i-…">` sprite |
| Owner console radius | 14/18/22 | `admin/platform.html` uses `--radius: 16px` and is **dark-only** (no light theme block) |
Fix the design doc in the same commit as the first new page (one table update), so the next team reads the truth.

### 1.3 Semantic mapping for the new concepts (one meaning → one token, everywhere)
| Concept | Colour | Chip/badge | Words always shown |
|:-|:-|:-|:-|
| CC tier **urgent** | `--red` | `.badge` red variant (`.badge-failed` look); a solid tag uses `--red-fill` with white text | "URGENT" |
| CC tier **watch** | `--amber` | amber chip; **light theme: text `--text-strong`, amber dot + amber border** (§6.2) | "WATCH" |
| CC tier **stable** | `--green` | `.badge-active` look | "STABLE" |
| Routine / info / candidate | `--blue` | `.badge-completed` look | "ROUTINE" / "CANDIDATE" |
| Verified (recorded) | `--green` | `.dx-chip.ok` | "Verified" |
| Not verified / overdue | `--amber` | `.dx-chip.off` | "Not recorded" |
| RI **leakage** | `--red` | red chip | "Not billed" |
| RI **review** | `--amber` | amber chip | "Check" |
| RI **integrity** | `--pink` | pink outline chip | "Integrity" |
| RI **opportunity** | `--blue` | blue chip | "Opportunity" |
| ₹ recovered | `--green` text on tiles | — | "Recovered ₹…" |
| Consent live / withdrawn | `--green` / `--muted-soft` | `.badge-active` / `.badge-inactive` | "Consented" / "Withdrawn" |
| SLA left / breached | `--text2` / `--red` | countdown text, red when breached | "2 h 10 m left" / "Overdue 35 m" |
Colour is never the only signal: every chip carries its word; charts carry labels and a table alternative.

## 2. Layout and components (reuse, in this order)

### 2.1 Page anatomy (every new page)
```
<div id="pg-<page>" class="sec">                      ← fades in 180 ms (existing .sec)
  .page-head  → h2 (≤ 4 words) + p (one sentence, what this page is for) + right-aligned controls (.field selects, .btn-ghost.btn-sm Refresh)
  [wind-down banner when the module is in WIND_DOWN — reuse .ipd-winddown]
  tiles row   → .rxd-tiles / .rxd-tile pattern (auto-fill minmax(190px,1fr), 12px gap, clickable filters)
  .card       → .card-head h3 + content (table / chart / form)
</div>
```
Spacing: 4px grid only (4/8/12/16/20/24/28/32). One primary `.btn.btn-accent` per view; everything else `.btn-ghost`
(add `.btn-sm` inside cards and tables). Never more than three action buttons visible on one row.

### 2.2 Component reuse table
| Need | Use | Notes |
|:-|:-|:-|
| KPI tiles that filter the table below | `.rxd-tiles` + `<button class="rxd-tile">` | number in `--text-strong` tabular, label `--text2`; selected tile: `border-color: var(--accent)` + `aria-pressed="true"` |
| Headline stats (Outcomes) | `.stats` + `.stat` (`.glow` colours follow nth-child order — order tiles as accent, green, red, amber, cyan, pink) | max 6 |
| Tables | `.tbl-wrap` + `table.dx-table` with `data-label` on every `td` | container-query card collapse below 1120 px is automatic (`:1891`); never horizontal scroll at 390 px |
| Status chips | `.dx-chip` (+ `.ok`, `.off`) and `.badge-*` | new variants only as `.cc-chip.urgent/.watch/.stable` composed from tokens (§1.3) |
| Segmented filter (Urgent / Watch / Routine; Today / 7 d / 30 d) | `.toggle-group` | ≤ 5 segments; `aria-pressed` |
| Side detail without losing the list | `.rx-drawer` pattern (fixed right, `min(640px,100vw)`, header with h3 + close) | Esc closes, focus returns to the row |
| Confirm / short forms | `.modal-overlay` + `.modal` (520 px) | destructive confirms name the patient and the effect |
| Forms | `.field` (label above input), `.dx-grid` (responsive columns), `.dx-actions` (right-aligned buttons), `label.check` | |
| Checklists (setup pages) | `.dx-checklist` | each item: state chip + one-line fix link |
| Feedback | `toast(text, err)` (`:21354`) | success ≤ 6 words; errors say what to do next |
| Live regions | `aria-live="polite"` on every async container; `role="status"` on filter results | as `rxdTiles` |
| Charts | Chart.js already vendored: `<script defer src="/panel-assets/chart.umd.min.js">` (`:15`) | §5 |
| Navigation | `.nav-group` + `.nav-group-head` (button, `aria-expanded`) + `.nav-link` (`tabindex="0"`, `data-page`, `data-cc="nav PERMS"`, `style="display:none"`) | gates as RX/DX |

### 2.3 New CSS rules
- Lives only inside `/* CC:<lane>:START */ … /* CC:<lane>:END */` (likewise `RI:`, `MI:`, `RXR:`, `SP:`), prefixed `.cc-`, `.ri-`,
  `.mi-`, `.sp-`; composed only from §1.1 tokens; checked in both themes; no `!important`; no element selectors.
- No new keyframes unless an existing one cannot serve; every transition ≤ `--motion-base` with `--ease-out` and
  disabled under `@media (prefers-reduced-motion: reduce)`.

## 3. Iconography

### 3.1 Which mechanism where (match the house)
| Where | Mechanism | Exact form |
|:-|:-|:-|
| Sidebar group heads and nav links | **inline SVG**, Lucide outline geometry | `<span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">…</svg></span>` — copy of the RX nav markup (`admin/index.html` ~`:4131-4186`) |
| Inline status / toast / empty-state glyphs | `icon(name, size)` from the `ICONS` registry | add new names to `ICONS` only inside a `/* CC:icons */` block |
| Buttons in the owner console | sprite `<svg class="i"><use href="#i-…"/></svg>` | add `<symbol>`s to `admin/platform.html`'s sprite |
Rules: one Lucide version for all new icons (record it in the CSS comment; Lucide is ISC-licensed); outline only, never
filled; 20 px in nav, 16 px inline, 18 px in buttons; `currentColor` so state colours flow; never an emoji in admin
chrome (emoji stay in patient WhatsApp texts only); never reuse a glyph already meaning something else in the sidebar.

### 3.2 New icons (Lucide names; checked against the existing sidebar glyphs)
| Item | Lucide icon | Why distinct |
|:-|:-|:-|
| **Care** group head + Care setup | `heart-pulse` | no heart glyph in the panel today |
| Care radar | `radar` | Leads already uses concentric circles (target) |
| Coordinator queue | `list-checks` | |
| Doctor inbox | `inbox` | |
| Patients in care | `user-check` | Referrers uses `users` |
| Outcomes | `chart-column-increasing` | DX Dashboard uses the line-chart shape |
| Revenue Guard | `receipt-indian-rupee` | |
| Switch to Kriya (import wizard) | `import` | |
| Optical jobs (eye) | `glasses` | |
| Dental lab orders | `package-check` | Purchase uses a truck |
| IVF cycles / Embryology / Cryo | `sprout` / `microscope` / `snowflake` | |
| Antenatal / Immunisation | `baby` / `syringe` | |
| Clinical media | `images` | |
| Packages & instalments | `layers` | |
| Estimates | `calculator` | |
| Camps | `tent` | |
| Pharmacy customers / order requests (retail plan) | `contact` / `clipboard-list` | |
Icon names differ between Lucide releases (e.g. `line-chart` → `chart-line`); confirm at copy time against the pinned version.

## 4. Interaction and motion (the "smooth" part)
1. **Never lose context.** Lists open details in the right drawer; closing returns focus to the same row and scroll
   position. Filters live in the URL hash (`#ccradar?reason=lab_overdue`) so back/forward and reload keep them.
2. **Every action is one obvious click, then visible.** Button text says the effect ("Book review — Tue 3 Dec 10:20");
   the button disables and reads "Booking…" during the request; on success the row updates in place with a 150 ms
   background fade (`--accent-row` → transparent) and a toast; on 409 the row reloads and the toast explains.
3. **Keyboard first for work queues** (Coordinator queue, Doctor inbox, Revenue Guard): `J`/`K` next/previous,
   `Enter` open, `A` acknowledge, `D` request doctor, `N` note, `Esc` close, `?` shows the key map overlay (the RX POS
   overlay pattern). Shortcuts never fire inside inputs.
4. **Quiet freshness.** Queues and radar refresh every 60 s only while the page is visible
   (`document.visibilityState`), never steal focus, and announce "3 new cases" through the live region.
5. **Loading:** keep the previous content visible and dim it (`opacity .6`, `aria-busy="true"`) during refresh; first
   load shows the existing muted "Loading…" line inside the card (no skeleton component exists — do not add a new
   pattern for one lane). **Empty:** one sentence of what will appear here and the one action that fills it.
   **Error:** what failed + "Try again" button; never a raw error string.
6. **Motion budget:** page switch = existing 180 ms fade; drawer = 250 ms `--ease-out` slide; tile hover = border colour
   only; no parallax, no bounce, nothing animates under reduced motion.
7. **No layout shift:** reserve chart heights (240 px desktop, 180 px mobile), fixed tile heights, tabular numbers.

## 5. Charts (Chart.js, already vendored)
- Colours read from tokens at render time (`getComputedStyle(document.documentElement).getPropertyValue('--accent')`).
- **Theme switch repaint:** `toggleTheme()` repaints only Insights and Corporate charts today (`admin/index.html:10321-10333`).
  Each new chart page adds **one guarded line** there inside `/* CC:theme */` (`if (cc.visible()) cc.repaint();`) —
  the only edit to an existing function the UI needs; listed in each lane's ARCHITECTURE §7.
- Target band without a plugin (none is vendored): two invisible datasets (low/high) with `fill: '-1'` in `--green-bg`;
  thresholds as dashed lines (`borderDash: [4,4]`) in `--amber` / `--red`; points coloured by tier.
- Destroy before re-render (the `insightCharts` pattern, `:21837-21862`); `animation: false` under reduced motion.
- Every chart has a "Show as table" toggle with the same numbers (screen readers, 390 px, print).
- Axes: dates as "3 Dec"; values with unit in the axis title; tooltip "Tue 3 Dec · FBS 142 mg/dL · typed on WhatsApp".

## 6. Responsive, accessibility, content

### 6.1 Breakpoints (live ones)
1100 / 900 / 760 / 640 / 480 px media queries plus container queries for tables. Check every page at **1440, 1024,
768 and 390 px**, dark and light. At ≤ 760 px: tiles become 2 per row, the drawer is full-width, primary action sits in
a sticky bottom bar, touch targets ≥ 44 × 44 px.

### 6.2 Contrast (WCAG 2.x; small text needs ≥ 4.5:1)
First pass measured on `--surface-solid`; the approved plan [A11Y-CONTRAST-PLAN.md](A11Y-CONTRAST-PLAN.md) re-measured on
the translucent surfaces **as rendered** — dark `--text3` also fails there (3.66 on `--surface3`, 3.82 on `--surface2`).
The table below is the first pass; the plan's §2 is authoritative.
| Pair | Dark | Light | Rule for new UI |
|:-|:-|:-|:-|
| `--text` / `--text2` | 15.11 / 8.36 | 15.60 / 6.17 | use freely |
| `--text3` | 4.69 (3.66–4.21 on translucent surfaces) at measurement time | **3.99** at measurement time | **A11Y-02 landed** (`--text3` dark `#7E97A3`, light `#536A75`; guarded by `tests/test_ui_contrast.py`) — the first-pass numbers here are historical; new UI may use `--text3` for secondary text within the plan's §2 limits, `--text2` stays the default for information |
| status chips green / red / blue / cyan / pink | 5.82 / 4.66 / 4.53 / 7.76 / 5.64 | 4.78 / 4.65 / 4.59 / 4.67 / 5.91 | pass |
| amber chip | 7.06 | **4.36** | light theme: chip text `--text-strong` (15.86) with amber dot + border |
| white on `.btn-accent` (`--brand-grad`) | **2.54–3.33** | 3.77–5.27 | pre-existing, see §9 |

### 6.3 Accessibility checklist
Labels on every input; `aria-expanded` on group heads and drawers; focus-visible ring 2 px `--accent` (existing);
`Esc` closes; focus trapped in modals/drawers; live regions for async; tables have `<th scope>`; charts have table
alternatives; patient-entered values and staff notes rendered with `textContent` / `esc()`; no information only by
colour; Hindi/Telugu names render (Noto fallback already in the stack via system fonts — verify on Windows).

### 6.4 Content and microcopy
Sentence case; verbs on buttons; numbers tabular; money `₹1,23,456.50` (Indian grouping, `toLocaleString('en-IN')`);
dates "Tue 3 Dec 2026", times "10:20 am" IST; relative time with the absolute time in `title`; phones masked
`+91XXXXXX7890` with the audited reveal; no internal codes on screen (show "Lab overdue", not `lab_overdue`); clinical
words exactly as the doctor wrote them; never "AI decided" — say "Protocol rule: FBS above 130 on 3 of 5 days".

## 7. Page blueprints

### 7.1 Coordinator queue (`ccqueue`) — 1440 px
```
┌ Care queue ───────────────────────────── [Branch ▾] [Mine ◻] [Refresh] ┐
│ [ URGENT 3 ] [ WATCH 12 ] [ ROUTINE 21 ] [ SLA BREACHED 1 ]   ← rxd-tiles │
├ card: toggle-group Urgent|Watch|Routine|All ─────────────────────────────┤
│ ▲ Patient            Pack   Why (protocol rule)            SLA     Owner │
│ ● URGENT Lakshmi R.  T2DM   FBS 52 on 3 Dec · hypo card sent 2h left Me  │
│   MRN 1043 · +91XXXXXX7890                         [Acknowledge][Open ›]  │
└──────────────────────────────────────────────────────────────────────────┘
Drawer (640 px): header (name, MRN, age, pack, consent chip) · "Why" list · 30-day chart · timeline · actions
(Call · Note · Request doctor · Close) — primary action depends on state (one only).
```
390 px: tiles 2 per row; each case is a card (container query); actions in a sticky bottom bar inside the drawer.

### 7.2 Doctor inbox (`ccinbox`)
One case per card, newest urgent first. Card: name · MRN · age/sex · pack chip · reasons with values and dates ·
chart (reading vs target band) · adherence strip (14 squares, ✓/✕ with `aria-label`) · "Current plan (Rx 28 Nov, Dr
Rao)" · **large action buttons** in one row: `Book review` (primary, shows the first free slot) · `Order HbA1c` ·
`Recheck in 3 days` · `Continue plan` · overflow menu (Refer, Call now). After a tap the card collapses to a one-line
receipt ("Booked Tue 3 Dec 10:20 · Undo within 10 s" — undo cancels the booking through the normal cancel path).

### 7.3 Patient care timeline (drawer or full page)
Header strip → charts row (glucose, BP — each 240 px, target band) → adherence heatmap (weeks × doses) → list of
prompts, observations (source icon: WhatsApp / voice / lab / staff), evaluations ("why"), cases and actions, plan
versions with "from prescription dated …" link. Filters: 30 d / 90 d / all (`toggle-group`).

### 7.4 Care radar (`ccradar`)
Tiles per reason (Overdue follow-up, Missed visit, Lab overdue, Refill lapsed, Off target, Not responding,
Candidates) → table sorted by score with a "why" column; row actions: Invite / Open case / Dismiss (reason modal).

### 7.5 Protocols (`ccprotocols`) and Care setup (`ccsetup`)
Protocols: pack cards (name, version, status chip, signed by/at). Activation form: one row per parameter — label,
number input with unit suffix, `min–max` hint, "Source: ADA 2025 §6" link, diff vs current in `--text2`; sticky footer
"Save draft" (ghost) / "Sign as Dr …" (primary, only for signers). Setup: `.dx-checklist` with red/amber/green chips and
a fix link per item; templates table with Meta status chips; messaging-budget card (tier, quality, today's share bar —
existing `.usage-bar`).

### 7.6 Outcomes (`ccoutcomes`)
`.stats` row (Enrolled · Return rate vs baseline · Labs on time · At target · Verified ₹ · Answered %) → funnel card
(horizontal bars, no plugin) → per-pack table → method note (version, "associated with, not caused by") → Export CSV.

### 7.7 Revenue Guard (`riguard`)
Tiles: Not billed yesterday ₹ · Open > 7 days ₹ · Recovered this month ₹ · Integrity issues → table grouped by rule
with "Bill now ›" deep links (opens the existing bill screen in place) → dismissed list (reason, by, ₹). Dashboard tile
on the main dashboard reuses `.stat`.

### 7.8 Switch to Kriya wizard (`miwizard`, inside Data & Support)
Left: vertical stepper (doctors → services → packages → payers → patients → history → dues → appointments), each with
state chip. Right: template download · drop zone (keyboard-accessible button + drag area) · dry-run result tiles
(rows OK / errors / duplicates) · error table (row, column, problem, fix) · Apply (primary) · Undo batch (ghost,
confirm modal). Progress persists; the owner console shows the same stepper read-only.

### 7.9 Specialty screens (SP)
- **Eye exam form** inside the consultation: two columns OD | OS with aligned rows (VA, refraction SPH/CYL/AXIS/ADD,
  IOP, slit lamp, fundus); steppers with 0.25 D increments and keyboard arrows; "copy OD → OS"; computed values
  (logMAR, spherical equivalent) shown read-only in `--text2`.
- **Optical job board:** columns Advance paid → At lab → Ready → Delivered (cards, not drag-and-drop on mobile —
  status buttons instead); "Ready" sends the patient message after a confirm.
- **Odontogram:** FDI grid (adult 18…11 | 21…28 / 48…41 | 31…38; primary set toggle), each tooth a 44 px button with
  surface segments; legend chips; selection feeds the treatment-plan form; full keyboard (arrows move, Space toggles).
- **IVF cycle chart:** day columns × rows (drugs as signed, follicles L/R, E2/LH/P4, endometrium); trigger time card
  pinned at the top with acknowledgement state.
- **Antenatal & immunisation:** pregnancy timeline (weeks 0–40 with visits/scans/tests as markers), vaccine schedule
  table by age with due/given/overdue chips and batch numbers.

### 7.10 Owner console additions (`admin/platform.html` — dark-only, its own tokens)
Segment tabs above the leaderboard (All · Hospitals & clinics · Specialty · Diagnostics · Pharmacies) styled like the
existing filter selects; KPI strip per segment with the console's existing stat cards; module toggles keep the
existing switch markup with an eligibility line in `--text2` under each; Pharmacy wizard as a three-step modal
(Business → Shops & licences → Review). Icons through the console's sprite (`#i-…`), new symbols added there.

### 7.11 Patient-facing WhatsApp (same visual language as the existing flows)
Header ≤ 60 chars (clinic name); body short lines, one leading emoji at most (house style, e.g. "💊 Today's medicines");
footer "Reply STOP CARE to stop"; reply buttons ≤ 3, titles ≤ 20 chars ("Taken", "Missed", "Send reading"); list rows
≤ 24-char titles; all three languages fit the limits (test fixtures check lengths); never a value, diagnosis or dose
in a template body.

## 8. UI acceptance gates (in every lane's Definition of Done)
| Gate | Check |
|:-|:-|
| UI-G1 | Only §1.1 tokens; `grep` of the lane's CSS block finds no hex/rgb literals except inside `color-mix()` of tokens |
| UI-G2 | Screenshots at 1440 / 1024 / 768 / 390 px in dark and light (owner console: dark) attached to the status log |
| UI-G3 | No horizontal page scroll at 390 px; tables collapse to cards; touch targets ≥ 44 px |
| UI-G4 | Keyboard-only pass: every action reachable; focus visible; Esc closes; focus returns |
| UI-G5 | Screen-reader pass (VoiceOver or NVDA): page title, live regions, chip words, chart tables |
| UI-G6 | Loading, empty, error and wind-down states seen for every list |
| UI-G7 | Contrast: new pairs ≥ 4.5:1 (small) / 3:1 (large, UI components) in both themes |
| UI-G8 | Icons per §3 (correct mechanism, Lucide version noted, no duplicate meaning) |
| UI-G9 | Theme toggle repaints the page's charts; reduced motion disables animation |
| UI-G10 | Long and Indic names (40-char Telugu name), 1,000-row list, slow network (throttled) — no layout shift, no jank |
| UI-G11 | Hidden for clinics without the module: nav, tiles, entry points; `go()` refuses the page |
| UI-G12 | Browser console clean; no request fires for a hidden module |

## 9. Pre-existing panel-wide accessibility items — **APPROVED 2026-11-24, planned in [A11Y-CONTRAST-PLAN.md](A11Y-CONTRAST-PLAN.md)**
The plan supersedes the candidate colours below (A11Y-2 needed `#536A75` light / `#7E97A3` dark to pass on every
surface; `#587078` fails on `--surface3`) and adds A11Y-4…6 found while planning.
| ID | Finding | Evidence | Proposed fix (changes every page — needs approval) |
|:-|:-|:-|:-|
| A11Y-1 | White text on the primary button gradient is 2.54–3.33:1 (dark) / 3.77–5.27:1 (light) — below 4.5:1 for 0.78–0.88 rem text | `.btn-accent { background: var(--brand-grad); color:#fff }` (`admin/index.html:644`) | a button-only gradient token with deeper stops, e.g. `#047857 → #0E6FA8 → #1668D6` (5.27–5.48:1), keeping `--brand-grad` for the logo |
| A11Y-2 | Light-theme `--text3` #6B838E is 3.99:1 on white, 3.69:1 on `--bg` — used for table headers and labels | `admin/index.html:128` | `#587078` (4.85:1 on `--bg`, 5.24:1 on white) |
| A11Y-3 | Light-theme amber text on amber background is 4.36:1 | `--amber: #A65E07` | `#94570A` (5.15:1) |
New lanes are built on the fixed tokens: land A11Y-00…07 before the first new screen, so old and new pages improve together.
