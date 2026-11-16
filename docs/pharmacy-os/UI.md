# Kriya Pharmacy OS — UI Specification (`admin/index.html`)

> Part of the Pharmacy OS set: start at [status.md](status.md). Existing UI facts: [AUDIT.md](AUDIT.md) §6
> (A39–A43). Routes: [API.md](API.md).

## 0. Rules
- All code inline in `admin/index.html` inside `<!-- RX:<lane>:START — … -->` / `<!-- RX:<lane>:END -->` (HTML) and
  `/* RX:<lane>:START */ … /* RX:<lane>:END */` (JS/CSS). Lanes: `foundation`, `catalogue`, `stock`, `purchase`,
  `pos`, `ipd`, `transfers`, `audit`, `registers`, `refills`, `dash`. `admin/admin.js` is never touched.
- Gating: every RX element carries `data-rx="nav PERM1 PERM2"` (shown if user holds any) and `style="display:none"`;
  `applyRxGates()` (copy of `applyDxGates`) runs after `/admin/me`. With `rx_access` null nothing is shown and no RX
  loader is reachable (`go()` refuses an RX page whose nav link is hidden).
- Theme: only existing tokens (`--accent`, `--surface*`, `--text*`, `--green/-bg`, `--amber/-bg`, `--red/-bg`,
  `--border`), dark and light. No new fonts, no external scripts.
- Money shows `₹1,234.50`; quantities show `2 strips + 4 tabs` when `pack_size > 1` (`fmtPackLoose`).
- Every action button is disabled during its request and re-enabled on response (no double submit); every list has
  loading, empty and error states.

## 1. Navigation
New group after Diagnostics (admin/index.html ≈ 3858): `<div class="nav-group" data-nav-group="rx">` titled
**Pharmacy** (pill-bottle icon, same SVG stroke style). Links (page id → loader):

| Page id | Label | Gate | Loader |
|:-|:-|:-|:-|
| `rxpos` | Pharmacy POS | RX_DISPENSE | `rxpos.load()` |
| `rxindents` | Ward Indents | RX_DISPENSE IPD_NURSING | `rxind.load()` |
| `rxrefills` | Refill Requests | RX_DISPENSE | `rxref.load()` |
| `rxstock` | Stock & Expiry | RX_READ | `rxstock.load()` |
| `rxpurchase` | Purchase & GRN | RX_PURCHASE | `rxpur.load()` |
| `rxtransfers` | Transfers | RX_STOCK IPD_NURSING | `rxtr.load()` |
| `rxaudit` | Stock Audit | RX_STOCK | `rxaud.load()` |
| `rxitems` | Drug Master | RX_ADMIN RX_READ | `rxcat.load()` |
| `rxregisters` | Drug Registers | RX_REGISTER_EXPORT | `rxreg.load()` |
| `rxdash` | Pharmacy Dashboard | RX_READ | `rxdash.load()` |
| `rxsetup` | Pharmacy Setup | RX_ADMIN | `rxos.loadSetup()` |

Loaders appended to the `loaders` map (admin/index.html:17909) inside `/* RX:<lane>:START */` comments, exactly as DX.
The Ketu `PAGES` table gains the same ids (RT-14).

## 2. Fast-POS (`rxpos`) — the 3-second counter
### 2.1 Layout at 1440 px
```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Store: OPD Counter ▾   Shift: open (Ravi) · ₹18,420   [F2 Search] [F9 Recent bills]   ⌁ online │
├──────────────────────────────────────────────────────────┬───────────────────────────────────┤
│ Rx: Dr. S. Rao · signed 10:42 · MRN 004211 · K. Lakshmi 54F│  TOTAL                ₹ 612.00    │
│ ┌──┬─────────────────────────┬──────────────┬─────┬──────┐│  Taxable  ₹ 582.86               │
│ │# │ Item (Rx text)          │ Batch / Exp  │ Qty │ ₹    ││  CGST 2.5% ₹ 14.57  SGST ₹ 14.57 │
│ │1 │ Glycomet 500 SR         │ GL2231 ● 09/27│ 60 │ 96.00││  Discount (F4)     ₹ 0.00        │
│ │  │ ↳ "Metformin 500 BD x30"│              │     │      ││  Round off        −₹ 0.00        │
│ │2 │ Augmentin 625 Duo   ⚠H  │ AU774 ● 03/27│ 10 │ 228.0││  ───────────────────────          │
│ │3 │ ⚠ Map: "Pan D"  ▸ 3 matches            │     │      ││  Cash F6 | UPI F7 | Card F8       │
│ └──┴─────────────────────────┴──────────────┴─────┴──────┘│  [ Enter  Complete & Print ]      │
│ Pending: line 3 needs confirmation                         │  ☐ WhatsApp bill                  │
└──────────────────────────────────────────────────────────┴───────────────────────────────────┘
```
- Expiry dot: `--green` (> near_expiry_days), `--amber` (near / short shelf), `--red` (expired — never selectable,
  shown only in the batch drawer for information).
- Schedule chips: `H`, `H1` (amber), `X`/`N` (red) with tooltip of what is required.

### 2.2 Keyboard map (all keys work without the mouse; shown in a `?` overlay)
| Key | Action |
|:-|:-|
| F2 | focus universal search (token, MRN, phone, bill no., item name, barcode) |
| ↑ ↓ / Enter | move through results / pick |
| Ctrl+B | open batch drawer for the focused line |
| + / − | quantity ±1 unit; Shift + / − ±1 pack |
| Del | remove focused line |
| F4 | discount (amount or %) |
| F6 / F7 / F8 | payment mode cash / UPI / card (amount defaults to the remaining balance) |
| Enter (on totals) | Complete & print |
| F3 | park current bill / show parked bills (ADVANCED-FEATURES A2) |
| F9 | recent bills (reprint / return) |
| F11 | repeat a previous bill of this patient (A5) |
| Ctrl+I | Rx Inbox panel (A1) |
| Ctrl+O | request manager override (A9) |
| F10 | return mode on a scanned bill |
| Esc | close drawer / cancel prompt (never discards the draft) |
- Barcode scanners (keyboard wedge, suffix Enter) are detected by inter-key timing < 30 ms; a scan never lands in a
  quantity field.
- Timing target: 3-line Rx from F2 to printed receipt ≤ 3 s for a trained user (UI script `tests/ui/rx_pos_timing.md`).

### 2.3 Batch selection drawer (Ctrl+B or click the batch chip)
Right-side drawer, FEFO list: batch, expiry (with "in 41 days"), MRP/pack, on hand (packs+loose), rack. Rank 1 is
pre-selected with a "FEFO" tag. Picking another batch opens a one-line reason prompt (required when
`fefo_override_requires_reason`). Blocked/expired rows are greyed with the reason. Short-shelf warning appears when
`expiry < today + max(min_shelf_days_at_sale, course_days)`.

### 2.4 Mapping and substitution
Amber line "Map: <Rx text>" opens a picker with ≤5 deterministic candidates (alias ✓, barcode, trigram score). Picking
saves the alias. Out-of-stock lines show "Same composition in stock:" list (same generic only) with reason picker
(`Out of stock`, `Patient request (cost)`, `Formulary`), or "Mark pending". If the Rx text contains a no-substitution
instruction, substitution controls are hidden with the note "Prescriber: no substitution".

### 2.5 Receipt
- **80 mm thermal** (default): clinic name, address, GSTIN (if any), drug licence no.; `TAX INVOICE` or `BILL OF
  SUPPLY`; bill no. `PHS-…`, date/time IST; patient name, MRN, prescriber + reg. no.; lines (item, batch, exp, qty, MRP,
  amount); GST slab table (taxable, CGST, SGST); total in words; substitution notes; pending balance; "Goods once
  sold … returns within 7 days with bill" (configurable); bill QR (bill id, for returns).
- **A5**: same content, two columns, for clinics with laser printers.
- Print via a same-origin print view + `window.print()`; `@page { size: 80mm auto; margin: 0 }` / `@page { size: A5 }`.

## 3. Ward Indents (`rxindents`)
- **Nurse view** (IPD_NURSING): admission picker (ward-scoped) → table of active medication orders (drug, dose, route,
  frequency, PRN) with checkbox, units (default 24 h need), ward stock badge; priority select; **Raise indent**. List
  of my ward's indents with status and **Receive** button on delivered ones.
- **Pharmacy view** (RX_DISPENSE): queue grouped STAT (red, top, audible chime once), URGENT, ROUTINE; each card: ward,
  bed, patient, age of indent, lines count. Opening an indent shows lines with mapping + FEFO batch like POS;
  **Dispense** (Enter) prints the issue slip.
- MAR integration (existing IPD chart page): the "Record administration" form adds a "Batch (optional)" select fed by
  `/admin/rx/ipd/admissions/{id}/dispense-lines?order_id` — rendered only when `rx_access`.

## 4. Purchase & GRN inward ledger (`rxpurchase`)
Tabs: **Reorder**, **Purchase Orders**, **GRN**, **Supplier Returns**, **Suppliers**.
GRN entry grid (spreadsheet-like, Tab moves right, Enter adds a row):
`Item | Batch | Exp (MM/YY) | Packs | Free | Rate | Sch% | CD% | GST% | MRP | Taxable | Tax | Landed/unit | ⚠`.
Footer: computed total vs supplier total with green ✓ / red diff; **Post GRN** disabled until equal within ₹0.99.
Posted GRNs list = the inward ledger (date, GRN no., supplier, invoice no., taxable, tax, total, items) with CSV export.

## 5. Stock & Expiry (`rxstock`)
Search + filters (store, schedule, below reorder, near expiry); table item → expandable batches; ledger drawer per
batch with running balance; **Block batch** (RX_ADMIN); **Write-off** (RX_STOCK). Expiry tab: buckets Expired / ≤30 /
≤60 / ≤90 days with value and **Create supplier return**.

## 6. Stock Audit (`rxaudit`)
Desktop: audit list → counting grid (system qty hidden by default for blind count, toggle for managers) → review
(variance by value, reason select) → post (second approver modal re-authenticates with password if over threshold).
Mobile 390 px: single-column count screen: scan → big item card → numeric keypad → next.

## 7. Drug Master (`rxitems`)
List with search; editor form sections: Identity (code, brand, generic picker with "new generic" inline, manufacturer),
Packaging (base unit, pack size, pack label, loose sale), Regulatory (schedule, HSN, GST rate, cold chain), Stock
policy (reorder level/qty, rack), Refill (chronic). Locked fields show a lock icon with the reason. Barcodes and
aliases sub-tables. Import wizard: upload → dry-run table of errors → apply.

## 8. Drug Registers (`rxregisters`)
Register tabs H / H1 / X / Narcotic; date range, store; paged table matching the PDF columns; chain status badge
(✓ verified at hh:mm / ✗ break at seq n); **Export PDF**, **Export CSV**.

## 9. Dashboard (`rxdash`)
Tiles: today's sales (₹, bills), returns, pending indents (STAT count red), refill requests, low-stock items,
near-expiry value (90 d), expired on hand (red if > 0), register chain status. Margin tile only for RX_ADMIN.
Charts follow the existing dashboard component (no new chart library).

## 10. Responsive behaviour
| Width | Behaviour |
|:-|:-|
| 1440 px | layouts above; POS two-pane (lines 65% / totals 35%) |
| 1024 px | POS totals pane collapses under lines; drawer overlays |
| 390 px | POS: single column — search, line cards (item, batch chip, qty stepper), sticky bottom bar with total + Complete. Keyboard shortcuts hidden; scanner via phone camera is **not** in v1 (manual search). Indents, transfers receive, stock lookup and audit count are fully usable; GRN entry and registers show "Use a desktop for this screen" with read-only lists |
Verification: live UI check at 1440 and 390 px recorded in status §9 for every page (P1–P9 DoD).

## 11. Accessibility
All interactive elements are `button`/`input` with labels; drawer traps focus and returns it; colour is never the only
signal (expiry dot + text "Exp 09/27 · 41 d"); `aria-live="polite"` on totals and error toasts; contrast ≥ 4.5:1 in
both themes using existing tokens.
