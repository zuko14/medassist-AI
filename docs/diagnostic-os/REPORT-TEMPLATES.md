# Kriya Diagnostic OS — Test Master, Formulas, Report Rendering, Templates, Labels

> Part of the Diagnostic OS set: start at [status.md](status.md). Schema in [DATA-MODEL.md](DATA-MODEL.md) §3, §7, §8.
> All of this is **zero-LLM** (ADR-DX-10). Every rule here gets a pure-function unit test with the vectors given.

## 1. Test master: how "n number of tests" are modelled

| Concept | Row | Example |
|:-|:-|:-|
| Orderable single test | `lab_tests.kind='test'` + its `lab_test_parameters` | `HBA1C` → parameters `HBA1C` (%), `MBG` (mg/dL, calculated) |
| Panel / profile | `lab_tests.kind='panel'` + `dx_test_components` → tests | `LIPID` → TC, TG, HDL, LDL, VLDL, NONHDL, TC_HDL, LDL_HDL (as one test with many parameters) or `RFT` → UREA, CREAT, UA, CA, PHOS, ELEC (as several tests, screenshot style) |
| Health package | `lab_tests.kind='package'` + components (tests and/or panels) | `AARUSH_EXEC` → CBC, LIPID, HBA1C, LFT, RFT, TSH, URINE_RE |
| Parameter band | one `lab_test_parameters` row per (name, age band, sex) | Hb: male adult 13.0–17.0; female adult 12.0–15.0; child bands |

Rules (enforced by `dx_guard_components` and the service):
- A *test* owns parameters; a *panel* or *package* owns none. Depth ≤ 2 (package → panel → test); no cycles.
- One work unit (`opd_orders` row) per **test**, whatever was sold. A package's price is on its
  `dx_accession_items` row; member tests carry `price_source='package_member'` information only through the order's
  `accession_item_id`.
- `container_id` and `department_id` are required on a *test* before the lab director can approve it.
- `dx_approved_at` is required before a test can be ordered in DX (WhatsApp booking is unaffected — it uses its own
  existing rules). Editing a band, unit, formula or reference text of an approved test clears approval in the same
  transaction (trigger) and the test cannot be ordered until re-approved; already-signed reports are unaffected
  because they carry snapshots.
- Branch-specific tests (`lab_tests.branch_id`, mig 076) remain supported; DX price resolution is: account rate card
  item → branch test row price → clinic test row price.

### 1.1 Worked parameter definitions (from the Taiyo reference report)

| Test | Code | Unit | Method | Decimals / rounding | Normal band (flagging) | `reference_text` (printed) | Formula |
|:-|:-|:-|:-|:-|:-|:-|:-|
| HbA1c | `HBA1C` | % | HPLC | 1 / half_up | 4.0–6.0 (adult, all) | `Non-diabetic: 4.0 - 6.0 %`<br>`Pre-diabetic: 6.0 - 6.5 %`<br>`Diabetic: > 6.5 %` | — |
| Mean Blood Glucose | `MBG` | mg/dL | Calculated | 2 / half_up | none (not flagged) | — | `28.7 * HBA1C - 46.7` |
| Total Cholesterol | `TC` | mg/dL | CHO-POD | 0 | `normal_high = 200`, `high_exclusive = true` (200 flags high) | `Desirable level < 200`<br>`Borderline High 200-239`<br>`High >= 240` | — |
| Triglycerides | `TG` | mg/dL | GPO-POD | 0 | `normal_high = 150`, `high_exclusive = true` (162 → high, printed bold as in the sample) | `Normal < 150` … `Very High > 500` | — |
| HDL | `HDL` | mg/dL | Enzymatic immunoinhibition | 0 | 40–60 | `40 - 60` | — |
| VLDL | `VLDL` | mg/dL | Calculated | 0 / half_up | 6–38 | `6 - 38` | `TG / 5` |
| LDL | `LDL` | mg/dL | Calculated | 0 / **truncate** | `normal_high = 100` | `Optimal < 100` … `Very High >= 190` | `TC - HDL - TG / 5` |
| Non-HDL | `NONHDL` | mg/dL | Calculated | 0 | `normal_high = 130` | `< 130` | `TC - HDL` |
| CHOL/HDL | `TC_HDL` | Ratio | Calculated | 2 | 0–5.0 | `0 - 5.0` | `TC / HDL` |
| LDL/HDL | `LDL_HDL` | Ratio | Calculated | 2 | 0–3.5 | `0 - 3.5` | `LDL / HDL` |

**Vectors (must pass exactly):** HBA1C 7.5 → MBG **168.55**; TC 134, TG 162, HDL 40 → VLDL **32**, LDL **61**
(61.6 truncated), NONHDL **94**, TC_HDL **3.35**, LDL_HDL **1.54** (computed from unrounded LDL 61.6; with the
rounded value it would be 1.53 — formulas always consume **unrounded** upstream values; only display is rounded).

## 2. Ranges and flags (extends `app/services/lis.py`, still pure)

- Band selection (existing `select_range`): parameter name group → band covering `age_days` and sex; none → value
  stored with flag `not_evaluated` (never assumed normal).
- Numeric flag order (existing `flag_value`): critical_low / critical_high first, then low / high, else normal.
  **Boundary rule (as built in 118, unchanged by default):** `value < normal_low` → low; `value > normal_high` →
  high; equal is normal. DX-M2 adds `low_exclusive` / `high_exclusive` (default `false`): when true, a value *equal*
  to the bound is flagged (for "desirable < 200" ranges). `lis.flag_value` reads the two keys with `False` defaults,
  so every existing parameter flags exactly as before (regression vector set from `tests/test_lis_api_e2e.py`). The
  printed text is `reference_text`, which never drives flags.
- Options/text parameters: existing `normal_text_value` comparison (`Negative`, `Absent`, `Nil`).
- Calculated parameters are flagged like any numeric value from their own bands (or not flagged when no band).
- **Delta check** (new, `dx_results.delta_check`): previous validated value of the same parameter `code` for the same
  person within `delta_window_days`; `|new − old| / old × 100 > delta_check_pct` → `delta_failed=true`; never blocks
  entry; blocks **batch** approval and auto-verification only.
- **Auto-verification eligibility** (opt-in per test): all values `normal`, none `not_evaluated`, no delta failure,
  no critical, no free-text remark, not an amendment.

## 3. Formula engine (`app/services/dx_formula.py`)

Grammar (parsed with `ast.parse(expr, mode='eval')`, whitelist only):
- literals: integers/decimals; names: parameter `code`s of the **same test** plus context names `AGE_YEARS`,
  `AGE_DAYS`, `IS_FEMALE`, `IS_MALE` (0/1);
- operators: `+ - * /`, unary `-`, `**` (exponent ≤ 4 in absolute value unless inside `pow`), comparisons
  `< <= > >= == !=` only inside `iif`;
- functions: `min, max, abs, sqrt, ln, log10, pow(x, y), round(x, n), iif(cond, a, b)`, and named clinical formulas
  `egfr_ckd_epi_2021(creatinine_mg_dl)` (uses `AGE_YEARS`, `IS_FEMALE`: 142 × min(Scr/κ,1)^α × max(Scr/κ,1)^−1.200 ×
  0.9938^Age × 1.012[female]; κ 0.7/0.9, α −0.241/−0.302);
- anything else (attributes, subscripts, calls to other names, lambdas, strings) → `dx_formula_invalid` at **save
  time** of the parameter definition, with the offending token.

Evaluation: `Decimal` for `+ - * /` (context precision 28), `float` for `sqrt/ln/log10/pow` converted back to
`Decimal` (12 significant digits); division by zero, missing input or domain error → parameter not computed,
worksheet warning, flag `not_evaluated`; dependency order by topological sort over codes; cycles refused at save.
Rounding at display/storage to `decimals` with `rounding` mode; `numeric_value` stored rounded to 3 dp (NUMERIC(10,3),
118) and the unrounded value used for downstream formulas within the same save.

Additional vectors: indirect bilirubin `TBIL - DBIL` (1.2, 0.3 → 0.9); A/G ratio `ALB / (TP - ALB)` (4.2, 7.0 →
1.50); eGFR (creatinine 1.0, 50 y, male → 90 ± 1 per the published equation; female 0.8, 50 y → 89 ± 1 —
compute and pin the exact values in the test from the published formula at implementation time).

## 4. Report templates

### 4.1 Layout document (`dx_report_templates.layout`, schema v1, pydantic-validated)
```json
{
  "schema": 1,
  "page": {"size": "A4", "margin_mm": {"top": 10, "bottom": 12, "left": 12, "right": 12}},
  "header": {"mode": "image", "image_asset": "dx-assets/<clinic>/templates/<id>/header.png", "height_mm": 34,
             "composed": {"logo_asset": null, "title": "DIAGNOSTIC REPORT", "lines": ["Address…", "Phone…"],
                          "accent_hex": "#F28C28"}},
  "footer": {"lines": ["24x7 Home collection: 9xxxx xxxxx"], "height_mm": 16, "page_numbers": true},
  "patient_block": {"fields": ["name", "age_sex", "referrer", "visit_no", "mrn", "bill_no", "external_ref",
                               "provider", "collected", "received", "reported", "sample_ids"],
                    "show_barcode": true},
  "body": {"group_by_department": true, "show_department_heading": true, "show_package_heading": true,
           "show_method": true, "show_previous_value": true, "flag_style": "bold", "font_pt": 9,
           "end_marker": "**END OF REPORT**"},
  "signatures": {"placement": "per_department", "show_image": true, "show_registration_no": true},
  "verification": {"qr": true, "position": "footer_right"},
  "watermark": null,
  "disclaimer": "Results relate only to the sample tested. Correlate clinically."
}
```
Validation: unknown keys refused; assets must live under the clinic's own storage prefix, PNG/JPEG ≤ 1 MB,
≤ 3000 px wide; colours `#RRGGBB`; text fields ≤ 300 chars; no HTML anywhere. `tested_at` is not a key: the renderer
**always** prints the "Tested at" line for white-label releases and "Performed at" for outsourced tests (ADR-DX-08).

### 4.2 Resolution cascade (at release time) and snapshot
`visit.template_override_id` → account template (`dx_b2b_accounts.white_label` and `template_id`) → template whose
owner is the registration branch → clinic default → built-in default. The resolved layout, the letterhead text of
the processing site (`dx_sites` legal fields), signatory snapshots and asset **bytes hashes** are copied into the
release. Published templates are immutable; assets are content-addressed (`…/<sha256>.png`) so an old release keeps
rendering identically even after the template changes.

### 4.3 How templates are created with little effort ("template generation")
1. **Starter templates**: three built-ins (Classic band, Minimal, Hospital white-label) parameterised by logo,
   accent colour, address lines — a template in under a minute.
2. **Clone from my current report**: upload one PDF page of the existing (e.g. MocDoc-printed) report; the server
   rasterises page 1 with `pdf2image`/poppler (already in the Docker image), the user crops the header and footer
   bands in the browser, they become `header.image_asset` / footer image. Deterministic, no AI.
3. **Optional AI layout suggestion** (off by default, ADR-DX-10): from the uploaded image, propose `composed` fields
   (colours, title text). Output is a draft layout a human edits and publishes.
4. **Live preview**: every edit re-renders a fixture visit (the Taiyo HbA1c + lipid example as seed data) to PDF.

## 5. Visit report rendering (`app/services/dx_pdf.py`, pure)

Input: release snapshot + signed results (values, units, flags, reference display, method, interpretation, notes,
attachments, previous values) + patient/visit snapshot. Output: bytes, page count, SHA-256.

Layout rules:
1. Header image/composed block on **every** page; footer with `Page n of N` (two-pass render), QR (verify code URL)
   and the mandatory "Tested at" line when applicable.
2. Patient block (two columns) per `patient_block.fields`; timestamps in IST `Sep 01, 2026, 07:02 a.m.` style; sample
   IDs listed with a Code128 of the first (or of the visit number when several).
3. Body: department heading (`report_heading`) → test print title → optional package heading → 4-column table
   (Test description · Value · Unit · Reference range) → method line in 7 pt italic under each parameter →
   `reference_text` multi-line in the range column (else `reference_range_display`) → previous value in a muted line
   `Prev: 7.9 % (12-Mar-2026)` when enabled → interpretation / notes paragraphs → attachments (graphs) scaled to width
   ≤ 120 mm, max 2 per row → per-department signatory block.
4. Typography: low/high **bold**; critical **bold red** with `*`; `not_evaluated` plain with "(range not applicable)"
   footnote. Never colour-only signalling.
5. Page breaks: never split a parameter row; repeat the table header after a break; keep a test title with ≥ 2 rows.
6. Statuses: `PARTIAL REPORT — remaining tests pending` banner on partial releases; `AMENDED — replaces R1 of <date>`
   on amended; `SUPERSEDED` banner when an old release is re-rendered for viewing.
7. Fonts: existing Noto Sans / Devanagari / Telugu assets (patient names in Indic scripts render, as OPD Rx).
8. Determinism: PDF metadata dates = release `created_at`; no random object ids; same input → same SHA-256 (tested).

## 6. Barcode labels (`app/services/dx_barcode.py` + `dx_pdf.render_labels`)

| Item | Spec |
|:-|:-|
| Sample ID | 12 digits: `YY` (IST year) + container code `cc` + 8-digit counter from `opd_next_counter(clinic, 'dx_sample:YYcc')` — e.g. `26` `01` `00016570` |
| Symbology | Code128-C for all-numeric even length (12 digits → 101 modules ≈ 25 mm at 0.25 mm/module); Code128-B fallback for pre-printed alphanumeric stock; check digit per Code128 mod-103 (unit-tested against published vectors) |
| Label | 50 × 25 mm default (38 × 25 configurable): barcode with human-readable digits; patient name (truncated 22 chars), age/sex; visit no.; test short codes (≤ 30 chars, "+3" overflow); tube colour bar using `dx_containers.colour_hex`; site code |
| Output | PDF sized to the label (browser print, printer driver set to the label size) and **ZPL** (`^XA … ^BCN … ^XZ`) download for Zebra/TSC printers in ZPL emulation; direct USB printing via a local print agent is DEFERRED |
| Count | one label per sample + `extra_labels` setting (aliquot/ backup) |

## 7. Bill print
Visit bill (A5 or 80 mm thermal, template setting): lab letterhead, visit no., MRN, patient, referrer, items with
MRP / price / discount, total, paid, due, payment modes, `INV-` number, QR of the visit number for quick lookup at
collection. Reuses `opd_pdf.render_invoice_pdf` where the layout fits; DX adds the thermal variant.

## 8. Seed library and imports
- `app/assets/dx_seed/tests_v1.json` (P2): ~150 common Indian tests and panels (CBC with differential and indices,
  ESR, blood group, LFT, RFT/KFT, electrolytes, lipid, thyroid T3/T4/TSH and free, HbA1c, glucose F/PP/R, urine
  routine & microscopy, stool routine, CRP, uric acid, calcium, phosphorus, vitamin D, B12, iron studies, ferritin,
  PT/INR, aPTT, Widal, dengue NS1/IgM/IgG, malaria antigen, HIV/HBsAg/HCV screens, PSA, beta-hCG, semen analysis…)
  with codes, units, methods, containers, departments, formulas and **indicative** bands marked
  `"source": "indicative — verify against your analyser and kit insert"`.
- Import creates tests and parameters **inactive and unapproved**; the lab director reviews each test's bands on one
  screen and approves; approval is audit-logged with the approver's name.
- CSV/XLSX parameter import (one row per band) with a dry-run report (unknown units, overlapping bands, invalid
  formulas, duplicates) before commit — same pattern as `price_list_parser.py`.
- PDF import (deterministic `pdfplumber` table extraction of an existing report) produces drafts only.
