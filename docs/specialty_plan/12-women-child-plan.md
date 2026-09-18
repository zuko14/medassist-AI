# Task 12 — The `womenchild` plan: Women & Child hospitals

**Status:** IMPLEMENTED (migration 082)

Rainbow Children's / BirthRight-style hospitals run three service lines under
one roof — **Child Care** (≈40 paediatric sub-specialties, NICU, therapies),
**Women Care** (gynaecology, pregnancy, delivery, fetal medicine) and
**Fertility Care** (IUI, IVF, ICSI, egg freezing). Each has its own doctors,
its own OPDs and its own treatments.

---

## The two ideas that carry the plan

**1. It is a hybrid, exactly like `multispecialty`.** Paediatric and obstetric
OPDs are *departments* with doctors, the lab and scans are the diagnostics
half, and the treatments catalogue sits on top. So:

```python
PLAN_FEATURES["womenchild"] = set(PLAN_FEATURES["multispecialty"])
HYBRID_SPECIALTY_PLANS = frozenset({"multispecialty", "womenchild"})
```

**2. Treatments are filed under service lines.** One nullable column,
`specialty_treatments.service_line`. Categories alone cannot carry three
service lines — "Surgery" exists in Women Care *and* in Fertility Care, and a
flat list of fifteen category headings is what a parent scrolls past.

| slug | patient sees (en) | hi | te |
| :-- | :-- | :-- | :-- |
| `child_care` | 👶 Child Care | शिशु व बाल देखभाल | పిల్లల సంరక్షణ |
| `women_care` | 🌸 Women Care | महिला स्वास्थ्य | మహిళల ఆరోగ్యం |
| `fertility_care` | 🌱 Fertility Care | प्रजनन देखभाल | సంతాన సాఫల్యం |
| `skin_hair` / `eye_care` / `dental_care` | for multispecialty hospitals | | |

The WhatsApp flow shows a service-line picker **only when the published
treatments span two or more lines.** That single rule is the zero-regression
property: every row that existed before 082 is NULL, a clinic that never
files anything sees today's flow, and a single-specialty clinic (one line at
most) sees today's flow.

## What the patient sees

```
Main menu (9 of Meta's 10 rows)
  🩺 Book Consultation      → Child / Gynae / Fertility consultation (3 rows, offered not guessed)
  🔍 Not sure? Tell us      → "fever in my child, pregnancy check-up, irregular periods"
  ✨ What We Treat          → 👶 Child Care · 🌸 Women Care · 🌱 Fertility Care
  Book Appointment · Our Services (departments) · Our Doctors · 🧪 Book Lab Test
  Emergency · Talk to Staff
```

`What We Treat → 👶 Child Care → Child Specialists → Child Heart Care → card`.
The care pathway from session 10 applies unchanged: NICU, delivery, epidural,
VBAC and gynae surgery are **doctor-decided** — their card is information and
its button books an examination.

## Real-world problems this plan pre-empts

| Situation | What Kriya does |
| :-- | :-- |
| Parent books for a child | Existing "Who is this for?" family-member flow — the child's name goes on the booking, not the parent's. |
| "Is it a boy or girl?" / "baby gender scan" | **PCPNDT Act guard** in the clinical firewall: a legal refusal in en/hi/te, before any LLM call. Applies to every tenant — it is national law. A patient writing "Gender: Female, need pregnancy scan" is *not* refused. |
| "My water broke", "baby not moving", "convulsion" | Emergency path: emergency number, location, staff alert. "Painless delivery?" and "labour pain relief cost" are deliberately *not* emergencies. |
| Parent does not know which sub-specialist | Entry rows: *Paediatric Consultation* / *Gynaecology Consultation* lead the menu; the paediatrician refers onward. |
| Patient asks for a C-section / epidural by name | Doctor-decided card: explains, books an examination, never the procedure. |
| Referred to a child heart / kidney specialist | Those rows are `direct` — the parent can book that specialist by name. |
| Scan booking | Prep note states the sex of the baby is not disclosed, as required by law. |

## Admin panel (`/women-child-panel`)

Treatments page gains **section tabs** — *All · 👶 Child Care · 🌸 Women Care ·
🌱 Fertility Care · No section* — with counts, a **Section** field on the
treatment form (pre-filled from the open tab), and a Section column. A new
Women & Child clinic is seeded at onboarding with all three starter lists,
hidden, each filed under its line (16 child, 20 women, 12 fertility rows).

Clinics that are not hybrid see none of this: no tabs, no field, and their
save payload does not carry `service_line` at all.

## Files

| File | Change |
| :-- | :-- |
| `migrations/082_women_child_plan.sql` (+ rollback) | Widen two plan CHECKs, tier row (5,000 msgs), `service_line` column + CHECK. No backfill. |
| `app/services/tenant.py` | Plan comment, `PLAN_FEATURES["womenchild"]`, `HYBRID_SPECIALTY_PLANS`. |
| `app/services/specialty_catalog.py` | `SERVICE_LINES`, `STARTER_SERVICE_LINE`, `STARTER_LISTS_BY_PLAN`, `pediatrics` + `womens_health` lists, 8 pathways, concern examples, `seed_starter_treatments(service_line=)`, ordering after existing rows. |
| `app/services/specialty_flow.py` | `service_lines_in`, `in_line`, `line_label`, `show_service_lines`, line threaded through browsing and paging, `trtline_` routing. |
| `app/services/clinical_firewall.py` | PCPNDT sex-determination guard. |
| `app/services/ai_engine.py` | Obstetric / newborn emergency phrases (en + hi). |
| `app/routers/admin.py` | `service_line` on create/update, `_starter_service_line`, `/admin/me` sections. |
| `app/routers/clinics.py` | Plan Literals, `seed_plan_starter_lists`. |
| `app/routers/platform.py`, `app/services/message_accounting.py`, `app/main.py`, `admin/platform.html`, `admin/index.html` | Registry sweep + UI. |
| `tests/test_women_child_plan.py` | 109 tests. |

## Deploy order

1. Apply `migrations/082_women_child_plan.sql` **first** — the new build writes
   `service_line` when a hybrid clinic seeds starter lists.
2. Deploy the application.
3. Owner console → Create Client → **Women & Child Hospital**.
4. Clinic admin at `/women-child-panel`: add departments and doctors, then
   Treatments → each section tab → price, edit, link doctors → *Show to patients*.

Rollback: application first, then `migrations/rollback/082_down.sql`, which
refuses while any clinic is on the plan.
