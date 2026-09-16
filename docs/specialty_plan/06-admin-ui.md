# Task 6 — Admin panel: Treatments page, staff permission, booking chips, specialty URLs

**Files:**
- Modify: `admin/index.html` (sidebar nav, new page section, auth state, `applyFeatureVisibility`, `adoptClinicPlan`, `go()` loaders, `bookingSubjectCell`, staff permission checkboxes, new JS block)
- Modify: `app/main.py` (specialty panel routes)
- Test: `tests/test_specialty_admin_ui.py`

**Interfaces:**
- Consumes: Task 5 endpoints and `/admin/me` keys `specialty` and `specialty_enabled`.
- Produces:
  - `GET /derma-panel`, `/eye-panel`, `/dental-panel` and `/ivf-panel`, all serving `admin/index.html` with the same headers as `/admin-panel`;
  - a Treatments page (`#pg-treatments`), visible only when `mySpecialtyEnabled`.

**Design rule:** reuse the panel's existing classes only (`sec`, `page-head`, `card`, `card-head`, `top-row`, `search-bar`, `btn btn-accent`, `btn btn-ghost`, `tbl-wrap`, `form-card`, `form-row`, `field`) and helpers (`api`, `apiPost`, `apiPut`, `apiDel`, `esc`, `toast`, `confirmDialog`, `loading`, `emptyState`, `badge`, `hasPermission`). No new CSS, colours or fonts: the "Clinical Depth" theme applies automatically, in dark and light modes.

**Why the specialty URLs need no security allowlist change:** both route matrices (`tests/test_admin_super_admin_scope_matrix.py`, `tests/test_phase2_route_adversarial_matrix.py`) only enumerate paths starting with `/admin`. `/derma-panel` is a static HTML page like `/admin-panel`, and every data call inside it goes through the authenticated, tenant-scoped `/admin/*` API.

---

- [ ] **Step 1: Write the failing test** — create `tests/test_specialty_admin_ui.py`:

```python
"""Static and route checks for the specialty admin UI."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO = Path(__file__).resolve().parent.parent
INDEX = (REPO / "admin" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["/derma-panel", "/eye-panel", "/dental-panel", "/ivf-panel"])
def test_specialty_urls_serve_the_same_panel_with_the_same_headers(path):
    client = TestClient(app)
    base = client.get("/admin-panel")
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.text == base.text
    assert resp.headers.get("cache-control") == base.headers.get("cache-control")
    assert resp.headers.get("content-security-policy") == base.headers.get("content-security-policy")


def test_treatments_nav_is_gated_on_specialty_not_features():
    nav = re.search(r'<div class="nav-link"[^>]*data-page="treatments"[^>]*>', INDEX)
    assert nav, "Treatments nav entry missing"
    assert "data-specialty" in nav.group(0)
    assert "data-feature" not in nav.group(0), "enterprise wildcard would show it"


def test_visibility_and_state_wiring():
    assert "let mySpecialtyEnabled = false;" in INDEX
    assert "mySpecialtyEnabled = !!me.specialty_enabled;" in INDEX
    assert "mySpecialtyEnabled = !!scoped.specialty_enabled;" in INDEX
    block = INDEX.split("function applyFeatureVisibility()")[1][:900]
    assert "[data-specialty]" in block
    assert "treatments: loadTreatments" in INDEX


def test_page_and_form_exist_with_whatsapp_limits():
    assert 'id="pg-treatments"' in INDEX
    assert 'id="f-trtShort" maxlength="24"' in INDEX
    assert 'id="f-trtDesc" rows="3" maxlength="400"' in INDEX
    for fn in ("async function loadTreatments()", "async function submitTreatment()",
               "async function generateTreatmentDescription()", "async function loadStarterTreatments()",
               "async function setSelectedTreatmentsActive(active)", "window.delTreatment = async function"):
        assert fn in INDEX, fn


def test_every_treatment_endpoint_used_by_the_page_exists():
    from app.main import app as fastapi_app

    paths = {getattr(r, "path", "") for r in fastapi_app.routes}
    for p in ("/admin/treatments", "/admin/treatments/{treatment_id}", "/admin/treatments/{treatment_id}/doctors",
              "/admin/treatments/status", "/admin/treatments/starter", "/admin/treatments/ai-description"):
        assert p in paths, p


def test_staff_can_be_granted_treatments_permission_in_both_forms():
    assert INDEX.count('value="TREATMENTS_MANAGE"') == 2
    assert 'class="staff-perm-cb" value="TREATMENTS_MANAGE"' in INDEX
    assert 'class="edit-staff-perm-cb" value="TREATMENTS_MANAGE"' in INDEX


def test_booking_rows_show_the_treatment():
    cell = INDEX.split("function bookingSubjectCell(b)")[1][:900]
    assert "b.treatment_name" in cell
    assert "esc(b.treatment_name)" in cell
    assert "b.booking_type === 'lab_test'" in cell


def test_ai_draft_is_labelled_as_a_draft():
    assert "AI text is a draft" in INDEX
```

- [ ] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_admin_ui.py -q
```
Expected: FAIL (404 for `/derma-panel`, missing nav, etc.).

- [ ] **Step 3: `app/main.py`, specialty URLs.** Insert directly **before** the line `@app.get("/panel-assets/chart.umd.min.js")`:

```python
@app.get("/derma-panel")
@app.get("/eye-panel")
@app.get("/dental-panel")
@app.get("/ivf-panel")
async def specialty_admin_panel():
    """Specialty-branded entry points to the same admin panel.

    The URL changes nothing about tenancy, features or data: the page asks
    GET /admin/me who the user is and what their clinic's plan allows, exactly
    as /admin-panel does. Kept outside /admin* on purpose — see the note on
    admin_panel_chartjs below.
    """
    return await admin_panel()
```

- [ ] **Step 4: `admin/index.html`, sidebar nav.** Find the nav item that opens with:
```html
            <div class="nav-link" tabindex="0" data-page="labtests" data-feature="lab_test_booking" onclick="go('labtests',this)">
```
Directly after **that item's closing `</div>`**, insert:
```html
            <div class="nav-link" tabindex="0" data-page="treatments" data-specialty="1" style="display:none" onclick="go('treatments',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15.5l-1.9-4.6L5.5 9l4.6-1.4z"></path><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"></path></svg></span>
                <span class="txt">Treatments</span>
            </div>
```
If the neighbouring nav items use a label element other than `<span class="txt">`, copy their exact label markup instead.

- [ ] **Step 5: `admin/index.html`, page section.** Insert directly **before** `<div id="pg-labtests" class="sec">`:

```html
        <div id="pg-treatments" class="sec">
            <div class="page-head">
                <h2>Treatments &amp; Procedures</h2>
                <p>What patients see under "Our Treatments" on WhatsApp. Only treatments marked active are shown to patients.</p>
            </div>

            <div class="card" id="trtStarterCard" style="margin-bottom:20px; display:none;">
                <div class="card-head"><h3>Starter treatment list</h3></div>
                <div style="padding:16px;">
                    <p id="trtStarterText" style="color:var(--text3); font-size:0.85rem; margin-bottom:12px;"></p>
                    <button type="button" class="btn btn-accent" id="btnLoadStarter" onclick="loadStarterTreatments()">Load starter treatments</button>
                </div>
            </div>

            <div class="top-row">
                <input type="text" class="search-bar" id="trtSearch" placeholder="Search by name, category or concern..." oninput="filterTreatments()" aria-label="Search treatments">
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <select id="trtCategoryFilter" class="search-bar" style="max-width:220px; padding:6px 12px; font-size:0.8rem; height:34px; margin:0;" onchange="filterTreatments()" aria-label="Filter by category">
                        <option value="">All categories</option>
                    </select>
                    <button type="button" class="btn btn-ghost trt-manage" onclick="setSelectedTreatmentsActive(true)">Show to patients</button>
                    <button type="button" class="btn btn-ghost trt-manage" onclick="setSelectedTreatmentsActive(false)">Hide from patients</button>
                    <button type="button" class="btn btn-accent trt-manage" onclick="openAddTreatment()"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-plus"/></svg> Add Treatment</button>
                </div>
            </div>

            <div class="card">
                <div class="card-head">
                    <h3>Treatment Catalog</h3>
                    <span id="trtCount" style="color:var(--text3); font-size:0.8rem;"></span>
                </div>
                <div class="tbl-wrap" id="trtList">
                    <div class="loader"><div class="spin"></div>Loading treatments...</div>
                </div>
            </div>

            <div class="form-card" id="trtFormCard" style="display:none; margin-top:20px;">
                <h3 id="trtFormTitle">Add Treatment</h3>
                <input type="hidden" id="f-trtId">
                <div class="form-row">
                    <div class="field"><label for="f-trtName">Treatment name *</label><input type="text" id="f-trtName" maxlength="120" placeholder="e.g. Root Canal Treatment" oninput="updateTrtShortCounter()"></div>
                    <div class="field"><label for="f-trtCategory">Category *</label><input type="text" id="f-trtCategory" maxlength="60" list="trtCategoryOptions" placeholder="e.g. Tooth Pain &amp; Root Canal"><datalist id="trtCategoryOptions"></datalist></div>
                </div>
                <div class="form-row">
                    <div class="field">
                        <label for="f-trtShort">WhatsApp list title <span id="trtShortCounter" style="color:var(--text3); font-weight:400;"></span></label>
                        <input type="text" id="f-trtShort" maxlength="24" placeholder="Needed when the name is longer than 24 characters" oninput="updateTrtShortCounter()">
                    </div>
                    <div class="field"><label for="f-trtOrder">Display order</label><input type="number" id="f-trtOrder" min="0" max="10000" step="1" value="0"></div>
                </div>
                <div class="form-row">
                    <div class="field">
                        <label for="f-trtPrice">Price starts from (₹)</label>
                        <input type="number" id="f-trtPrice" min="0" step="1" value="0">
                        <p style="color:var(--text3); font-size:0.78rem; margin-top:4px;">Leave 0 to show "Price shared after consultation". WhatsApp payment always uses the doctor's consultation fee.</p>
                    </div>
                    <div class="field"><label for="f-trtDuration">Visit duration (minutes)</label><input type="number" id="f-trtDuration" min="5" max="1440" step="5"></div>
                </div>
                <div class="field">
                    <label for="f-trtConcerns">Concerns it helps with</label>
                    <textarea id="f-trtConcerns" rows="2" maxlength="500" placeholder="Words patients use, comma-separated. e.g. tooth pain, sensitivity, swelling"></textarea>
                </div>
                <div class="field">
                    <label for="f-trtDesc">Patient description, English (2 lines) <span id="trtDescCounter" style="color:var(--text3); font-weight:400;"></span></label>
                    <textarea id="f-trtDesc" rows="3" maxlength="400" oninput="updateTrtDescCounter()"></textarea>
                    <button type="button" class="btn btn-ghost trt-manage" id="btnTrtAi" style="margin-top:8px;" onclick="generateTreatmentDescription()">✨ Generate with AI</button>
                    <p style="color:var(--text3); font-size:0.78rem; margin-top:4px;">AI text is a draft. Read it, correct anything inaccurate, then save. Nothing is sent to patients until you save.</p>
                </div>
                <div class="form-row">
                    <div class="field"><label for="f-trtDescHi">Description, Hindi</label><textarea id="f-trtDescHi" rows="3" maxlength="600"></textarea></div>
                    <div class="field"><label for="f-trtDescTe">Description, Telugu</label><textarea id="f-trtDescTe" rows="3" maxlength="600"></textarea></div>
                </div>
                <div class="field">
                    <label for="f-trtPrep">Before your visit (instructions)</label>
                    <textarea id="f-trtPrep" rows="2" maxlength="600" placeholder="e.g. Stop soft contact lenses 7 days before."></textarea>
                </div>
                <div class="field">
                    <label>Doctors who perform this</label>
                    <div id="f-trtDoctors" style="display:flex; flex-wrap:wrap; gap:12px;"></div>
                    <p style="color:var(--text3); font-size:0.78rem; margin-top:4px;">Leave all unticked to let patients choose any active doctor.</p>
                </div>
                <div class="field"><label><input type="checkbox" id="f-trtActive" checked> Show to patients on WhatsApp</label></div>
                <div style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap;">
                    <button type="button" class="btn btn-accent" id="btnSaveTreatment" onclick="submitTreatment()">Save Treatment</button>
                    <button type="button" class="btn btn-ghost" onclick="cancelTreatmentForm()">Cancel</button>
                </div>
            </div>
        </div>

```

- [ ] **Step 6: `admin/index.html`, auth state.** Replace exactly:
```js
let myStaffRole = null;
```
(the one in the `// ═══════ AUTH ═══════` block) with:
```js
let myStaffRole = null;
let mySpecialty = null;
let mySpecialtyEnabled = false;
```

- [ ] **Step 7: `admin/index.html`, login.** Replace exactly:
```js
    myStaffRole = me.staff_role || null;
```
with:
```js
    myStaffRole = me.staff_role || null;
    mySpecialty = me.specialty || null;
    mySpecialtyEnabled = !!me.specialty_enabled;
```

- [ ] **Step 8: `admin/index.html`, super-admin clinic switch.** In `adoptClinicPlan`, replace exactly:
```js
            myPlan = scoped.plan;
            myFeatures = scoped.features;
```
with:
```js
            myPlan = scoped.plan;
            myFeatures = scoped.features;
            mySpecialty = scoped.specialty || null;
            mySpecialtyEnabled = !!scoped.specialty_enabled;
```

- [ ] **Step 9: `admin/index.html`, visibility.** In `applyFeatureVisibility`, replace exactly:
```js
    document.querySelectorAll('[data-feature]').forEach(el => {
        el.style.display = planAllowsFeature(el.dataset.feature) ? '' : 'none';
    });
```
with:
```js
    document.querySelectorAll('[data-feature]').forEach(el => {
        el.style.display = planAllowsFeature(el.dataset.feature) ? '' : 'none';
    });
    // Specialty tabs follow /admin/me.specialty_enabled, never features[]:
    // the enterprise wildcard lists every feature, which would show the tab
    // to every enterprise clinic.
    document.querySelectorAll('[data-specialty]').forEach(el => {
        el.style.display = mySpecialtyEnabled ? '' : 'none';
    });
```

- [ ] **Step 10: `admin/index.html`, page loader.** In `go()`, replace exactly:
```js
labtests: loadLabTests,
```
with:
```js
labtests: loadLabTests, treatments: loadTreatments,
```

- [ ] **Step 11: `admin/index.html`, booking chip.** Replace the whole function:
```js
function bookingSubjectCell(b) {
    if (b.booking_type === 'lab_test') {
        return '<span style="background:rgba(56,189,248,.14);color:#38bdf8;padding:2px 7px;'
            + 'border-radius:4px;font-size:11px;font-weight:600">'
            + (esc(b.lab_test_name) || 'Lab Test') + '</span>';
    }
    return esc(b.doctor_name) || '—';
}
```
with:
```js
function bookingSubjectCell(b) {
    if (b.booking_type === 'lab_test') {
        return '<span style="background:rgba(56,189,248,.14);color:#38bdf8;padding:2px 7px;'
            + 'border-radius:4px;font-size:11px;font-weight:600">'
            + (esc(b.lab_test_name) || 'Lab Test') + '</span>';
    }
    const doctor = esc(b.doctor_name) || '—';
    if (!b.treatment_name) return doctor;
    // Specialty booking: a consultation tagged with the treatment (migration 077).
    return doctor + '<br><span style="background:rgba(167,139,250,.14);color:#a78bfa;padding:2px 7px;'
        + 'border-radius:4px;font-size:11px;font-weight:600">' + esc(b.treatment_name) + '</span>';
}
```

- [ ] **Step 12: `admin/index.html`, staff permission checkboxes.** There are exactly two lines containing `value="LAB_TESTS_MANAGE"`: one with `class="staff-perm-cb"` (Create Staff) and one with `class="edit-staff-perm-cb"` (Edit Staff). Directly after **each** line, add the same line with these three substitutions:
- `data-perm-feature="lab_test_booking"` → `data-perm-feature="specialty_treatments"`
- `value="LAB_TESTS_MANAGE"` → `value="TREATMENTS_MANAGE"`
- `Manage Lab Tests` → `Manage Treatments`

Keep the class and every other character identical.

- [ ] **Step 13: `admin/index.html`, JS block.** Insert directly **before** the line `async function openLabCsvModal() {`:

```js
// ═══════ TREATMENTS (specialty plans, migration 077) ═══════
let treatmentsCache = [];
let treatmentDoctorsCache = [];

async function loadTreatments() {
    const el = document.getElementById('trtList');
    if (!el) return;
    el.innerHTML = loading();
    const canManage = hasPermission('TREATMENTS_MANAGE');
    document.querySelectorAll('.trt-manage').forEach(b => { b.style.display = canManage ? '' : 'none'; });
    try {
        const [rows, docs] = await Promise.all([api('/admin/treatments'), api('/admin/doctors')]);
        treatmentsCache = Array.isArray(rows) ? rows : [];
        const docList = Array.isArray(docs) ? docs : ((docs && docs.doctors) || []);
        treatmentDoctorsCache = docList.filter(d => d.is_active !== false);
        renderTreatmentCategoryOptions();
        renderStarterCard();
        filterTreatments();
    } catch (e) {
        el.innerHTML = emptyState('warning', e.message || 'Failed to load treatments');
    }
}

function treatmentCategories() {
    return [...new Set(treatmentsCache.map(t => (t.category || '').trim()).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b));
}

function renderTreatmentCategoryOptions() {
    const cats = treatmentCategories();
    const sel = document.getElementById('trtCategoryFilter');
    if (sel) {
        const current = sel.value;
        sel.innerHTML = '<option value="">All categories</option>'
            + cats.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
        sel.value = cats.includes(current) ? current : '';
    }
    const list = document.getElementById('trtCategoryOptions');
    if (list) list.innerHTML = cats.map(c => `<option value="${esc(c)}"></option>`).join('');
}

function renderStarterCard() {
    const card = document.getElementById('trtStarterCard');
    const text = document.getElementById('trtStarterText');
    const btn = document.getElementById('btnLoadStarter');
    if (!card || !text || !btn) return;
    const canManage = hasPermission('TREATMENTS_MANAGE');
    const hiddenStarters = treatmentsCache.filter(t => t.source === 'starter' && !t.is_active).length;
    if (!treatmentsCache.length && canManage) {
        text.textContent = 'Your catalog is empty. Load a ready-made list of common treatments for your specialty. '
            + 'They are added hidden, so you can set prices and edit descriptions, then choose which ones patients see.';
        btn.style.display = '';
        card.style.display = '';
    } else if (hiddenStarters && canManage) {
        text.textContent = `${hiddenStarters} starter treatment${hiddenStarters !== 1 ? 's are' : ' is'} hidden from patients. `
            + 'Tick the ones you offer and click "Show to patients". Delete the ones you do not offer.';
        btn.style.display = 'none';
        card.style.display = '';
    } else {
        card.style.display = 'none';
    }
}

function filterTreatments() {
    const q = (document.getElementById('trtSearch')?.value || '').toLowerCase().trim();
    const cat = document.getElementById('trtCategoryFilter')?.value || '';
    const rows = treatmentsCache.filter(t =>
        (!cat || t.category === cat)
        && (!q || [t.name, t.short_name, t.category, t.concerns].some(v => (v || '').toLowerCase().includes(q))));
    const countEl = document.getElementById('trtCount');
    if (countEl) {
        const shown = treatmentsCache.filter(t => t.is_active).length;
        countEl.textContent = `${treatmentsCache.length} treatment${treatmentsCache.length !== 1 ? 's' : ''} · ${shown} shown to patients`;
    }
    renderTreatmentsTable(rows);
}

function treatmentDoctorLabel(t) {
    const ids = t.doctor_ids || [];
    const names = treatmentDoctorsCache.filter(d => ids.includes(d.id)).map(d => esc(d.name));
    return names.length ? names.join(', ') : '<span style="color:var(--text3)">Any doctor</span>';
}

function formatTreatmentPrice(paise) {
    const rupees = Math.round((paise || 0) / 100);
    return rupees > 0 ? '₹' + String(rupees).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : 'After consultation';
}

function renderTreatmentsTable(rows) {
    const el = document.getElementById('trtList');
    if (!el) return;
    if (!rows.length) {
        el.innerHTML = emptyState('clipboard', treatmentsCache.length ? 'No treatments match your search' : 'No treatments added yet');
        return;
    }
    const canManage = hasPermission('TREATMENTS_MANAGE');
    el.innerHTML = `<table>
        <thead><tr>
            ${canManage ? '<th><input type="checkbox" aria-label="Select all treatments" onchange="toggleAllTreatments(this.checked)"></th>' : ''}
            <th>Treatment</th><th>Category</th><th>Price from</th><th>Duration</th><th>Doctors</th><th>Status</th>
            ${canManage ? '<th>Actions</th>' : ''}
        </tr></thead>
        <tbody>${rows.map(t => `<tr>
            ${canManage ? `<td><input type="checkbox" class="trt-select" value="${esc(t.id)}" aria-label="Select ${esc(t.name)}"></td>` : ''}
            <td style="font-weight:600; color:var(--text-strong)">${esc(t.name)}${t.short_name ? `<div style="color:var(--text3); font-size:0.75rem; font-weight:400">List title: ${esc(t.short_name)}</div>` : ''}</td>
            <td>${esc(t.category)}</td>
            <td>${formatTreatmentPrice(t.price_from_paise)}</td>
            <td>${t.duration_minutes ? esc(String(t.duration_minutes)) + ' min' : '—'}</td>
            <td>${treatmentDoctorLabel(t)}</td>
            <td>${t.is_active ? badge('active') : badge('inactive')}</td>
            ${canManage ? `<td>
                <button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--surface); min-width:auto" aria-label="Edit ${esc(t.name)}" onclick="editTreatment('${esc(t.id)}')"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-pencil"/></svg></button>
                <button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--red-bg); color:var(--red); min-width:auto" aria-label="Delete ${esc(t.name)}" onclick="delTreatment('${esc(t.id)}')"><svg class="i" aria-hidden="true" focusable="false"><use href="#i-trash"/></svg></button>
            </td>` : ''}
        </tr>`).join('')}</tbody>
    </table>`;
}

function toggleAllTreatments(checked) {
    document.querySelectorAll('.trt-select').forEach(cb => { cb.checked = checked; });
}

function renderTreatmentDoctorChecks(selectedIds) {
    const box = document.getElementById('f-trtDoctors');
    if (!box) return;
    if (!treatmentDoctorsCache.length) {
        box.innerHTML = '<span style="color:var(--text3); font-size:0.85rem;">No active doctors yet. Add doctors first.</span>';
        return;
    }
    box.innerHTML = treatmentDoctorsCache.map(d => `<label style="display:flex; gap:6px; align-items:center; font-weight:400;">
        <input type="checkbox" class="trt-doc-cb" value="${esc(d.id)}" ${selectedIds.includes(d.id) ? 'checked' : ''}>
        ${esc(d.name)}${d.specialization ? ` <span style="color:var(--text3)">(${esc(d.specialization)})</span>` : ''}
    </label>`).join('');
}

function updateTrtShortCounter() {
    const shortEl = document.getElementById('f-trtShort');
    const nameEl = document.getElementById('f-trtName');
    const counter = document.getElementById('trtShortCounter');
    if (!shortEl || !nameEl || !counter) return;
    const title = shortEl.value.trim() || nameEl.value.trim();
    counter.textContent = `${title.length}/24`;
    counter.style.color = title.length > 24 ? 'var(--red)' : 'var(--text3)';
}

function updateTrtDescCounter() {
    const desc = document.getElementById('f-trtDesc');
    const counter = document.getElementById('trtDescCounter');
    if (desc && counter) counter.textContent = `${desc.value.length}/400`;
}

function resetTreatmentForm() {
    ['f-trtId', 'f-trtName', 'f-trtCategory', 'f-trtShort', 'f-trtDuration', 'f-trtConcerns',
     'f-trtDesc', 'f-trtDescHi', 'f-trtDescTe', 'f-trtPrep'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('f-trtPrice').value = '0';
    document.getElementById('f-trtOrder').value = '0';
    document.getElementById('f-trtActive').checked = true;
    document.getElementById('trtFormTitle').textContent = 'Add Treatment';
    renderTreatmentDoctorChecks([]);
    updateTrtShortCounter();
    updateTrtDescCounter();
}

function openTreatmentForm() {
    const card = document.getElementById('trtFormCard');
    if (card) {
        card.style.display = 'block';
        card.scrollIntoView({ behavior: 'smooth' });
    }
}

function openAddTreatment() {
    resetTreatmentForm();
    openTreatmentForm();
}

function cancelTreatmentForm() {
    resetTreatmentForm();
    const card = document.getElementById('trtFormCard');
    if (card) card.style.display = 'none';
}

window.editTreatment = function (id) {
    const t = treatmentsCache.find(x => x.id === id);
    if (!t) return;
    resetTreatmentForm();
    document.getElementById('f-trtId').value = t.id;
    document.getElementById('f-trtName').value = t.name || '';
    document.getElementById('f-trtCategory').value = t.category || '';
    document.getElementById('f-trtShort').value = t.short_name || '';
    document.getElementById('f-trtOrder').value = t.display_order || 0;
    document.getElementById('f-trtPrice').value = Math.round((t.price_from_paise || 0) / 100);
    document.getElementById('f-trtDuration').value = t.duration_minutes || '';
    document.getElementById('f-trtConcerns').value = t.concerns || '';
    document.getElementById('f-trtDesc').value = t.description || '';
    document.getElementById('f-trtDescHi').value = t.description_hi || '';
    document.getElementById('f-trtDescTe').value = t.description_te || '';
    document.getElementById('f-trtPrep').value = t.prep_instructions || '';
    document.getElementById('f-trtActive').checked = !!t.is_active;
    document.getElementById('trtFormTitle').textContent = 'Edit Treatment';
    renderTreatmentDoctorChecks(t.doctor_ids || []);
    updateTrtShortCounter();
    updateTrtDescCounter();
    openTreatmentForm();
};

window.delTreatment = async function (id) {
    const t = treatmentsCache.find(x => x.id === id);
    const name = t ? t.name : 'this treatment';
    const ok = await confirmDialog(`Delete "${name}"? Past bookings keep the treatment name.`, { title: 'Delete Treatment', danger: true, okText: 'Delete' });
    if (!ok) return;
    try {
        await apiDel('/admin/treatments/' + encodeURIComponent(id));
        toast('Treatment deleted');
        loadTreatments();
    } catch (e) {
        toast(e.message || 'Failed to delete treatment', true);
    }
};

function optionalText(id) {
    const v = document.getElementById(id).value.trim();
    return v || null;
}

async function submitTreatment() {
    const id = document.getElementById('f-trtId').value;
    const name = document.getElementById('f-trtName').value.trim();
    const category = document.getElementById('f-trtCategory').value.trim();
    const shortName = document.getElementById('f-trtShort').value.trim();
    if (!name || !category) { toast('Treatment name and category are required', true); return; }
    if (name.length > 24 && !shortName) {
        toast('The name is longer than 24 characters. Add a WhatsApp list title of up to 24 characters.', true);
        return;
    }
    const price = parseInt(document.getElementById('f-trtPrice').value || '0', 10);
    if (isNaN(price) || price < 0) { toast('Enter a valid price (0 or more)', true); return; }
    const durationRaw = document.getElementById('f-trtDuration').value;
    const duration = durationRaw ? parseInt(durationRaw, 10) : null;
    if (duration !== null && (isNaN(duration) || duration < 5 || duration > 1440)) {
        toast('Duration must be between 5 and 1440 minutes', true);
        return;
    }
    const order = parseInt(document.getElementById('f-trtOrder').value || '0', 10) || 0;

    const payload = {
        name,
        category,
        short_name: shortName || null,
        price_from_rupees: price,
        duration_minutes: duration,
        concerns: optionalText('f-trtConcerns'),
        description: optionalText('f-trtDesc'),
        description_hi: optionalText('f-trtDescHi'),
        description_te: optionalText('f-trtDescTe'),
        prep_instructions: optionalText('f-trtPrep'),
        is_active: document.getElementById('f-trtActive').checked,
        display_order: order,
    };
    const doctorIds = [...document.querySelectorAll('.trt-doc-cb:checked')].map(cb => cb.value);

    const btn = document.getElementById('btnSaveTreatment');
    if (btn) btn.disabled = true;
    try {
        const saved = id
            ? await apiPut('/admin/treatments/' + encodeURIComponent(id), payload)
            : await apiPost('/admin/treatments', payload);
        const treatmentId = id || (saved && saved.id);
        if (treatmentId) {
            await apiPut('/admin/treatments/' + encodeURIComponent(treatmentId) + '/doctors', { doctor_ids: doctorIds });
        }
        toast(id ? 'Treatment updated' : 'Treatment added');
        cancelTreatmentForm();
        loadTreatments();
    } catch (err) {
        toast(err.message || 'Failed to save treatment', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function generateTreatmentDescription() {
    const name = document.getElementById('f-trtName').value.trim();
    const category = document.getElementById('f-trtCategory').value.trim();
    if (name.length < 2) { toast('Enter the treatment name first', true); return; }
    const hasText = ['f-trtDesc', 'f-trtDescHi', 'f-trtDescTe'].some(i => document.getElementById(i).value.trim());
    if (hasText) {
        const ok = await confirmDialog('Replace the current descriptions with a new AI draft?', { title: 'Generate description', okText: 'Replace' });
        if (!ok) return;
    }
    const btn = document.getElementById('btnTrtAi');
    if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
    try {
        const r = await apiPost('/admin/treatments/ai-description', { name, category: category || null });
        document.getElementById('f-trtDesc').value = r.description || '';
        document.getElementById('f-trtDescHi').value = r.description_hi || '';
        document.getElementById('f-trtDescTe').value = r.description_te || '';
        updateTrtDescCounter();
        toast(r.source === 'ai'
            ? 'AI draft added. Please review it before saving.'
            : 'AI is unavailable right now. A standard description was added; please edit it.');
    } catch (err) {
        toast(err.message || 'Failed to generate a description', true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '✨ Generate with AI'; }
    }
}

async function loadStarterTreatments() {
    const btn = document.getElementById('btnLoadStarter');
    if (btn) btn.disabled = true;
    try {
        const r = await apiPost('/admin/treatments/starter', {});
        toast(`${r.added} starter treatment${r.added !== 1 ? 's' : ''} added, hidden from patients until you show them`);
        loadTreatments();
    } catch (err) {
        toast(err.message || 'Failed to load starter treatments', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function setSelectedTreatmentsActive(active) {
    const ids = [...document.querySelectorAll('.trt-select:checked')].map(cb => cb.value);
    if (!ids.length) { toast('Tick at least one treatment first', true); return; }
    try {
        const r = await apiPost('/admin/treatments/status', { treatment_ids: ids, is_active: active });
        toast(`${r.updated} treatment${r.updated !== 1 ? 's' : ''} ${active ? 'shown to' : 'hidden from'} patients`);
        loadTreatments();
    } catch (err) {
        toast(err.message || 'Failed to update treatments', true);
    }
}

```

- [ ] **Step 14: Syntax-check the panel's inline JavaScript**

```bash
python - <<'PY'
import re, subprocess, pathlib, tempfile
html = pathlib.Path("admin/index.html").read_text(encoding="utf-8")
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S)
src = "\n;\n".join(blocks)
f = pathlib.Path(tempfile.gettempdir()) / "index_inline.js"
f.write_text(src, encoding="utf-8")
print(subprocess.run(["node", "--check", str(f)], capture_output=True, text=True))
PY
```
Expected: `returncode=0`. If Node is not installed, skip this step and say so in the session doc.

- [ ] **Step 15: Run tests**

```bash
pytest tests/test_specialty_admin_ui.py tests/test_diagbooking_admin_visibility.py tests/test_admin_panel_appointments_filter.py tests/test_security.py tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q
```
Expected: all PASS. Run the orphan check.

- [ ] **Step 16: Manual browser check (local, no production data)**
   1. Open `admin/index.html` served locally.
   2. Log in with a test clinic on plan `derma`, pointed at a **non-production** database.
   3. Check at 1280px and at 400px width, in dark and light themes:
      - The Treatments tab is visible.
      - The form fields wrap on mobile.
      - Nothing overflows horizontally except the table (inside `.tbl-wrap`).
   4. Log in as a `polyclinic` clinic: the Treatments tab must be hidden.

   If no non-production database is available, skip the browser check and record that in the session doc. **Never** run the app locally against the production `.env`.

- [ ] **Step 17: Commit**

```bash
git add admin/index.html app/main.py tests/test_specialty_admin_ui.py
git commit -m "feat(admin-ui): Treatments page, staff permission, treatment chips, specialty panel URLs

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
