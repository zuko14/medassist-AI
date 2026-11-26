# Kriya — Panel-wide Contrast Fix Plan (lane **A11Y**)

> **Approved by the owner on 2026-11-24** (UI spec §9, A11Y-1…3). This plan widens the approval to the three dependent
> items found while planning (A11Y-4…6) — they are the same kind of change and one is a real light-theme bug.
> Scope: `admin/index.html`, `admin/platform.html`, `admin/queue-display.html`. CSS tokens and CSS rules only — **no
> JavaScript, DOM, API, database or PDF change.** Status: NOT_STARTED.

## 1. Goal and method
WCAG 2.2 AA: text ≥ 4.5:1 (≥ 3:1 for large text ≥ 24 px or ≥ 18.66 px bold), UI components and graphics ≥ 3:1.
Measured with the WCAG 2.x formula on the colours **as rendered**: Kriya's surfaces are translucent, so every
`--surface*` was alpha-composited over its theme's `--bg` before measuring (the first pass of the UI spec measured on
`--surface-solid` only and was too optimistic for dark `--text3` — corrected here and in the UI spec).

## 2. Findings (before → after)
| ID | What fails today | Worst ratio today | Where it shows | Fix | After |
|:-|:-|:-|:-|:-|:-|
| A11Y-1 | **White text on bright fills.** Primary buttons on `--brand-grad`; selected states on `--accent`; critical flags on `--red`; done steps on `--green`; notification badges `#ef4444` | 2.54 (white on #10B981, dark) · 2.17 (white on `--accent2` end of the selected service chip) · 3.33 (white on #3D8BFD) · 3.37 (white on #F2555F — **critical lab flags**) · 3.76 (badges) | every primary button, day picker, IPD tabs, nav group count, Ketu send, service chips, critical/STAT flags, OT steps, triage tags, unread badges, owner-console primary buttons | new fill tokens used only where white text sits on a colour: `--action-grad`, `--accent-fill`, `--red-fill`, `--green-fill` (§3) | ≥ 5.27 everywhere |
| A11Y-2 | **`--text3` too faint** for labels, table headers, meta | dark 3.66 on `--surface3`, 3.82 on `--surface2`; light 3.34 on `--surface3`, 3.69 on `--bg` | 376 uses in the clinic panel, 131 in the owner console | dark `#6E8794 → #7E97A3`; light `#6B838E → #536A75` | dark ≥ 4.51, light ≥ 4.76 on every surface; still clearly below `--text2` (6.81 / 5.58) so the hierarchy stays |
| A11Y-3 | **Light-theme amber text** | 4.15 on `--surface3`, 4.36 on its own tint | pending/no-show/watch chips, warnings (102 `var(--amber)` uses) | light `--amber #A65E07 → #94570A`, `--amber-bg → rgba(148,87,10,0.10)` | ≥ 4.84 on every surface, 5.02 on its tint |
| A11Y-4 | **Bug:** the Report Connector title is hard-coded white on `--surface2` — **invisible in light theme** | 1.12 | `#diagStatusTitle` (`admin/index.html:4473`) | `color: var(--text-strong)` | 15+ |
| A11Y-5 | **OPD stage pills / shift warning** (text on an 18 % tint of the same colour); light theme overrides only 3 of 7 stage tokens | light: vitals 1.88, registered 2.21, billing 2.32, cancel 2.96, consult 3.03, waiting 4.01; shift-closed message 1.93 · dark: done 3.09, cancel 3.96, waiting 4.12 | OPD queue, desk, workspace, billing pills; closed-shift bar | per-theme OPD stage tokens (§3) | ≥ 4.57 everywhere |
| A11Y-6 | **Dark text on amber fills in light theme** (triage "yellow", OT "urgent") — today 3.63, and it would get worse with the darker amber of A11Y-3 | 3.63 | `.emg-tag[data-t="yellow"]` (`:1745`), `.ot-prio[data-p="urgent"]` (`:1827`) | light theme only: white text on the new amber | 5.79 |

## 3. Exact token changes
### 3.1 `admin/index.html` — dark `:root` (`:19-107`)
```css
--text3: #7E97A3;                       /* was #6E8794 (:48) */
--opd-waiting: #5B9DFF;                 /* was #3D8BFD (:89) */
--opd-done: #8A98AB;                    /* was #64748B (:92) */
--opd-cancel: #F87171;                  /* was #EF4444 (:93) */
/* new — fills that carry white text (A11Y-1) */
--action-grad: linear-gradient(135deg, #047857 0%, #0E6FA8 55%, #1668D6 100%);
--accent-fill: #1A66D1;
--red-fill: #C62F3B;
--green-fill: #047857;
```
`--brand-grad` (`:55`) is **unchanged**: it keeps painting the logo, the 1–3 px accent lines, progress fills and the
nav indicator — decoration with no text on it.

### 3.2 `admin/index.html` — light `:root[data-theme="light"]` (`:109-162`)
```css
--text3: #536A75;                       /* was #6B838E (:128) */
--amber: #94570A;                       /* was #A65E07 (:143) */
--amber-bg: rgba(148,87,10,0.10);       /* was rgba(166,94,7,0.10) (:144) */
--opd-registered: #475569;              /* new override */
--opd-vitals: #92400E;                  /* new override */
--opd-billing: #6D28D9;                 /* new override */
--opd-cancel: #B91C1C;                  /* new override */
--opd-waiting: #1D4ED8;                 /* was #2563EB (:159) */
--opd-consult: #065F46;                 /* was #059669 (:160) */
--action-grad: linear-gradient(135deg, #047857 0%, #0E6FA8 55%, #1668D6 100%);
--accent-fill: #1668D6;
--red-fill: #C62F3B;
--green-fill: #047857;
```

### 3.3 `admin/platform.html` — `:root` (dark-only)
`--text3: #7E97A3` (was `#6E8794`, `:32`); add `--action-grad`, `--accent-fill`, `--red-fill` with the dark values above.

## 4. Exact rule changes (each a one-property swap)
| File:line | Selector / element | Change |
|:-|:-|:-|
| index `:645` | `.btn-accent` | `background: var(--action-grad)` |
| index `:1270` | `.upgrade-cta` | `background: var(--action-grad)` |
| index `:1581` | `.dx-scan-go` | `background: var(--action-grad); color: #fff` (dark text on the light-theme gradient end is 3.60 today) |
| index `:1924` | `.hc-day.on` | `background: var(--action-grad)` |
| index `:2741` | `.ipd-av` (avatar initials) | `background: var(--action-grad)` |
| index `:601` | `.day-picker label:has(input:checked)` | `background` + `border-color: var(--accent-fill)` |
| index `:759` | `.nav-group.has-active:not(.open) .nav-group-count` | `background: var(--accent-fill)` |
| index `:1712` | `.svc-chip[aria-checked="true"]` | `background: var(--accent-fill)` (drops the gradient to `--accent2`, 2.17 at its end) |
| index `:2453` | `.ketu-send` | `background: var(--accent-fill)` |
| index `:2798` | `.ipd-tabs button[aria-selected="true"]` | `background: var(--accent-fill)` |
| index `:24339`, `:29790` | inline `var(--accent…)` + white text | `var(--accent-fill)` |
| index `:1541`, `:1617`, `:1826`, `:3420`, `:3489` | `.ord-stat`, `.lis-flag.crit`, `.ot-prio`, `.dxc-chip.stat`, `.dxb-flag.critical_*` | `background` (and `border-color`) `var(--red-fill)` |
| index `:1831` | `.ot-step[data-done="true"]` | `background: var(--green-fill)` |
| index `:1722-1723`, `:1744` | triage `[data-t]` + `.emg-tag` | `[data-t="red"] { --c-fill: var(--red-fill) }`, `[data-t="green"] { --c-fill: var(--green-fill) }`; `.emg-tag { background: var(--c-fill, var(--c)) }` — `--c` itself (borders, dots) unchanged |
| index `:1745`, `:1827` | `.emg-tag[data-t="yellow"]`, `.ot-prio[data-p="urgent"]` | add `:root[data-theme="light"] … { color: #fff }` (A11Y-6); dark keeps `#1f1500` (8.83) |
| index `:3910`, `:3924` | inline notification badges `#ef4444` | `var(--red-fill)` |
| index `:4473` | `#diagStatusTitle` inline `color:#fff` | `var(--text-strong)` (A11Y-4) |
| platform `:776` | `.btn-primary` | `background: var(--action-grad)` |
| platform `:605` | `.action-btn:hover` | `background: var(--accent-fill)` |
| platform `:1103` | owner bell badge `#ef4444` | `var(--red-fill)` |
| platform `:2542`, `:5274` | inline `var(--accent)` + white text | `var(--accent-fill)` |
Left as they are (measured, pass): `.action-btn-danger:hover` (4.82), `#b45309` badge (5.02), `#2563eb` button (5.17),
amber fills with dark text in dark theme (8.83), dark-surface headings in the owner console (white on navy).
`admin/queue-display.html` `.room-badge` (white on `#0284c7`, ≈ 4.1:1) is large bold TV text (≥ 3:1 required) — verify its
rendered size on the TV during A11Y-08; change to `#0369A1` only if it renders below 18.66 px bold.

## 5. What does not change
Layouts, spacing, fonts, radii, shadows, JavaScript, any HTML structure, server-generated PDFs and WhatsApp messages.
`--accent`, `--red`, `--green` as **text, border and dot** colours (they already pass as text). The logo and its gradient.
Charts pick the new `--text3` for axis labels automatically the next time they render.

## 6. Regression guards (new tests, run in the normal suite)
| Test | Asserts |
|:-|:-|
| `tests/test_ui_contrast.py` | parses both `:root` blocks of `admin/index.html` and the console's `:root`; composites translucent surfaces over `--bg`; asserts `--text`, `--text2`, `--text3` ≥ 4.5 on `--bg`, `--surface`, `--surface2`, `--surface3`; each status colour ≥ 4.5 on its own `-bg` tint; white ≥ 4.5 on `--accent-fill`, `--red-fill`, `--green-fill` and on every stop of `--action-grad`; every OPD stage token ≥ 4.5 on its 18 % tint, in both themes |
| `tests/test_ui_no_white_on_bright_fill.py` | scans CSS rules and inline styles in the three HTML files: a rule that sets white text and a background of `var(--accent)`, `var(--accent2)`, `var(--red)`, `var(--green)`, `var(--brand-grad)`, `#ef4444` or `#10B981` fails (allow-list for decorative rules without text) — stops the problem coming back |
| `tests/test_ui_light_theme_no_hardcoded_white_on_surface.py` | no inline `color:#fff` on an element whose own style sets a `var(--surface*)` background (catches A11Y-4-type bugs) |
These parse files only — hermetic, no browser.

## 7. Visual review (before merge)
Screenshots before/after at 1440 and 390 px, dark and light: login · dashboard · appointments · OPD queue, desk,
workspace, billing (pills, shift bar) · IPD chart (avatar, tabs) · DX worklist (scan button, flags) · LIS result entry
(critical flags) · Emergency board (triage tags) · OT board (priority, steps) · Pharmacy POS and setup · settings
(day picker) · home-collection calendar · Kriya Ketu panel · Insights charts · owner console dashboard, clinic modal,
broadcast. Expected visible difference: primary buttons and selected states slightly deeper; labels and table headers
slightly clearer; light-theme amber slightly darker; OPD pills readable in light theme; the Report Connector title
visible in light theme. Nothing moves.

## 8. Rollout
One commit `fix(ui): WCAG AA contrast — fill tokens, text3, amber, OPD stages (A11Y-1…6)`; no migration, no flag (a
token change cannot be half-on); deploy with the normal process; rollback = revert the commit. Release note for clinics:
"Buttons and labels are easier to read, especially in light mode and on bright screens."

## 9. Tasks
| ID | Task | Depends | Acceptance | State |
|:-|:-|:-|:-|:-|
| A11Y-00 | Re-locate every §3/§4 line by selector on current `main` | — | table updated if lines moved | VERIFIED |
| A11Y-01 | Contrast tests first (§6) — they fail on today's tokens | A11Y-00 | red for exactly the §2 findings | VERIFIED |
| A11Y-02 | Token changes §3.1–§3.2 | A11Y-01 | `test_ui_contrast.py` green | VERIFIED |
| A11Y-03 | Rule swaps in `admin/index.html` (§4) incl. A11Y-4 and A11Y-6 | A11Y-02 | `test_ui_no_white_on_bright_fill.py` + light-theme test green | VERIFIED |
| A11Y-04 | Owner console tokens and rules (§3.3, §4) | A11Y-02 | same tests green for `platform.html` | VERIFIED |
| A11Y-05 | Full existing suite unedited | A11Y-03, A11Y-04 | 0 failed | VERIFIED |
| A11Y-06 | Visual review §7 (screenshots in the session log) | A11Y-05 | owner sign-off | IN_PROGRESS — component before/after comparison (all four theme cells) checked; per-page review in the logged-in panel by the owner |
| A11Y-07 | Update `KRIYA_AI_DESIGN_SYSTEM.md` colour tables and NEXT-LANES-UI-SPEC §6.2 to the new values | A11Y-06 | docs match code | VERIFIED (design doc colour tables) |
| A11Y-08 | Queue display TV check (§4 note) | A11Y-06 | measured on the TV | NOT_STARTED |
Independent of every other lane; best done **before** the new lanes build screens, so they inherit the fixed tokens.

## 10. Implementation notes (2026-11-24)
- Also fixed (found by the new guard test, same kind): emergency FAB `linear-gradient(#ef4444,#f97316)` → `var(--red-fill)` → `#B45309`; triage segment buttons and the black triage tag (`#8b95a5` → fill `#4B5563`) via `--c-fill`; owner-console `.btn-primary` base gradient and an overridden logo gradient.
- Gates: `tests/test_ui_contrast.py`, `tests/test_ui_no_white_on_bright_fill.py` (the planned third test was folded into the second as `test_clinic_panel_inline_white_text_has_its_own_background`). 47 passed, 1 skipped; full suite 10,276 passed, 6 skipped, 0 failed.
