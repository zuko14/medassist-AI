# Task 2 — Plan registry: the four plans everywhere a plan slug is validated or shown

**Files:**
- Modify: `app/services/tenant.py` (plan comment block ~line 420–434; `PLAN_FEATURES` ~435; `FEATURE_LABELS` ~550; after `require_feature` ~625)
- Modify: `app/services/permissions.py` (`PERMISSIONS` frozenset)
- Modify: `app/tenancy.py` (`TENANT_OWNED_TABLES`)
- Modify: `app/services/message_accounting.py` (fallback dict ~line 261)
- Modify: `app/routers/platform.py` (`clinics_by_plan` ~356; `valid_plans` ~1603)
- Modify: `app/routers/clinics.py` (`CreateClinicRequest.plan` ~58; `UpdateClinicRequest.plan` ~275)
- Modify: `admin/platform.html` (badge CSS ~580; `#planFilter` ~1020; `#ccPlan` ~1490)
- Test: `tests/test_specialty_plans.py`

**Interfaces:**
- Consumes: Task 1 schema, which accepts the four slugs.
- Produces:
  - `SPECIALTY_BY_PLAN: dict[str, str]` and `specialty_enabled(clinic: Optional[dict]) -> bool` in `app.services.tenant`;
  - feature `"specialty_treatments"`;
  - permission `"TREATMENTS_MANAGE"`;
  - both new tables registered in `TENANT_OWNED_TABLES`.

---

- [x] **Step 1: Write the failing test** — create `tests/test_specialty_plans.py`:

```python
"""Specialty plans are registered everywhere, and the six existing plans are
byte-for-byte unchanged."""

import re
from pathlib import Path

import pytest

from app.services.tenant import (
    ALL_FEATURES,
    FEATURE_LABELS,
    PLAN_FEATURES,
    SPECIALTY_BY_PLAN,
    has_feature,
    specialty_enabled,
)

REPO = Path(__file__).resolve().parent.parent
SPECIALTY_PLANS = ["derma", "eye", "dental", "ivf"]

# Snapshot of the six production plans taken BEFORE this change. If this test
# fails, an existing tenant's features changed — that is a regression, not a
# snapshot to update.
EXISTING_PLAN_SNAPSHOT = {
    "soloclinic": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                   "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                   "compliance_nmc", "payments_razorpay"},
    "diagstream": {"multilingual", "emergency_escalation", "clinical_firewall", "compliance_dpdp",
                   "compliance_nmc", "lab_reports", "diagnostic_reports", "ai_report_summary",
                   "pii_sanitization", "multi_branch", "lab_test_booking", "payments_razorpay",
                   "holiday_calendar"},
    "diagbooking": {"lab_test_booking", "payments_razorpay", "admin_dashboard", "holiday_calendar",
                    "multi_branch", "multilingual", "emergency_escalation", "clinical_firewall",
                    "compliance_dpdp", "compliance_nmc"},
    "essential": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                  "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                  "compliance_nmc", "lab_reports", "ai_report_summary", "pii_sanitization", "feedback",
                  "analytics", "multi_department", "payments_razorpay", "staff_training"},
    "polyclinic": {"booking", "reminders", "multilingual", "emergency_escalation", "clinical_firewall",
                   "admin_dashboard", "roster_management", "holiday_calendar", "compliance_dpdp",
                   "compliance_nmc", "lab_reports", "diagnostic_reports", "ai_report_summary",
                   "pii_sanitization", "feedback", "analytics", "multi_department", "payments_razorpay",
                   "staff_training", "multi_branch", "lab_test_booking"},
    "enterprise": {"*"},
}


@pytest.mark.parametrize("plan", list(EXISTING_PLAN_SNAPSHOT))
def test_existing_plans_are_unchanged(plan):
    assert set(PLAN_FEATURES[plan]) == EXISTING_PLAN_SNAPSHOT[plan]


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_specialty_plans_can_book_take_payments_and_run_branches(plan):
    clinic = {"plan": plan}
    for feature in ("booking", "payments_razorpay", "multi_branch", "reminders",
                    "specialty_treatments", "roster_management", "holiday_calendar",
                    "admin_dashboard", "clinical_firewall", "compliance_dpdp"):
        assert has_feature(clinic, feature), f"{plan} lacks {feature}"
    # Specialty plans do not dispatch lab reports.
    assert not has_feature(clinic, "lab_reports")


def test_only_ivf_books_lab_tests():
    assert has_feature({"plan": "ivf"}, "lab_test_booking")
    for plan in ("derma", "eye", "dental"):
        assert not has_feature({"plan": plan}, "lab_test_booking")


def test_specialty_by_plan_mapping():
    assert SPECIALTY_BY_PLAN == {
        "derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "fertility",
    }


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": "derma"}, True),
    ({"plan": "ivf", "features": {}}, True),
    ({"plan": "eye", "features": {"specialty_treatments": False}}, False),
    # The enterprise wildcard must NOT switch the specialty flow on.
    ({"plan": "enterprise"}, False),
    ({"plan": "enterprise", "features": {"specialty_treatments": True}}, True),
    ({"plan": "polyclinic"}, False),
    ({"plan": "polyclinic", "features": {"specialty_treatments": True}}, True),
    ({"plan": "soloclinic", "features": {"specialty_treatments": "yes"}}, False),
    ({}, False),
    (None, False),
])
def test_specialty_enabled_truth_table(clinic, expected):
    assert specialty_enabled(clinic) is expected


def test_new_feature_has_a_label_and_is_listed():
    assert "specialty_treatments" in ALL_FEATURES
    assert FEATURE_LABELS["specialty_treatments"] == "Treatments & Procedures Catalog"
    assert set(FEATURE_LABELS) == set(ALL_FEATURES)


def test_treatments_permission_is_registered_but_in_no_preset():
    from app.services.permissions import PERMISSIONS, ROLE_PRESETS

    assert "TREATMENTS_MANAGE" in PERMISSIONS
    for role, grants in ROLE_PRESETS.items():
        assert "TREATMENTS_MANAGE" not in grants, role


def test_new_tables_are_tenant_owned():
    from app.tenancy import TENANT_OWNED_TABLES

    assert {"specialty_treatments", "treatment_doctors"} <= TENANT_OWNED_TABLES


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_onboarding_and_update_validators_accept_the_plan(plan):
    from app.routers.clinics import CreateClinicRequest, UpdateClinicRequest

    req = CreateClinicRequest(
        name="Test Specialty", whatsapp_number="+919876543210", plan=plan,
        meta_phone_number_id="000000000000", meta_access_token="EAAG_test",
    )
    assert req.plan == plan
    assert UpdateClinicRequest(plan=plan).plan == plan


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_platform_router_lists_the_plan(plan):
    src = (REPO / "app" / "routers" / "platform.py").read_text(encoding="utf-8")
    valid_block = src.split("valid_plans = {")[1].split("}")[0]
    assert f'"{plan}"' in valid_block
    by_plan_block = src.split("clinics_by_plan = {")[1].split("}")[0]
    assert f'"{plan}": 0' in by_plan_block


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_message_accounting_fallback_lists_the_plan(plan):
    src = (REPO / "app" / "services" / "message_accounting.py").read_text(encoding="utf-8")
    assert re.search(rf'"{plan}": \{{"included_messages_month": 2500', src)


@pytest.mark.parametrize("plan", SPECIALTY_PLANS)
def test_platform_console_offers_and_styles_the_plan(plan):
    html = (REPO / "admin" / "platform.html").read_text(encoding="utf-8")
    assert f".badge-{plan} " in html
    create_block = html.split('id="ccPlan"')[1].split("</select>")[0]
    assert f'value="{plan}"' in create_block
    filter_block = html.split('id="planFilter"')[1].split("</select>")[0]
    assert f'value="{plan}"' in filter_block
```

- [x] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_plans.py -q
```
Expected: `ImportError: cannot import name 'SPECIALTY_BY_PLAN'`.

- [x] **Step 3: `app/services/tenant.py`, plan comment.** Directly after the line
```python
#   enterprise  — Unlimited (all current + future features via wildcard)
```
add:
```python
#   derma       — Dermatology, skin & hair specialty clinic (treatments catalogue)
#   eye         — Eye hospital (treatments catalogue)
#   dental      — Dental clinic / hospital (treatments catalogue)
#   ivf         — IVF & fertility centre (treatments catalogue + lab test booking)
```

- [x] **Step 4: `app/services/tenant.py`, shared specialty feature set.** Directly **before** the line `PLAN_FEATURES: dict[str, set[str]] = {` insert:

```python
# Specialty hospitals (migration 077). A doctor clinic's core, plus the
# treatments catalogue. multi_branch and staff_training are included because
# the target clients (dental, eye and fertility chains) run several centres.
# lab_reports is NOT included: these plans have no report connector.
_SPECIALTY_FEATURES: frozenset[str] = frozenset({
    "booking",
    "reminders",
    "multilingual",
    "emergency_escalation",
    "clinical_firewall",
    "admin_dashboard",
    "roster_management",
    "holiday_calendar",
    "compliance_dpdp",
    "compliance_nmc",
    "payments_razorpay",
    "analytics",
    "feedback",
    "multi_branch",
    "staff_training",
    "specialty_treatments",
})

```

- [x] **Step 5: `app/services/tenant.py`, register the plans.** Replace exactly:
```python
        "multi_branch",  # Multi-branch support
        "lab_test_booking",
    },
    "enterprise": {
```
with:
```python
        "multi_branch",  # Multi-branch support
        "lab_test_booking",
    },
    "derma": set(_SPECIALTY_FEATURES),
    "eye": set(_SPECIALTY_FEATURES),
    "dental": set(_SPECIALTY_FEATURES),
    # Fertility centres run their own hormone / semen tests (AMH, semen
    # analysis). lab_test_booking implies payments_razorpay, which the shared
    # set already carries (test_lab_test_booking_always_implies_razorpay_payments).
    "ivf": set(_SPECIALTY_FEATURES) | {"lab_test_booking"},
    "enterprise": {
```

- [x] **Step 6: `app/services/tenant.py`, label.** Replace exactly:
```python
    "roster_management": "Doctor Roster & Leave",
```
with:
```python
    "roster_management": "Doctor Roster & Leave",
    "specialty_treatments": "Treatments & Procedures Catalog",
```

- [x] **Step 7: `app/services/tenant.py`, gate function.** Directly **before** the line `# ─── Branch Resolution ───` (the comment block after `require_feature`) insert:

```python
# ─── Specialty hospitals (migration 077) ─────────────────────────────────────
#: Plan slug -> specialty. The specialty picks the starter treatment list and
#: the WhatsApp examples; it is never taken from a URL or a request body.
SPECIALTY_BY_PLAN: dict[str, str] = {
    "derma": "dermatology",
    "eye": "ophthalmology",
    "dental": "dental",
    "ivf": "fertility",
}


def specialty_enabled(clinic: Optional[dict]) -> bool:
    """True when the treatments catalogue and specialty WhatsApp flow apply.

    Deliberately NOT has_feature(clinic, "specialty_treatments"): the
    enterprise plan is a "*" wildcard, so has_feature() is True for every
    enterprise clinic, and switching their WhatsApp menu on silently would be a
    production change nobody asked for.

      * specialty plans: on, unless the owner set an explicit False override;
      * every other plan (enterprise included): on only with an explicit True
        override in clinics.features (PATCH /platform/clinics/{id}/features).
    """
    if not clinic:
        return False
    overrides = clinic.get("features") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    if clinic.get("plan") in SPECIALTY_BY_PLAN:
        return overrides.get("specialty_treatments") is not False
    return overrides.get("specialty_treatments") is True

```

- [x] **Step 8: `app/services/permissions.py`.** Replace exactly:
```python
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
})
```
with:
```python
    "CONNECTOR_MANAGE",
    "LAB_TESTS_MANAGE",
    # Create/edit/delete/publish treatments. Deliberately in no role preset: a
    # clinic_admin grants it per staff account.
    "TREATMENTS_MANAGE",
})
```
Note: the similar block `"CONNECTOR_MANAGE",\n    "LAB_TESTS_MANAGE",\n]` further down, inside `_DIAGNOSTIC_OPERATOR_GRANTS`, ends with `]`, not `})`. Do not edit that one.

- [x] **Step 9: `app/tenancy.py`.** Replace exactly:
```python
    "clinic_daily_usage",
})
```
with:
```python
    "clinic_daily_usage",
    # Migration 077. Both carry clinic_id (treatment_doctors unlike
    # doctor_branches), so scoped_query() predicates are valid on each.
    "specialty_treatments", "treatment_doctors",
})
```

- [x] **Step 10: `app/services/message_accounting.py`.** Replace exactly:
```python
        "polyclinic": {"included_messages_month": 5000, "display_name": "PolyClinic"},
```
with:
```python
        "polyclinic": {"included_messages_month": 5000, "display_name": "PolyClinic"},
        "derma": {"included_messages_month": 2500, "display_name": "Dermatology & Hair"},
        "eye": {"included_messages_month": 2500, "display_name": "Eye Hospital"},
        "dental": {"included_messages_month": 2500, "display_name": "Dental Clinic"},
        "ivf": {"included_messages_month": 2500, "display_name": "IVF & Fertility"},
```

- [x] **Step 11: `app/routers/platform.py`.**

a) Replace exactly:
```python
            "polyclinic": 0,
            "enterprise": 0,
        }
```
with:
```python
            "polyclinic": 0,
            "enterprise": 0,
            "derma": 0,
            "eye": 0,
            "dental": 0,
            "ivf": 0,
        }
```
b) Replace exactly:
```python
    valid_plans = {
        "soloclinic", "diagstream", "diagbooking", "essential", "polyclinic",
        "enterprise",
    }
```
with:
```python
    valid_plans = {
        "soloclinic", "diagstream", "diagbooking", "essential", "polyclinic",
        "enterprise", "derma", "eye", "dental", "ivf",
    }
```

- [x] **Step 12: `app/routers/clinics.py`.** There are two occurrences of the Literal body (Create at ~58, Update at ~275). In **both**, replace:
```python
        "soloclinic", "diagstream", "diagbooking", "essential", "polyclinic",
        "enterprise",
```
(the Update one is indented four more spaces) with the same lines plus the new slugs:
```python
        "soloclinic", "diagstream", "diagbooking", "essential", "polyclinic",
        "enterprise", "derma", "eye", "dental", "ivf",
```
Keep each occurrence's original indentation.

- [x] **Step 13: `admin/platform.html`.**

a) Replace exactly:
```css
        .badge-diagbooking { background: rgba(56,189,248,0.12); color: #38bdf8; }
```
with:
```css
        .badge-diagbooking { background: rgba(56,189,248,0.12); color: #38bdf8; }
        .badge-derma { background: rgba(244,114,182,0.12); color: #f472b6; }
        .badge-eye { background: rgba(45,212,191,0.12); color: #2dd4bf; }
        .badge-dental { background: rgba(165,180,252,0.14); color: #a5b4fc; }
        .badge-ivf { background: rgba(251,146,60,0.12); color: #fb923c; }
```
b) In `<select class="filter-select" id="planFilter" …>`, replace exactly:
```html
                            <option value="diagbooking">Diagnostic Test Booking</option>
                        </select>
```
with:
```html
                            <option value="diagbooking">Diagnostic Test Booking</option>
                            <option value="derma">Dermatology &amp; Hair</option>
                            <option value="eye">Eye Hospital</option>
                            <option value="dental">Dental Clinic</option>
                            <option value="ivf">IVF &amp; Fertility</option>
                        </select>
```
c) In `<select class="form-control" id="ccPlan" …>`, replace exactly:
```html
                        <option value="diagbooking">Diagnostic Test Booking — lab-test booking &amp; payments only</option>
```
with:
```html
                        <option value="diagbooking">Diagnostic Test Booking — lab-test booking &amp; payments only</option>
                        <option value="derma">Dermatology &amp; Hair — skin, hair &amp; aesthetic clinic</option>
                        <option value="eye">Eye Hospital — cataract, LASIK, retina &amp; eye care</option>
                        <option value="dental">Dental Clinic — dental care, braces &amp; implants</option>
                        <option value="ivf">IVF &amp; Fertility Centre — fertility treatments &amp; lab tests</option>
```
Do **not** add specialty plans to `PLANS_WITH_LAB_REPORTS`. `tests/test_diagbooking_admin_visibility.py` checks that list against the backend, and specialty plans have no `lab_reports`.

- [x] **Step 14: Run the new test and the registry regression tests**

```bash
pytest tests/test_specialty_plans.py tests/test_plan_features.py tests/test_platform_roster_and_plan_features.py tests/test_diagbooking_admin_visibility.py tests/test_lab_tests_admin.py tests/test_admin_me.py tests/test_forensic_hardening_suite.py tests/test_phase4_scoped_queries.py tests/test_production_launch_gates.py tests/test_clinic_settings.py -q
```
Expected: all PASS.
- If a test fails because it asserts the **exact** contents of `TENANT_OWNED_TABLES` or `PERMISSIONS`, add the new entries to that test's expected set in this commit. That is a legitimate registry update.
- Any other failure is a regression: stop and fix the code, not the test.

Run the orphan check.

- [x] **Step 15: Commit**

```bash
git add app/services/tenant.py app/services/permissions.py app/tenancy.py app/services/message_accounting.py app/routers/platform.py app/routers/clinics.py admin/platform.html tests/test_specialty_plans.py
git commit -m "feat(plans): register derma, eye, dental and ivf specialty plans

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
