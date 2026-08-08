# Admin Panel UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `admin/index.html` look and feel professional and stunning — light/dark theming, SVG icons instead of emoji, real accessibility (focus states, aria-labels), a restrained motion layer, and wider responsive tiers — without changing any JS logic, IDs, or backend calls.

**Architecture:** Every change is additive or token-driven within the existing single-file HTML/CSS/JS structure. The file already routes almost all colors through CSS custom properties (`var(--token)`), so light-mode support is mostly a sibling `:root[data-theme="light"]` override block. Icons become a small `ICONS`/`icon()` JS registry consumed both by static nav markup and by the existing `emptyState()`/toast helpers. No build step, no new dependencies.

**Tech Stack:** Vanilla HTML/CSS/JS (no framework, no bundler) — matches the file as it exists today.

## Global Constraints

- No structural HTML changes: every `id`, `data-page`, and `onclick="..."` function reference stays byte-identical. This is a visual/CSS/icon/accessibility layer only.
- No new build tooling, no new JS/CSS dependencies (no icon library import — icons are hand-authored inline SVG).
- **Default appearance must not change for any existing user on first load.** The theme toggle defaults to the current dark palette; light mode is opt-in and persisted via `localStorage`, never auto-applied from `prefers-color-scheme`.
- Backend (`app/routers/*.py`, `app/services/*.py`) is not touched by this plan.
- Spec reference: `docs/superpowers/specs/2026-08-08-admin-panel-ui-polish-design.md`

---

### Task 1: Theme tokens — light mode + fix hardcoded whites

**Files:**
- Modify: `admin/index.html` (`:root` block ~lines 9-33; ~11 hardcoded `#fff` locations; login screen HTML ~line 646; add theme toggle)

**Interfaces:**
- Produces: `--text-strong` CSS token (replaces all hardcoded `#fff`); `:root[data-theme="light"]` override block; `toggleTheme()` JS function; `localStorage` key `mediassist_theme`. Tasks 2-5 build on top of these tokens without needing to know this task's internals beyond "use `var(--token)`, never hardcode a color."

- [ ] **Step 1: Add `--text-strong` to the dark `:root` block and extend it**

Replace (lines 9-33):

```css
        :root {
            --bg: #060912;
            --surface: #0d1120;
            --surface2: #131830;
            --border: #1e2540;
            --border2: #2a3155;
            --text: #e2e8f0;
            --text2: #8892b0;
            --text3: #5a6480;
            --accent: #6c63ff;
            --accent2: #8b7dff;
            --accent-glow: rgba(108,99,255,0.15);
            --green: #00d68f;
            --green-bg: rgba(0,214,143,0.1);
            --red: #ff6b6b;
            --red-bg: rgba(255,107,107,0.1);
            --blue: #4da6ff;
            --blue-bg: rgba(77,166,255,0.1);
            --amber: #ffc857;
            --amber-bg: rgba(255,200,87,0.1);
            --pink: #f472b6;
            --cyan: #22d3ee;
            --radius: 14px;
            --shadow: 0 4px 30px rgba(0,0,0,0.3);
        }
```

with:

```css
        :root {
            --bg: #060912;
            --surface: #0d1120;
            --surface2: #131830;
            --border: #1e2540;
            --border2: #2a3155;
            --text: #e2e8f0;
            --text2: #8892b0;
            --text3: #5a6480;
            --text-strong: #ffffff;
            --accent: #6c63ff;
            --accent2: #8b7dff;
            --accent-glow: rgba(108,99,255,0.15);
            --green: #00d68f;
            --green-bg: rgba(0,214,143,0.1);
            --red: #ff6b6b;
            --red-bg: rgba(255,107,107,0.1);
            --blue: #4da6ff;
            --blue-bg: rgba(77,166,255,0.1);
            --amber: #ffc857;
            --amber-bg: rgba(255,200,87,0.1);
            --pink: #f472b6;
            --cyan: #22d3ee;
            --radius: 14px;
            --shadow: 0 4px 30px rgba(0,0,0,0.3);
            --motion-fast: 150ms;
            --motion-base: 250ms;
            --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
        }

        :root[data-theme="light"] {
            --bg: #f7f8fc;
            --surface: #ffffff;
            --surface2: #f1f3f9;
            --border: #e2e5f0;
            --border2: #d0d4e6;
            --text: #1a2035;
            --text2: #5a6480;
            --text3: #78829c;
            --text-strong: #0f1425;
            --accent: #6c63ff;
            --accent2: #5b4fe0;
            --accent-glow: rgba(108,99,255,0.08);
            --green: #059669;
            --green-bg: rgba(5,150,105,0.08);
            --red: #dc2626;
            --red-bg: rgba(220,38,38,0.08);
            --blue: #2563eb;
            --blue-bg: rgba(37,99,235,0.08);
            --amber: #b45309;
            --amber-bg: rgba(180,83,9,0.08);
            --pink: #db2777;
            --cyan: #0891b2;
            --shadow: 0 4px 30px rgba(15,20,37,0.08);
        }
```

- [ ] **Step 2: Fix hardcoded `#fff` in `<style>` rules**

`.btn-accent`'s `color: #fff` (line 133) sits on a solid violet gradient in both themes — leave that one literal, no change needed. The other five need fixing (each has distinct surrounding context, so each is its own edit):

Replace (line 238):
```css
        .page-head h2 { font-size: 1.5rem; font-weight: 700; color: #fff; margin-bottom: 4px; }
```
with:
```css
        .page-head h2 { font-size: 1.5rem; font-weight: 700; color: var(--text-strong); margin-bottom: 4px; }
```

Replace (line 283):
```css
        .stat .num { font-size: 2.2rem; font-weight: 800; color: #fff; line-height: 1; }
```
with:
```css
        .stat .num { font-size: 2.2rem; font-weight: 800; color: var(--text-strong); line-height: 1; }
```

Replace (line 311):
```css
        .card-head h3 { font-size: 1rem; font-weight: 600; color: #fff; }
```
with:
```css
        .card-head h3 { font-size: 1rem; font-weight: 600; color: var(--text-strong); }
```

Replace (line 95):
```css
        .login-logo h2 { font-size: 1.35rem; font-weight: 700; color: #fff; }
```
with:
```css
        .login-logo h2 { font-size: 1.35rem; font-weight: 700; color: var(--text-strong); }
```

Replace (lines 401-408, `.modal h3`):
```css
        .modal h3 {
            font-size: 1.1rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: 22px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border);
        }
```
with:
```css
        .modal h3 {
            font-size: 1.1rem;
            font-weight: 700;
            color: var(--text-strong);
            margin-bottom: 22px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border);
        }
```

Replace (lines 571-578, `.form-card h3`):
```css
        .form-card h3 {
            font-size: 1rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 22px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border);
        }
```
with:
```css
        .form-card h3 {
            font-size: 1rem;
            font-weight: 600;
            color: var(--text-strong);
            margin-bottom: 22px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border);
        }
```

- [ ] **Step 3: Fix hardcoded `color:#fff` inline in JS template strings**

Four render functions hardcode inline white on table cells. Do two `replace_all` edits:

Edit 1 — replace all occurrences of `font-weight:600;color:#fff` with `font-weight:600;color:var(--text-strong)` (`replace_all: true`). This covers lines 1757, 1799, 1930.

Edit 2 — replace the one occurrence of `font-weight:600; color:#fff` (line 1537, note the space after the semicolon) with `font-weight:600; color:var(--text-strong)`.

- [ ] **Step 4: Add the theme toggle button to the sidebar**

Replace:

```html
        <div class="nav-bottom">
            <div class="nav-link" onclick="logout()" style="color:var(--red)">
                <span class="ico">🚪</span> Sign Out
            </div>
        </div>
```

with:

```html
        <div class="nav-bottom">
            <div class="nav-link" onclick="toggleTheme()" id="themeToggleBtn" role="button" aria-label="Switch to light theme">
                <span class="ico">🌙</span> <span id="themeToggleLabel">Dark Mode</span>
            </div>
            <div class="nav-link" onclick="logout()" style="color:var(--red)" role="button" aria-label="Sign out">
                <span class="ico">🚪</span> Sign Out
            </div>
        </div>
```

(The `🌙`/`🚪` emoji here are placeholders swapped for real SVG icons in Task 2 — this step only wires the toggle button and its click handler.)

- [ ] **Step 5: Add the `toggleTheme()` JS function and load-time theme restore**

Insert immediately after the `let doctorCache = [];` line (in the `// ═══════ CONFIG ═══════` block, right before `// ═══════ AUTH ═══════`):

```javascript
// ═══════ THEME ═══════
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = document.getElementById('themeToggleLabel');
    if (label) label.textContent = theme === 'light' ? 'Light Mode' : 'Dark Mode';
    const btn = document.getElementById('themeToggleBtn');
    if (btn) btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
}

function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('mediassist_theme', next);
    applyTheme(next);
}

// Restore saved theme on load. Defaults to dark (no data-theme attribute) if
// nothing is stored — never auto-applies prefers-color-scheme, so existing
// users see zero visual change unless they explicitly opt into light mode.
(function initTheme() {
    const saved = localStorage.getItem('mediassist_theme');
    if (saved === 'light') applyTheme('light');
})();
```

- [ ] **Step 6: Manual verification**

Open `admin/index.html` in a browser (served by the running app). Confirm:
1. Page loads in the exact same dark appearance as before this task (no visual regression).
2. Clicking "Dark Mode" in the sidebar switches to a light theme — all text remains readable, no white-on-white or dark-on-dark spots (check Dashboard, a table page, a modal, and the login screen by logging out).
3. Reload the page — the light choice persists (localStorage).
4. Click again to switch back to dark — confirm it matches the original exactly.

- [ ] **Step 7: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add light/dark theme toggle and fix hardcoded white text colors"
```

---

### Task 2: Replace all emoji with a consistent inline-SVG icon system

**Files:**
- Modify: `admin/index.html` (add `ICONS`/`icon()` JS; 12 nav-link spots; login/sidebar brand mark ×2; hamburger button; toast icons; 20 `emptyState()` call sites)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ICONS` object and `icon(name, size)` JS helper. No other task depends on this, but it must land after Task 1 (some icons are used inside the theme toggle button added in Task 1 Step 4).

- [ ] **Step 1: Add the `ICONS` registry and `icon()` helper**

Insert immediately after the `initTheme()` IIFE from Task 1 Step 5:

```javascript
// ═══════ ICONS (inline SVG, stroke-based, theme-able via currentColor) ═══════
const ICONS = {
    dashboard: '<rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line>',
    doctor: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle>',
    clipboard: '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1"></rect>',
    sun: '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>',
    moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path>',
    flask: '<path d="M9 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L15 8V2"></path><line x1="9" y1="2" x2="15" y2="2"></line><line x1="7" y1="14" x2="17" y2="14"></line>',
    pill: '<rect x="3" y="9.5" width="18" height="5" rx="2.5" transform="rotate(-45 12 12)"></rect><line x1="8.5" y1="15.5" x2="15.5" y2="8.5"></line>',
    card: '<rect x="1" y="4" width="22" height="16" rx="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line>',
    sliders: '<line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line>',
    building: '<rect x="4" y="2" width="16" height="20" rx="1"></rect><rect x="9" y="6" width="2" height="2"></rect><rect x="13" y="6" width="2" height="2"></rect><rect x="9" y="11" width="2" height="2"></rect><rect x="13" y="11" width="2" height="2"></rect><rect x="10" y="16" width="4" height="6"></rect>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line>',
    folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>',
    warning: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>',
    success: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>',
    brand: '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line>',
    menu: '<line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line>',
};

function icon(name, size = 20) {
    const path = ICONS[name];
    if (!path) return '';
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}
```

- [ ] **Step 2: Add an `.ico svg` sizing rule**

Insert right after the existing `.nav-link .ico { font-size: 1.15rem; width: 26px; text-align: center; }` rule (line 219):

```css
        .nav-link .ico svg { vertical-align: middle; }
```

- [ ] **Step 3: Replace the 12 nav-link emoji icons**

Replace (lines 663-694 — the full `nav-links` block):

```html
        <div class="nav-links">
            <div class="nav-link on" data-page="dashboard" onclick="go('dashboard',this)">
                <span class="ico">📊</span> Dashboard
            </div>
            <div class="nav-link" data-page="appointments" data-feature="booking" onclick="go('appointments',this)">
                <span class="ico">📅</span> Appointments
            </div>
            <div class="nav-link" data-page="doctors" data-feature="booking" onclick="go('doctors',this)">
                <span class="ico">👨‍⚕️</span> Doctors
            </div>
            <div class="nav-link" data-page="leaves" data-feature="roster_management" onclick="go('leaves',this)">
                <span class="ico">📋</span> Leaves
            </div>
            <div class="nav-link" data-page="holidays" data-feature="roster_management" onclick="go('holidays',this)">
                <span class="ico">🗓️</span> Holidays
            </div>
            <div class="nav-link" data-page="patients" onclick="go('patients',this)">
                <span class="ico">👥</span> Patients
            </div>
            <div class="nav-link" data-page="labreports" data-feature="lab_reports" onclick="go('labreports',this)">
                <span class="ico">🧪</span> Lab Reports
            </div>
            <div class="nav-link" data-page="prescriptions" onclick="go('prescriptions',this)">
                <span class="ico">💊</span> Prescriptions
            </div>
            <div class="nav-link" data-page="payments" data-feature="booking" onclick="go('payments',this)">
                <span class="ico">💳</span> Payments
            </div>
            <div class="nav-link" data-page="paysettings" data-feature="payments_razorpay" onclick="go('paysettings',this)">
                <span class="ico">⚙️</span> Payment Settings
            </div>
            <div class="nav-link" data-page="branches" data-feature="multi_branch" onclick="go('branches',this)">
                <span class="ico">🏢</span> Branches
            </div>
        </div>
```

with (icons authored inline directly, since this is static HTML — using the same SVG markup the `icon()` helper generates, kept in sync manually since these are static):

```html
        <div class="nav-links">
            <div class="nav-link on" data-page="dashboard" onclick="go('dashboard',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg></span> Dashboard
            </div>
            <div class="nav-link" data-page="appointments" data-feature="booking" onclick="go('appointments',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg></span> Appointments
            </div>
            <div class="nav-link" data-page="doctors" data-feature="booking" onclick="go('doctors',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg></span> Doctors
            </div>
            <div class="nav-link" data-page="leaves" data-feature="roster_management" onclick="go('leaves',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><rect x="8" y="2" width="8" height="4" rx="1"></rect></svg></span> Leaves
            </div>
            <div class="nav-link" data-page="holidays" data-feature="roster_management" onclick="go('holidays',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg></span> Holidays
            </div>
            <div class="nav-link" data-page="patients" onclick="go('patients',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg></span> Patients
            </div>
            <div class="nav-link" data-page="labreports" data-feature="lab_reports" onclick="go('labreports',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L15 8V2"></path><line x1="9" y1="2" x2="15" y2="2"></line><line x1="7" y1="14" x2="17" y2="14"></line></svg></span> Lab Reports
            </div>
            <div class="nav-link" data-page="prescriptions" onclick="go('prescriptions',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="9.5" width="18" height="5" rx="2.5" transform="rotate(-45 12 12)"></rect><line x1="8.5" y1="15.5" x2="15.5" y2="8.5"></line></svg></span> Prescriptions
            </div>
            <div class="nav-link" data-page="payments" data-feature="booking" onclick="go('payments',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1" y="4" width="22" height="16" rx="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg></span> Payments
            </div>
            <div class="nav-link" data-page="paysettings" data-feature="payments_razorpay" onclick="go('paysettings',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line></svg></span> Payment Settings
            </div>
            <div class="nav-link" data-page="branches" data-feature="multi_branch" onclick="go('branches',this)">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="2" width="16" height="20" rx="1"></rect><rect x="9" y="6" width="2" height="2"></rect><rect x="13" y="6" width="2" height="2"></rect><rect x="9" y="11" width="2" height="2"></rect><rect x="13" y="11" width="2" height="2"></rect><rect x="10" y="16" width="4" height="6"></rect></svg></span> Branches
            </div>
        </div>
```

- [ ] **Step 4: Replace the theme toggle and logout nav-link icons (added in Task 1 Step 4)**

Replace:

```html
        <div class="nav-bottom">
            <div class="nav-link" onclick="toggleTheme()" id="themeToggleBtn" role="button" aria-label="Switch to light theme">
                <span class="ico">🌙</span> <span id="themeToggleLabel">Dark Mode</span>
            </div>
            <div class="nav-link" onclick="logout()" style="color:var(--red)" role="button" aria-label="Sign out">
                <span class="ico">🚪</span> Sign Out
            </div>
        </div>
```

with:

```html
        <div class="nav-bottom">
            <div class="nav-link" onclick="toggleTheme()" id="themeToggleBtn" role="button" aria-label="Switch to light theme">
                <span class="ico" id="themeToggleIcon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg></span> <span id="themeToggleLabel">Dark Mode</span>
            </div>
            <div class="nav-link" onclick="logout()" style="color:var(--red)" role="button" aria-label="Sign out">
                <span class="ico"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg></span> Sign Out
            </div>
        </div>
```

- [ ] **Step 5: Swap the theme icon (sun/moon) when toggled**

Replace the `applyTheme` function from Task 1 Step 5:

```javascript
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = document.getElementById('themeToggleLabel');
    if (label) label.textContent = theme === 'light' ? 'Light Mode' : 'Dark Mode';
    const btn = document.getElementById('themeToggleBtn');
    if (btn) btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
}
```

with:

```javascript
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = document.getElementById('themeToggleLabel');
    if (label) label.textContent = theme === 'light' ? 'Light Mode' : 'Dark Mode';
    const btn = document.getElementById('themeToggleBtn');
    if (btn) btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
    const iconEl = document.getElementById('themeToggleIcon');
    if (iconEl) iconEl.innerHTML = icon(theme === 'light' ? 'sun' : 'moon');
}
```

- [ ] **Step 6: Replace the brand mark (🏥) on the login screen and sidebar header**

Replace (login screen):

```html
        <div class="login-logo">
            <div class="icon">🏥</div>
```

with:

```html
        <div class="login-logo">
            <div class="icon"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line></svg></div>
```

Replace (sidebar header):

```html
            <h1>🏥 MediAssist <span class="dot"></span></h1>
```

with:

```html
            <h1><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-4px; margin-right:4px"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line></svg>MediAssist <span class="dot"></span></h1>
```

- [ ] **Step 7: Replace the hamburger glyph**

Replace:

```html
<button class="hamburger" onclick="toggleSidebar()">☰</button>
```

with:

```html
<button class="hamburger" onclick="toggleSidebar()" aria-label="Toggle navigation menu"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg></button>
```

- [ ] **Step 8: Replace toast icons**

Find the `toast()` function's icon assignment:

```javascript
    icon.textContent = err ? '⚠️' : '✅';
```

Replace with:

```javascript
    icon.innerHTML = icon_(err ? 'warning' : 'success');
```

Insert this one-line alias right after the `ICONS`/`icon()` definitions from Step 1 (needed because `toast()` already declares a local `const icon = document.createElement('span')`, which would otherwise shadow the global `icon()` helper):

```javascript
const icon_ = icon; // alias so the toast() function's local `icon` element var doesn't shadow the helper
```

- [ ] **Step 9: Replace `emptyState()` emoji arguments with icon keys**

First, update the `emptyState()` helper itself. Replace:

```javascript
function emptyState(icon, text) {
    return `<div class="empty"><div class="empty-icon">${icon}</div><p>${text}</p></div>`;
}
```

with:

```javascript
function emptyState(iconKey, text) {
    return `<div class="empty"><div class="empty-icon">${icon(iconKey, 32)}</div><p>${text}</p></div>`;
}
```

Then run 10 `replace_all` edits, one per unique emoji, scoped to the `emptyState('<emoji>'` call prefix so only these call sites are touched (not any unrelated occurrence of the same emoji elsewhere in the file):

| Find (`replace_all`) | Replace with |
|---|---|
| `emptyState('📁'` | `emptyState('folder'` |
| `emptyState('📅'` | `emptyState('calendar'` |
| `emptyState('⚠️'` | `emptyState('warning'` |
| `emptyState('👨‍⚕️'` | `emptyState('doctor'` |
| `emptyState('📋'` | `emptyState('clipboard'` |
| `emptyState('🗓️'` | `emptyState('sun'` |
| `emptyState('👥'` | `emptyState('users'` |
| `emptyState('🧪'` | `emptyState('flask'` |
| `emptyState('💊'` | `emptyState('pill'` |
| `emptyState('🏢'` | `emptyState('building'` |

This covers all 20 call sites (lines 1438, 1460, 1490, 1518, 1533, 1549, 1627, 1638, 1679, 1688, 1748, 1753, 1790, 1795, 1921, 1926, 2137, 2217, 2246, 2360) since each is one of these 10 emoji.

- [ ] **Step 10: Add `.empty-icon` sizing/color CSS**

Replace:

```css
        .empty { text-align: center; padding: 40px; color: var(--text3); }
        .empty .empty-icon { font-size: 2.4rem; margin-bottom: 10px; }
        .empty p { font-size: 0.88rem; }
```

with:

```css
        .empty { text-align: center; padding: 40px; color: var(--text3); }
        .empty .empty-icon { display: flex; justify-content: center; margin-bottom: 10px; opacity: 0.5; }
        .empty p { font-size: 0.88rem; }
```

- [ ] **Step 11: Manual verification**

In the browser: check every nav item renders a crisp SVG icon (not a broken image or missing glyph) in both themes; open a few empty-state pages (e.g. Leaves/Holidays with no data, or filter Payments to a status with no rows) and confirm the folder/warning/etc. icons render instead of emoji; trigger a toast (save any form) and confirm the success/warning icon renders; check the login screen and sidebar header brand mark.

- [ ] **Step 12: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): replace all emoji icons with a consistent inline-SVG icon system"
```

---

### Task 3: Accessibility — focus states, aria-labels, touch targets

**Files:**
- Modify: `admin/index.html` (add global `:focus-visible` rule; keyboard reachability for nav-links; touch-target fix)

**Interfaces:**
- Consumes: existing `.btn`, `.nav-link`, `.toggle-btn` classes.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Add a global focus-visible ring**

Insert right after the `* { margin: 0; padding: 0; box-sizing: border-box; }` rule (line 35):

```css
        :focus-visible {
            outline: 2px solid var(--accent);
            outline-offset: 2px;
            border-radius: 4px;
        }
        button:focus-visible, .nav-link:focus-visible, .btn:focus-visible {
            outline-offset: 3px;
        }
```

- [ ] **Step 2: Make `.nav-link` items keyboard-focusable**

`.nav-link` divs use `onclick` but have no `tabindex`, so they're unreachable by keyboard `Tab`. Run one `replace_all` edit: find `class="nav-link" data-page=` → replace with `class="nav-link" tabindex="0" data-page=` (covers all 10 `data-page` nav-links from Task 2 Step 3).

Then two individual edits for the two `nav-bottom` links (Task 2 Step 4's output):
- `onclick="toggleTheme()" id="themeToggleBtn" role="button"` → `onclick="toggleTheme()" id="themeToggleBtn" role="button" tabindex="0"`
- `onclick="logout()" style="color:var(--red)" role="button"` → `onclick="logout()" style="color:var(--red)" role="button" tabindex="0"`

Also make them activatable with Enter/Space. Insert after the `go(page, el)` function definition:

```javascript
document.querySelectorAll('.nav-link[tabindex]').forEach(link => {
    link.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            link.click();
        }
    });
});
```

- [ ] **Step 3: Verify touch target sizing**

`.hamburger` is 42×42px (line 592) — bump to 44×44 for full compliance:

Replace:

```css
        .hamburger {
            display: none;
            position: fixed;
            top: 14px; left: 14px;
            z-index: 200;
            width: 42px; height: 42px;
```

with:

```css
        .hamburger {
            display: none;
            position: fixed;
            top: 14px; left: 14px;
            z-index: 200;
            width: 44px; height: 44px;
```

`.btn-sm` (`padding: 7px 16px; font-size: 0.78rem`, used for compact table-row actions) stays under 44px on purpose — bumping it would break table-row density on a mouse/trackpad-driven admin surface. `# ponytail: .btn-sm stays under 44px touch target for table-row density; acceptable since these are mouse/trackpad admin surfaces, revisit if tablet-only front-desk usage becomes primary`.

- [ ] **Step 4: Manual verification**

Tab through the entire sidebar using only the keyboard (no mouse) — confirm every nav item, the theme toggle, and sign-out are reachable and show a visible focus ring, and Enter/Space activates them. Run a Lighthouse accessibility audit in Chrome DevTools if available and confirm no new regressions vs. before this task.

- [ ] **Step 5: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add keyboard focus states, tabindex, and touch-target fixes to nav"
```

---

### Task 4: Motion polish

**Files:**
- Modify: `admin/index.html` (apply motion tokens to existing transitions; add stagger-in entrance for stat cards; respect `prefers-reduced-motion`)

**Interfaces:**
- Consumes: `--motion-fast`, `--motion-base`, `--ease-out` tokens from Task 1.
- Produces: `.stagger-in` CSS class — not required elsewhere in this plan, but available for future use.

- [ ] **Step 1: Wrap all new motion in a reduced-motion guard, add stagger keyframes**

Insert right before the `/* ═══════ RESPONSIVE ═══════ */` comment (line 611):

```css
        /* ═══════ MOTION ═══════ */
        @media (prefers-reduced-motion: no-preference) {
            @keyframes staggerFadeUp {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .stagger-in > * {
                animation: staggerFadeUp var(--motion-base) var(--ease-out) both;
            }
            .stagger-in > *:nth-child(1) { animation-delay: 0ms; }
            .stagger-in > *:nth-child(2) { animation-delay: 40ms; }
            .stagger-in > *:nth-child(3) { animation-delay: 80ms; }
            .stagger-in > *:nth-child(4) { animation-delay: 120ms; }
            .stagger-in > *:nth-child(5) { animation-delay: 160ms; }
            .stagger-in > *:nth-child(6) { animation-delay: 200ms; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.001ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.001ms !important;
            }
        }
```

- [ ] **Step 2: Apply the stagger class to the dashboard stats grid**

Replace:

```html
            <div class="stats">
                <div class="stat"><div class="glow"></div><div class="label">Total Appointments</div><div class="num" id="s-total">—</div></div>
```

with:

```html
            <div class="stats stagger-in">
                <div class="stat"><div class="glow"></div><div class="label">Total Appointments</div><div class="num" id="s-total">—</div></div>
```

This is the first `class="stats"` occurrence in the file (the Dashboard page) — other `.stats` blocks (e.g. `#paymentStats`) are left unchanged since they're secondary surfaces.

- [ ] **Step 3: Standardize transition durations onto the motion tokens and add press feedback**

Replace:
```css
        .stat {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 22px;
            position: relative;
            overflow: hidden;
            transition: transform 0.2s, border-color 0.2s;
        }
```
with:
```css
        .stat {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 22px;
            position: relative;
            overflow: hidden;
            transition: transform var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
        }
```

Replace:
```css
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.88rem;
            font-family: inherit;
            transition: all 0.2s;
            position: relative;
            overflow: hidden;
        }
```
with:
```css
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.88rem;
            font-family: inherit;
            transition: all var(--motion-fast) var(--ease-out);
            position: relative;
            overflow: hidden;
        }
        .btn:active { transform: scale(0.97); }
```

- [ ] **Step 4: Manual verification**

Reload the Dashboard — confirm stat cards fade/slide in with a subtle stagger on first paint. Enable "Reduce motion" in OS accessibility settings and reload — confirm the stagger and all transitions become instant. Click a few buttons and confirm the subtle press-scale feedback.

- [ ] **Step 5: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add motion tokens, stat-card stagger entrance, and reduced-motion support"
```

---

### Task 5: Responsive breakpoint tiers

**Files:**
- Modify: `admin/index.html` (`@media` block, lines 611-624)

**Interfaces:**
- Consumes: existing `.stats`, `.main` classes.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Add 1024px and wide-desktop tiers**

Replace:

```css
        /* ═══════ RESPONSIVE ═══════ */
        @media (max-width: 768px) {
            .hamburger { display: flex; }
            .sidebar { transform: translateX(-100%); }
            .sidebar.mobile-open { transform: translateX(0); }
            .overlay.show { display: block; }
            .main { margin-left: 0; padding: 68px 16px 24px; }
            .stats { grid-template-columns: repeat(2, 1fr); }
            .form-row { grid-template-columns: 1fr; }
        }

        @media (max-width: 480px) {
            .stats { grid-template-columns: 1fr; }
        }
```

with:

```css
        /* ═══════ RESPONSIVE ═══════ */
        @media (max-width: 1024px) {
            .main { padding: 28px 24px; }
            .stats { grid-template-columns: repeat(3, 1fr); }
        }

        @media (max-width: 768px) {
            .hamburger { display: flex; }
            .sidebar { transform: translateX(-100%); }
            .sidebar.mobile-open { transform: translateX(0); }
            .overlay.show { display: block; }
            .main { margin-left: 0; padding: 68px 16px 24px; }
            .stats { grid-template-columns: repeat(2, 1fr); }
            .form-row { grid-template-columns: 1fr; }
        }

        @media (max-width: 480px) {
            .stats { grid-template-columns: 1fr; }
        }

        @media (min-width: 1441px) {
            .main { max-width: 1400px; }
        }
```

- [ ] **Step 2: Manual verification**

Using browser DevTools responsive mode, check the layout at 375px (phone), 768px (tablet portrait), 1024px (tablet landscape), and 1920px (wide desktop): no horizontal scroll at any width, sidebar collapses correctly below 768px, stat cards reflow sensibly at each tier, content doesn't stretch edge-to-edge on an ultrawide monitor.

- [ ] **Step 3: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): add 1024px and wide-desktop responsive breakpoint tiers"
```

---

### Task 6: Full cross-check

**Files:** none (verification-only task)

- [ ] **Step 1: Full regression pass**

Open `admin/index.html` in a browser against the running app and, without skipping any:
1. Log in as `super_admin` — confirm dashboard loads identically to before this plan (dark theme default).
2. Toggle to light mode, click through every nav tab (Dashboard, Appointments, Doctors, Leaves, Holidays, Patients, Lab Reports, Prescriptions, Payments, Payment Settings, Branches) — confirm every page is readable, no invisible text, no broken icons.
3. Toggle back to dark, repeat the same click-through.
4. Resize through 375 / 768 / 1024 / 1920px in both themes.
5. Tab through the sidebar with keyboard only; confirm visible focus rings throughout.
6. Trigger at least one toast (e.g. edit and save a doctor) and one empty state (filter a list to zero results) in both themes.
7. Enable OS reduced-motion and confirm animations are suppressed.
8. Confirm every `data-feature` tab-visibility behavior from the prior clinic-self-service-settings work still functions correctly (log in as a `soloclinic` clinic_admin if a test account is available, confirm Lab Reports/Branches stay hidden) — this plan must not have broken that JS.

- [ ] **Step 2: Report results**

If any step fails, fix the specific CSS/HTML/JS introduced by this plan (not the pre-existing logic) and re-verify before considering the plan complete.

---

## Self-Review Notes

- **Spec coverage:** theming/light-dark (Task 1), icon replacement covering the *complete* emoji inventory — nav + brand + toast + empty states, not just nav (Task 2), accessibility (Task 3), motion (Task 4), responsive tiers (Task 5) — every section of the design spec has a task.
- **Type/name consistency checked:** `icon(name, size)` (Task 2 Step 1) is called identically by `applyTheme()` (Step 5), the toast `icon_` alias (Step 8), and `emptyState()` (Step 9) — same signature throughout. `--text-strong`, `--motion-fast`, `--motion-base`, `--ease-out` tokens defined in Task 1 are the only new tokens Tasks 2-5 reference.
- **No placeholders:** every step is real, runnable HTML/CSS/JS, not a description of what to do.
- **Non-breaking guarantee honored:** no `id`, `data-page`, `onclick` target, or JS function signature used elsewhere in the file (e.g. by the clinic self-service settings work) is renamed or removed anywhere in this plan.
