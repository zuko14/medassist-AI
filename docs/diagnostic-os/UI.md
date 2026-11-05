# Kriya Diagnostic OS — Screens (admin panel, partner portal, owner console)

> Part of the Diagnostic OS set: start at [status.md](status.md). Frontend rule (AGENTS.md §1.7): the live panel is the
> inlined `admin/index.html`; `admin/admin.js` is dead — never edit it. Follow the existing patterns: `go(page)`,
> `api*()` helpers, design tokens from Session 29, `data-ipd`-style gating.

## 1. Conventions
- Every DX element carries `data-dx="page|nav|perm"`; `applyDxGates()` (called after `/admin/me`) hides DX navigation
  unless `me.dx_access` is truthy and the user holds the page's permission. Gating is cosmetic: the server decides.
- All DX code lives between `<!-- DX:<lane>:START -->` and `<!-- DX:<lane>:END -->` (HTML) and
  `/* DX:<lane>:START */ … /* DX:<lane>:END */` (JS) markers so parallel lanes merge without overlapping hunks
  (AGENT-PROMPT.md §4). Lanes: `foundation`, `catalogue`, `desk`, `samples`, `logistics`, `bench`, `reports`, `b2b`, `mis`.
- Never show a state before the server confirms it; buttons disable while a request is in flight; every write sends
  an `Idempotency-Key` generated per click.
- 390 px: no horizontal page scroll; tables collapse to cards (container queries as in the G1 worklist).
- Barcode scanning: keyboard-wedge scanners type into a focused input ending with Enter (primary); camera scan with
  the `BarcodeDetector` API where available (phlebotomist, phones); manual entry always possible.
- Sidebar group "Diagnostics" (Session 52 sidebar groups) holds the DX pages.

## 2. Pages

| Page id | Name | Users | Key elements |
|:-|:-|:-|:-|
| `dxsetup` | DX Setup | DX_ADMIN | checklist with deep links; sites (kind, code, legal/NABL fields); departments; containers with colour swatches & stability; signatories (doctor, designation, signature upload); rejection reasons; settings form (approval levels, payment rules, release policy, TAT clock, prefixes, share-link TTL) with CAS; delivery owner per branch (connector / shadow / dx); Go live |
| `dxcatalog` | Test Master | DX_CATALOGUE, approver | list with filters (kind, department, approved, active); test editor (code, kind, department, container, method, TAT, print title, interpretation, notes, gender restriction); components editor (panel/package); parameter grid with bands per age/sex, reference text, formula field with live check, decimals/rounding, machine code, delta; approval panel showing a digest of all bands → **Approve**; seed library preview/apply; CSV import with dry-run report |
| `dxdesk` | Registration | DX_REGISTER, DX_BILLING | left: patient search/register, referrer type-ahead (+ add), provider/B2B account, external ref, priority, fasting & last meal, notes, delivery consent; centre: test cart (code/name search, package expansion preview, container dots, fasting/prep warnings, price source per line, discount within limit); right: totals, bill status, receipt (cash/UPI/card), payment link, **Save & Bill**, **Print labels**, **Print bill**; tab "Today's bookings" (WhatsApp/voice/home) with **Arrived → Register** |
| `dxcollect` | Sample Collection | DX_COLLECT | queue by site (STAT first); visit card with tubes (colour, tests, barcode); scan to bind/confirm; **Collected**; reject with reason; reprint label |
| `dxlogistics` | Logistics | DX_LOGISTICS, DX_RECEIVE | Outgoing: new batch (destination), scan-in list, stability timers (green/amber/red), dispatch form; Incoming: batches in transit, receive by scanning, temperature, discrepancy summary; Track sample (barcode → timeline) |
| `dxbench` | Lab Worklist | LAB_ENTRY, DX_VERIFY, LAB_VALIDATE | stage tabs **Receipt · Entry · Verification · Approval · Release · Done · All** (MocDoc intent, our names) with counts; filters site, department, priority, date; row = visit + tests with tube colour, sample ID, TAT bar (elapsed / target, amber at 75 %, red at 100 %), critical badge; open worksheet |
| `dxworksheet` | Worksheet (drawer) | LAB_ENTRY / DX_VERIFY | parameters in print order: value input (numeric / options), unit, applicable band, reference text, flag preview **from the server** after save, previous value + delta badge, calculated values read-only, instrument value with source tag, remarks, attachments upload; Save; Verify / Return (verifier); version history and amendment reason |
| `dxapprove` | Approval | LAB_VALIDATE | queue (critical first), single review with all tests of the visit side by side; **Approve & sign**; "Normal results" tab with multi-select batch sign (≤ 50) and per-item outcome; critical-call drawer |
| `dxreports` | Reports & Dispatch | DX_DISPATCH | tabs To release · Held (dues) · Sent · Failed · Printed · Shadow; release now (partial/final), preview PDF, print (logs), resend, share link, delivery receipts per target |
| `dxtemplates` | Report Templates | DX_ADMIN | list by owner (clinic / branch / account); editor (header image or composed, footer, fields, body toggles, signatures, QR, disclaimer); clone-from-PDF crop tool; live preview; publish / retire; version history |
| `dxb2b` | B2B Accounts | DX_B2B_MANAGE | accounts (identity, delivery, white label, billing mode, limit, days, hold state); rate cards with CSV import; ledger; receivables ageing; statements (generate, PDF, send, void); payments; partner logins; barcode stock |
| `dxreferrers` | Referrers | DX_ADMIN / DX_REGISTER | list, merge duplicates (re-points future visits only), report-copy preference; referral volume (no payouts, ADR-DX-11) |
| `dxdash` | DX Dashboard | DX_READ / DX_REPORTS_MIS | today by stage per site, TAT breaches, critical open, held dues, rejections by reason, revenue (billing users), CSV exports |
| `dxinstruments` | Instruments & QC | DX_ADMIN / DX_QC | bridge agents (key once), instruments, unmatched results, QC materials, Levey-Jennings chart with Westgard flags |

## 3. Partner portal (B2B_PARTNER, confined)
Served by the same `admin/index.html` with a confined navigation (like the phlebotomist portal): **New sample**
(patient, external ref, tests from the account rate card, labels), **My patients** (visits with stage chips, report
PDF when released and the account's delivery rule allows), **Statements** (download, dues). No other page renders;
the server refuses every other route for this role.

## 4. Phlebotomist portal additions (existing `/admin/home-collection/my-visits`)
On each visit card: **Samples** panel listing tubes with colour and tests; scan or type barcodes; **Collected**
calls the new samples route; offline is not supported (shows a clear "no connection — not saved" state).

## 5. Owner console (`admin/platform.html`)
Diagnostic OS card in the clinic modal beside OPD/IPD: toggle `toggleDx(cb)`, eligibility message, deactivation
preview dialog (visits in process, samples in transit, unreleased results, B2B balance) with confirm → wind-down;
directory pill "DX".

## 6. Accessibility and print
- Contrast per the design tokens; flags never colour-only (bold/asterisk + text).
- Label and bill printing use dedicated print CSS / PDFs sized to the medium (labels 50×25 mm, bill A5 or 80 mm).
- Keyboard flow on the desk: search → Enter → test code → Enter adds → Ctrl+S save & bill → Ctrl+P labels.
