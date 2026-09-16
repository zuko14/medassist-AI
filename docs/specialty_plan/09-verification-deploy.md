# Tasks 9–11 — Regression verification, production deployment, acceptance

## Task 9 — Full targeted regression (no code changes)

- [x] **Step 1: Confirm the branch contains exactly the expected commits**

```bash
git log --oneline main..feat/specialty-plans
git diff --stat main..feat/specialty-plans
```
Expected: 9 commits (Tasks 1, 2, 3, 4, 5, 6, 7A, 7B, 8). The changed files are only those named in the tasks, plus the new tests and docs. **Any other changed file is scope creep: revert it.**

- [x] **Step 2: Prove no forbidden change slipped in**

```bash
git diff main..feat/specialty-plans -- app/services/clinical_firewall.py app/services/scheduler.py app/utils/helpers.py migrations/0*.sql | grep -v "^+++\|^---" | grep "^[+-]" | grep -v "077_specialty_plans_and_treatments" || echo "OK: firewall, scheduler, slot helpers and old migrations untouched"
git diff main..feat/specialty-plans | grep -n "treatment_procedure" && echo "FAIL: forbidden booking_type" || echo "OK: no new booking_type"
git diff main..feat/specialty-plans -- tests/ | grep "^-[^-]" | head -40
```
- The last command shows **removed** lines in tests. The only acceptable removals are registry expectations updated as described in Tasks 2 and 8.
- Every other removed test line must be justified in the session doc, or restored.

- [x] **Step 3: Run every test file touched by, or guarding, this feature** (one command; **never** the bare full suite, **never** `tests/test_multi_worker_smoke.py`)

```bash
pytest \
  tests/test_specialty_migration_077.py tests/test_specialty_plans.py tests/test_specialty_catalog.py \
  tests/test_specialty_ai.py tests/test_specialty_treatments_admin.py tests/test_specialty_admin_ui.py \
  tests/test_specialty_whatsapp_flow.py tests/test_specialty_conversation_wiring.py tests/test_specialty_booking_payment.py \
  tests/test_plan_features.py tests/test_platform_roster_and_plan_features.py tests/test_diagbooking_admin_visibility.py \
  tests/test_admin_me.py tests/test_lab_tests_admin.py tests/test_lab_tests_branch_scoping.py tests/test_lab_tests_unique_name_migration.py \
  tests/test_lab_test_booking_conversation.py tests/test_lab_test_booking_payment.py tests/test_lab_test_search.py tests/test_lab_booking_production_fixes.py \
  tests/test_conversation_navigation_and_timeout.py tests/test_conversation_session_timeout.py tests/test_conversation_payment_mode.py \
  tests/test_conversation_unreadable_messages.py tests/test_conversation_admin_sync_and_csv.py tests/test_webhook.py \
  tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_tenant_isolation_matrix.py \
  tests/test_lint_unscoped_queries.py tests/test_phase2_unscoped_query_linter.py tests/test_phase4_scoped_queries.py \
  tests/test_forensic_hardening_suite.py tests/test_production_launch_gates.py tests/test_rls_security.py tests/test_security.py \
  tests/test_clinic_settings.py tests/test_insights.py tests/test_appointments_list.py tests/test_admin_panel_appointments_filter.py \
  tests/test_razorpay_webhook_default_clinic.py tests/test_phase1_payment_integrity.py tests/test_real_postgres_invariants.py \
  tests/test_openrouter.py tests/test_ai_engine.py tests/test_appointment.py \
  $(ls tests/test_payment*.py) -q
```
Expected: **0 failures, 0 errors.**
- If a failure also occurs on `main` (check with `git stash` or a clean `main` checkout of the same file), record it in the session doc as pre-existing, with its output.
- Any failure that does **not** occur on `main` blocks deployment.

- [x] **Step 4: Orphan check** (`00-global-constraints.md` §E). Kill orphans. Record in the session doc that it was done.

- [x] **Step 5: Independent review.** Before any deploy, request a code review of `main..feat/specialty-plans` focused on:
  1. tenant isolation (every new query scoped);
  2. `specialty_enabled` gating (no enterprise leak);
  3. the booking path (no new booking_type, slot guard intact);
  4. WhatsApp limits;
  5. clinical copy.

  Fix CONFIRMED findings in new commits and re-run Step 3.

---

## Task 10 — Production deployment runbook (owner-supervised)

**Order is mandatory:**
1. Database first.
2. Verify.
3. Then code.
4. Then smoke test.

Never deploy code before migration 077 is verified in production.

- [ ] **Step 1: Pre-flight (owner)**
  1. Confirm a Supabase backup or PITR point exists from the last hour. Note its timestamp in the session doc.
  2. Pick a low-traffic window (for Indian clinics, typically 22:30–06:00 IST).
  3. Confirm with the Render dashboard, not `render.yaml`, whether `preDeployCommand: python scripts/migrate.py` actually runs on the production web service. The configured topology may differ from the file.

- [ ] **Step 2: Merge**

```bash
git checkout main
git pull
git merge --no-ff feat/specialty-plans
```
Do **not** push yet if the migration will be applied manually (Step 3b).

- [ ] **Step 3: Apply migration 077**

- **3a — if Render's preDeploy migration is confirmed active:** it runs on push, before the new code starts. Continue at Step 4 immediately after the deploy's pre-deploy phase finishes, and **before** checking the app.
- **3b — if not (apply manually from a trusted machine):**
  ```bash
  python scripts/migrate.py --url "$DATABASE_URL" --status
  python scripts/migrate.py --url "$DATABASE_URL" --dry-run
  python scripts/migrate.py --url "$DATABASE_URL"
  ```
  Expected: exactly one migration applied, `077_specialty_plans_and_treatments.sql`. If it fails with `lock_timeout`, wait five minutes and re-run; the migration is re-runnable and nothing was committed.

- [ ] **Step 4: Verify the schema in production** (Supabase SQL editor)

```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'clinics_plan_check';
SELECT plan_name, included_messages_month FROM plan_tiers WHERE plan_name IN ('derma','eye','dental','ivf');
SELECT column_name, is_nullable FROM information_schema.columns
 WHERE table_name = 'appointments' AND column_name IN ('treatment_id','treatment_name');
SELECT to_regclass('public.specialty_treatments'), to_regclass('public.treatment_doctors');
SELECT indexdef FROM pg_indexes WHERE indexname IN ('uq_appointment_active_slot','uq_appointment_active_slot_unassigned');
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'appointments_booking_type_check';
SELECT plan, COUNT(*) FROM clinics GROUP BY plan ORDER BY plan;
```
Expected:
- the plan check lists 10 plans;
- 4 plan_tiers rows at 2500;
- both columns exist with `is_nullable = YES`;
- both tables exist;
- **both slot indexes still contain `booking_type = 'consultation'`**;
- the booking_type check still lists only `consultation` and `lab_test`;
- clinic counts per plan equal the pre-deploy counts. Record both sets of counts in the session doc.

Then run `migrations/verify_supabase_schema.sql` and confirm it reports no missing tables or columns.

- [ ] **Step 5: Deploy the code**
  1. `git push origin main` (or trigger the Render deploy).
  2. Wait for the deploy to become live.
  3. Confirm `/health` returns the merge commit SHA.

- [ ] **Step 6: Production smoke test on EXISTING tenants (first 15 minutes)**
  1. **Existing clinic, WhatsApp:** from the owner's own test phone, message one existing live clinic of each available type: soloclinic / polyclinic / diagnostics.
     - "hi" → the main menu rows are **identical** to before: Book Appointment, Our Services, Our Doctors, [Book Lab Test], Emergency, Talk to Staff.
     - Book one test appointment through to the confirmation or payment link, then cancel it.
  2. **Existing clinic, admin panel:** log in to `/admin-panel`.
     - There is **no** Treatments tab.
     - Appointments, Payments and Insights load without errors.
  3. **Enterprise clinic** (if one exists): no Treatments tab, no treatment rows in WhatsApp.
  4. **Logs:** Render logs show no new `ERROR` lines mentioning `treatment`, `specialty`, `column … does not exist`, or `PGRST`.

  If any check fails: **roll back the code** (redeploy the previous commit from Render). The migration is additive and can stay in place while you investigate.

- [ ] **Step 7: Rollback procedure** (only if needed; owner decision)
  1. **Code:** in Render, redeploy the previous successful deploy. This alone restores all existing behaviour; the additive schema is harmless to old code.
  2. **Schema** (only if the feature is being abandoned):
     1. Export the tags using the `COPY` query in `migrations/rollback/077_down.sql`.
     2. Move any specialty clinics to another plan.
     3. Run `migrations/rollback/077_down.sql`.
     4. Delete the `schema_migrations` row for `077_specialty_plans_and_treatments.sql`.

---

## Task 11 — Acceptance test with a sandbox specialty clinic (owner + agent)

Use the Meta test number or an existing sandbox number and a clinic flagged `is_sandbox`, never a live client's number. One number can test all four plans by changing the sandbox clinic's plan between runs (PATCH `/admin/clinics/{id}` with the X-Admin-Secret, or the owner console).

- [ ] **A. Onboarding**
  1. In `/platform-panel` → New Hospital, check that the Plan dropdown lists Dermatology & Hair, Eye Hospital, Dental Clinic and IVF & Fertility Centre.
  2. Create "Sandbox Skin Clinic" on `derma`.
  3. The response shows `starter_treatments.added` = 15.
  4. The leaderboard filter has the four new plans, and the badge is styled (pink for derma).
  5. Plan Tiers widget: the four plans appear, their quota and price are editable, and Treatments & Procedures Catalog is ticked for them.

- [ ] **B. Admin panel**
  1. Log in at `/derma-panel` with the generated admin. Check that the page is identical to `/admin-panel` and the Treatments tab is visible.
  2. Treatments page:
     - 15 rows, all hidden;
     - the banner explains review;
     - the Payment Settings tab is visible;
     - the Branches tab is visible.
  3. Add a doctor (Dermatology). Add a second branch and assign the doctor to one branch only.
  4. Edit "Hair PRP Therapy":
     1. Set the price to 4000 and duration to 60.
     2. Tick the doctor.
     3. Click ✨ Generate with AI → EN/HI/TE drafts appear. Edit a word, then save.
     4. The row shows ₹4,000, the doctor's name, and "Active" after "Show to patients".
  5. Tick 3 more starters → Show to patients. Delete one starter.
  6. Add a custom treatment "HydraFacial Glow" (category Laser & Aesthetic).
     - Typing a 30-character name without a list title shows the save error.
     - Adding the title "HydraFacial" saves.
  7. Create a staff account without `TREATMENTS_MANAGE`: they see the catalog read-only (no buttons). Grant the permission: the buttons appear.
  8. Payment Settings: enter Razorpay **test** keys and set mode "full".

- [ ] **C. WhatsApp: patient journey (derma)**
  1. "hi" → language → consent → menu. The first rows are ✨ Our Treatments and 🔍 Find by Concern; there is no "Our Services".
  2. Our Treatments → categories → Hair & Scalp → Hair PRP Therapy → the card shows the 2-line description, ₹4,000 "Starts from", specialist name, Before your visit, the disclaimer, and 3 buttons.
  3. Book Consultation:
     1. Branch choice (2 branches).
     2. Choose the branch where the doctor is **not** assigned → "no specialist … Request Callback".
     3. Restart and choose the correct branch → For Me → only the mapped doctor is listed → date → slot → the confirmation shows 🩺 Treatment: Hair PRP Therapy → Confirm.
     4. The Razorpay test link arrives; pay with a test card → payment confirmation, then the "Before your visit — Hair PRP Therapy" message.
  4. Admin panel → Appointments: the row shows the doctor plus a purple "Hair PRP Therapy" chip. Payments shows the same. The clinic admin phone received the alert with 🩺 Treatment.
  5. **Double-booking check:** from a second phone, book the same doctor, date and slot through Book Appointment (no treatment) while the first booking is active → the slot is not offered, or "slot taken".
  6. Request Callback on a card: the patient gets the thank-you, the admin phone gets an alert, and an admin notification appears. Tap again within 24h → "already have your request".
  7. Find by Concern → "hair fall" → Hair PRP / GFC / Hair Fall Evaluation are listed. "marks after pimples" → AI suggests Acne Scar Treatment (or the categories fallback if AI is down). Type an emergency word → emergency flow.
  8. Type "what treatment for acne scars" → clinical firewall safe reply **plus** the "Our Treatments" button.
  9. Abandon a treatment booking midway, send "menu" → Book Appointment → finish a normal booking → the admin row has **no** treatment chip.
  10. Hide all treatments in admin → "hi" → the menu is back to the normal rows (no treatment rows).

- [ ] **D. Repeat quickly for `eye`, `dental`, `ivf`**
  - Switch the sandbox clinic's plan and use "Load starter treatments". It adds the new specialty's list; the old rows stay until deleted.
  - The concern prompt examples change per plan.
  - IVF only: the menu also shows 🧪 Book Lab Test.

- [ ] **E. Record results.** Write each check with pass/fail and evidence (screenshots or log lines) into the session doc (`10-session-memory.md`).
