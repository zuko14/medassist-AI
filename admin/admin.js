// ═══════ CONFIG ═══════
const API = window.location.origin;
let auth = '';

// Build request headers. When `auth` is empty the browser is authenticating
// with the HttpOnly session cookie (set by POST /admin/login), which fetch
// sends automatically for same-origin requests. The Authorization header must
// then be ABSENT, not empty: leaving it in place would let the server fall
// back to HTTP Basic and re-authenticate a session that was just revoked.
function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (auth) h.Authorization = auth;
    return h;
}
// T0.1d: real clinic scope, sourced from the server via /admin/me.
// 'default' is the platform-wide sentinel, honoured only for super_admin.
let CLINIC_SCOPE = 'default';
let doctorCache = [];

const DEPARTMENT_OPTIONS = [
    'General Medicine', 'Cardiology', 'Cardiothoracic Surgery', 'Neurology', 'Neurosurgery',
    'Orthopedics', 'Gynecology & Obstetrics', 'Pediatrics', 'Dermatology', 'Ophthalmology',
    'ENT', 'Dental', 'Urology', 'Nephrology', 'Gastroenterology', 'Endocrinology',
    'Pulmonology', 'Oncology', 'Psychiatry', 'General Surgery', 'Plastic Surgery',
    'Radiology', 'Pathology', 'Anesthesiology', 'Emergency Medicine', 'Physiotherapy',
    'Dietetics & Nutrition', 'Ayurveda', 'Homeopathy', 'IVF & Fertility', 'Rheumatology',
    'Diabetology', 'Bariatric Surgery', 'Vascular Surgery', 'Andrology', 'Geriatrics',
    'Sports Medicine',
];

function populateDeptOptions() {
    const el = document.getElementById('deptOptions');
    if (el) {
        el.innerHTML = DEPARTMENT_OPTIONS.map(d => `<option value="${d}">`).join('');
    }
}

function updateSlotPreview() {
    const toMinutes = (t) => {
        if (!t || !t.includes(':')) return 0;
        const [h, m] = t.split(':').map(Number);
        return h * 60 + m;
    };
    const countSlots = (start, end, dur) => {
        if (!start || !end || !dur) return 0;
        const diff = toMinutes(end) - toMinutes(start);
        return diff > 0 ? Math.floor(diff / dur) : 0;
    };
    const durEl = document.getElementById('f-docDuration');
    const mornStartEl = document.getElementById('f-docMornStart');
    const mornEndEl = document.getElementById('f-docMornEnd');
    const eveStartEl = document.getElementById('f-docEveStart');
    const eveEndEl = document.getElementById('f-docEveEnd');
    const previewEl = document.getElementById('docSlotPreview');
    if (!durEl || !previewEl) return;

    const dur = parseInt(durEl.value) || 30;
    const mornEnabled = document.getElementById('f-docMornEnabled')?.checked;
    const eveEnabled = document.getElementById('f-docEveEnabled')?.checked;
    const morn = mornEnabled ? countSlots(mornStartEl?.value, mornEndEl?.value, dur) : 0;
    const eve = eveEnabled ? countSlots(eveStartEl?.value, eveEndEl?.value, dur) : 0;
    
    if (!mornEnabled && !eveEnabled) {
        previewEl.textContent = '⚠️ Please enable at least one shift (Morning or Evening)';
        previewEl.style.color = 'var(--red)';
    } else {
        const parts = [];
        if (mornEnabled) parts.push(`${morn} morning`);
        if (eveEnabled) parts.push(`${eve} evening`);
        previewEl.textContent = `→ generates ${parts.join(' + ')} slots`;
        previewEl.style.color = 'var(--text3)';
    }
}

function toggleShiftFields() {
    const mornEnabled = document.getElementById('f-docMornEnabled')?.checked;
    const eveEnabled = document.getElementById('f-docEveEnabled')?.checked;
    const mornRow = document.getElementById('mornShiftFields');
    const eveRow = document.getElementById('eveShiftFields');
    if (mornRow) mornRow.style.display = mornEnabled ? 'flex' : 'none';
    if (eveRow) eveRow.style.display = eveEnabled ? 'flex' : 'none';
    updateSlotPreview();
}

document.addEventListener('DOMContentLoaded', () => {
    populateDeptOptions();
    toggleShiftFields();
    ['f-docMornStart', 'f-docMornEnd', 'f-docEveStart', 'f-docEveEnd', 'f-docDuration', 'f-docMornEnabled', 'f-docEveEnabled'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', updateSlotPreview);
        if (el) el.addEventListener('input', updateSlotPreview);
    });
});

// ═══════ THEME ═══════
function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = document.getElementById('themeToggleLabel');
    if (label) label.textContent = theme === 'light' ? 'Light Mode' : 'Dark Mode';
    const btn = document.getElementById('themeToggleBtn');
    if (btn) btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme');
    const iconEl = document.getElementById('themeToggleIcon');
    if (iconEl) iconEl.innerHTML = icon(theme === 'light' ? 'sun' : 'moon');
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

const icon_ = icon; // alias so the toast() function's local `icon` element var doesn't shadow the helper

// ═══════ AUTH ═══════
let myPlan = null;
let myFeatures = null; // null = super_admin, sees everything
let myRole = null;
let myPermissions = [];
let myBranchId = null;
let myStaffRole = null;

function hasPermission(perm) {
    if (myRole === 'super_admin' || myRole === 'clinic_admin') return true;
    return Boolean(myPermissions && myPermissions.includes(perm));
}

function applyFeatureVisibility() {
    document.querySelectorAll('[data-feature]').forEach(el => {
        const feature = el.dataset.feature;
        const visible = myFeatures === null || myFeatures.includes(feature);
        el.style.display = visible ? '' : 'none';
    });
    document.querySelectorAll('[data-role="admin"]').forEach(el => {
        const page = el.dataset.page;
        if (myRole === 'staff') {
            if (page === 'doctors') {
                el.style.display = (hasPermission('DOCTORS_CREATE') || hasPermission('DOCTORS_UPDATE') || hasPermission('DOCTORS_DELETE')) ? '' : 'none';
            } else if (page === 'leaves') {
                el.style.display = (hasPermission('DOCTOR_LEAVES_CREATE') || hasPermission('DOCTOR_LEAVES_DELETE')) ? '' : 'none';
            } else if (page === 'holidays') {
                el.style.display = (hasPermission('HOLIDAYS_CREATE') || hasPermission('HOLIDAYS_DELETE')) ? '' : 'none';
            } else if (page === 'branches') {
                el.style.display = hasPermission('DOCTOR_BRANCH_ASSIGN') ? '' : 'none';
            } else if (page === 'staff') {
                el.style.display = (hasPermission('STAFF_VIEW') || hasPermission('STAFF_CREATE') || hasPermission('STAFF_UPDATE')) ? '' : 'none';
            } else {
                el.style.display = 'none';
            }
        } else {
            el.style.display = '';
        }
    });

    const docForm = document.getElementById('docFormCard');
    if (docForm) docForm.style.display = hasPermission('DOCTORS_CREATE') ? '' : 'none';
    const leaveForm = document.getElementById('leaveFormCard');
    if (leaveForm) leaveForm.style.display = hasPermission('DOCTOR_LEAVES_CREATE') ? '' : 'none';
    const holForm = document.getElementById('holFormCard');
    if (holForm) holForm.style.display = hasPermission('HOLIDAYS_CREATE') ? '' : 'none';
    const staffForm = document.getElementById('staffFormCard');
    if (staffForm) staffForm.style.display = hasPermission('STAFF_CREATE') ? '' : 'none';
}

async function login() {
    const u = document.getElementById('loginUser').value.trim();
    const p = document.getElementById('loginPass').value;
    if (!u || !p) return;

    // Exchange the password for a revocable, expiring HttpOnly session cookie.
    // If the server reports session:false, migration 067 has not been applied
    // there yet, so keep using HTTP Basic rather than locking the user out.
    try {
        const r = await fetch(API + '/admin/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: u, password: p })
        });
        if (!r.ok) {
            let detail = 'Invalid username or password';
            try { detail = (await r.json()).detail || detail; } catch (e) {}
            auth = '';
            document.getElementById('loginError').innerHTML =
                '<div class="alert alert-err">' + esc(detail) + '</div>';
            return;
        }
        const d = await r.json();
        auth = d.session ? '' : 'Basic ' + btoa(u + ':' + p);
    } catch (e) {
        document.getElementById('loginError').innerHTML =
            '<div class="alert alert-err">Could not reach the server. Check your connection.</div>';
        return;
    }

    api('/admin/stats?days=30').then(data => {
        document.getElementById('loginScreen').style.display = 'none';
        document.getElementById('app').classList.add('open');
        renderDashboard(data);
        loadDoctorsSilent();
        fetchNotificationCount();
        startNotificationPolling();
        api('/admin/me').then(me => {
            CLINIC_SCOPE = me.clinic_id || 'default';
            myPlan = me.plan;
            myFeatures = me.features; // null for super_admin
            myRole = me.role;
            myPermissions = me.permissions || [];
            myBranchId = me.branch_id || null;
            myStaffRole = me.staff_role || null;
            applyFeatureVisibility();
            if (myPlan === 'diagstream') {
                loadDiagnosticDashboard();
            }
        }).catch(() => {});
    }).catch(() => {
        auth = '';
        document.getElementById('loginError').innerHTML =
            '<div class="alert alert-err">Invalid username or password</div>';
    });
}

function logout() {
    stopNotificationPolling();
    closeNotifDrawer();
    // Revoke server-side first, while the cookie is still being sent. Clearing
    // local state alone would leave the session valid until it expired.
    fetch(API + '/admin/logout', { method: 'POST', headers: authHeaders() }).catch(() => {});
    auth = '';
    CLINIC_SCOPE = 'default';
    myPlan = null;
    myFeatures = null;
    myRole = null;
    myPermissions = [];
    myBranchId = null;
    myStaffRole = null;
    doctorCache = [];
    document.getElementById('app').classList.remove('open');
    document.getElementById('loginScreen').style.display = '';
    document.getElementById('loginUser').value = '';
    document.getElementById('loginPass').value = '';
}

function togglePwd(inputId, btn) {
    const input = document.getElementById(inputId);
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.textContent = showing ? '👁' : '🙈';
    btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
}

function openChangePassword() {
    document.getElementById('cpCurrent').value = '';
    document.getElementById('cpNew').value = '';
    document.getElementById('cpConfirm').value = '';
    document.getElementById('changePasswordError').innerHTML = '';
    document.getElementById('changePasswordModal').classList.add('open');
}

function closeChangePassword() {
    document.getElementById('changePasswordModal').classList.remove('open');
}

async function submitChangePassword() {
    const current_password = document.getElementById('cpCurrent').value;
    const new_password = document.getElementById('cpNew').value;
    const confirm = document.getElementById('cpConfirm').value;

    if (!current_password || !new_password) {
        msg('changePasswordError', 'Please fill in all fields.', true);
        return;
    }
    if (new_password.length < 8) {
        msg('changePasswordError', 'New password must be at least 8 characters.', true);
        return;
    }
    if (new_password !== confirm) {
        msg('changePasswordError', 'New passwords do not match.', true);
        return;
    }

    try {
        const r = await fetch(API + '/admin/change-password', {
            method: 'PUT',
            headers: authHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ current_password, new_password })
        });
        const data = await r.json();
        if (!r.ok) {
            msg('changePasswordError', esc(data.detail || 'Failed to change password.'), true);
            return;
        }
        closeChangePassword();
        toast('Password updated successfully.');
    } catch (e) {
        msg('changePasswordError', 'Network error — please try again.', true);
    }
}

document.getElementById('loginPass').onkeypress = e => { if (e.key === 'Enter') login(); };
document.getElementById('loginUser').onkeypress = e => { if (e.key === 'Enter') document.getElementById('loginPass').focus(); };

// ═══════ NAV ═══════
function go(page, el) {
    document.querySelectorAll('.sec').forEach(s => s.classList.remove('on'));
    document.getElementById('pg-' + page).classList.add('on');

    document.querySelectorAll('.nav-link').forEach(n => n.classList.remove('on'));
    if (el) el.classList.add('on');
    else document.querySelector(`.nav-link[data-page="${page}"]`)?.classList.add('on');

    // Close mobile
    document.getElementById('sidebar').classList.remove('mobile-open');
    document.getElementById('overlay').classList.remove('show');

    const loaders = { dashboard: loadDashboard, profile: loadProfile, appointments: loadAppointments, doctors: loadDoctors, leaves: loadLeaves, holidays: loadHolidays, patients: loadPatients, diagreports: loadDiagnosticQueuePage, labreports: loadLabReports, labtests: loadLabTests, prescriptions: loadPrescriptions, payments: loadPayments, paysettings: loadPaymentSettings, branches: loadBranches, connectors: loadConnectorsPage, staff: loadStaff };
    if (loaders[page]) loaders[page]();
}

function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('mobile-open');
    document.getElementById('overlay').classList.toggle('show');
}

document.querySelectorAll('.nav-link[tabindex]').forEach(link => {
    link.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            link.click();
        }
    });
});

// ═══════ API HELPERS ═══════
async function api(path) {
    const r = await fetch(API + path, { headers: authHeaders() });
    if (!r.ok) throw new Error(r.status);
    return r.json();
}

async function apiPost(path, body) {
    const r = await fetch(API + path, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body)
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.status);
    return data;
}

async function apiPut(path, body) {
    const r = await fetch(API + path, {
        method: 'PUT',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body)
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.status);
    return data;
}

async function apiDel(path) {
    const r = await fetch(API + path, { method: 'DELETE', headers: authHeaders() });
    if (!r.ok) throw new Error(r.status);
}

function badge(status) {
    return `<span class="badge badge-${status || 'confirmed'}">${(status || 'unknown').replace('_', ' ')}</span>`;
}

function msg(id, text, err) {
    const el = document.getElementById(id);
    el.innerHTML = `<div class="alert ${err ? 'alert-err' : 'alert-ok'}">${text}</div>`;
    setTimeout(() => { el.innerHTML = ''; }, 4000);
}

function emptyState(iconKey, text) {
    return `<div class="empty"><div class="empty-icon">${icon(iconKey, 32)}</div><p>${text}</p></div>`;
}

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function loading() {
    return '<div class="loader"><div class="spin"></div>Loading...</div>';
}

// ═══════ TOAST (replaces alert()) ═══════
function toast(text, err = false) {
    const container = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = 'toast ' + (err ? 'toast-err' : 'toast-ok');
    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.innerHTML = icon_(err ? 'warning' : 'success');
    const label = document.createElement('span');
    label.textContent = text;
    el.appendChild(icon);
    el.appendChild(label);
    container.appendChild(el);
    setTimeout(() => {
        el.classList.add('out');
        setTimeout(() => el.remove(), 200);
    }, 4000);
}

// ═══════ CONFIRM / PROMPT DIALOG (replaces confirm()/prompt()) ═══════
let _dialogResolve = null;

function _openDialog({ title = 'Are you sure?', message = '', okText = 'Confirm', cancelText = 'Cancel', danger = false, isPrompt = false, placeholder = '' } = {}) {
    return new Promise((resolve) => {
        _dialogResolve = resolve;
        document.getElementById('confirmTitle').textContent = title;
        document.getElementById('confirmMessage').textContent = message;

        const okBtn = document.getElementById('confirmOkBtn');
        okBtn.textContent = okText;
        okBtn.className = 'btn ' + (danger ? 'btn-red' : 'btn-accent');
        document.getElementById('confirmCancelBtn').textContent = cancelText;

        const field = document.getElementById('confirmPromptField');
        const input = document.getElementById('confirmPromptInput');
        field.style.display = isPrompt ? 'block' : 'none';
        input.value = '';
        input.placeholder = placeholder;

        document.getElementById('confirmOverlay').classList.add('open');
        if (isPrompt) setTimeout(() => input.focus(), 50);
    });
}

function _closeDialog(value) {
    document.getElementById('confirmOverlay').classList.remove('open');
    if (_dialogResolve) { _dialogResolve(value); _dialogResolve = null; }
}

document.getElementById('confirmCancelBtn').onclick = () => _closeDialog(null);
document.getElementById('confirmOkBtn').onclick = () => {
    const isPrompt = document.getElementById('confirmPromptField').style.display !== 'none';
    _closeDialog(isPrompt ? document.getElementById('confirmPromptInput').value.trim() : true);
};
document.getElementById('confirmPromptInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('confirmOkBtn').click();
});

document.querySelectorAll('input[name="payMode"]').forEach(radio => {
    radio.addEventListener('change', () => {
        document.getElementById('f-payPercentRow').style.display =
            document.getElementById('f-payModePartial').checked ? 'block' : 'none';
    });
});

async function confirmDialog(message, opts = {}) {
    return (await _openDialog({ message, ...opts, isPrompt: false })) === true;
}

async function promptDialog(message, opts = {}) {
    return await _openDialog({ message, ...opts, isPrompt: true });
}

// ═══════ DASHBOARD ═══════
async function loadDashboard() {
    if (myPlan === 'diagstream') {
        loadDiagnosticDashboard();
        return;
    }
    const clinicDash = document.getElementById('clinicDashboardContent');
    const diagDash = document.getElementById('diagDashboardContent');
    if (clinicDash) clinicDash.style.display = 'block';
    if (diagDash) diagDash.style.display = 'none';

    try {
        const [stats, appts] = await Promise.all([
            api('/admin/stats?days=30'),
            api('/admin/appointments/recent?limit=5').catch(() => [])
        ]);
        renderDashboard(stats);
        renderDashAppts(appts);
        loadMessagingUsage();  // fire-and-forget, don't block dashboard
    } catch (e) { console.error('Dashboard:', e); }
}

function renderDashboard(d) {
    document.getElementById('s-total').textContent = d.total_appointments ?? 0;
    document.getElementById('s-confirmed').textContent = d.confirmed ?? 0;
    document.getElementById('s-cancelled').textContent = d.cancelled ?? 0;
    document.getElementById('s-completed').textContent = d.completed ?? 0;
    document.getElementById('s-patients').textContent = d.total_patients ?? 0;
    document.getElementById('s-new').textContent = d.new_patients ?? 0;

    // Animate numbers
    document.querySelectorAll('.stat .num').forEach(el => {
        const val = parseInt(el.textContent) || 0;
        animateNum(el, val);
    });

    // Department bars
    const depts = d.by_department || [];
    const el = document.getElementById('deptBars');
    if (!depts.length) { el.innerHTML = emptyState('folder', 'No department data yet'); return; }
    const max = Math.max(...depts.map(x => x.count));
    el.innerHTML = depts.map(dept => `
        <div class="dept-bar">
            <div class="dept-label"><span>${esc(dept.department)}</span><span>${dept.count}</span></div>
            <div class="dept-track"><div class="dept-fill" style="width:0%" data-w="${(dept.count / max * 100).toFixed(0)}%"></div></div>
        </div>
    `).join('');

    // Animate bars
    requestAnimationFrame(() => {
        el.querySelectorAll('.dept-fill').forEach(bar => {
            bar.style.width = bar.dataset.w;
        });
    });

    // Dash recent appointments
    api('/admin/appointments/recent?limit=5').then(renderDashAppts).catch(() => {});
}

function renderDashAppts(data) {
    const el = document.getElementById('dashAppts');
    if (!data || !data.length) { el.innerHTML = emptyState('calendar', 'No recent appointments'); return; }
    el.innerHTML = `<table>
        <thead><tr><th>Ref</th><th>Patient</th><th>Doctor</th><th>Date</th><th>Status</th></tr></thead>
        <tbody>${data.map(a => `<tr>
            <td style="color:var(--accent2); font-weight:600">${esc(a.booking_ref) || '—'}</td>
            <td>${esc(a.patient_name || a.patient_phone) || '—'}</td>
            <td>${esc(a.doctor_name) || '—'}</td>
            <td>${a.appointment_date || '—'}</td>
            <td>${badge(a.status)}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

function animateNum(el, target) {
    if (target === 0) { el.textContent = '0'; return; }
    let current = 0;
    const step = Math.max(1, Math.ceil(target / 20));
    const timer = setInterval(() => {
        current += step;
        if (current >= target) { current = target; clearInterval(timer); }
        el.textContent = current;
    }, 30);
}

// ═══════ MESSAGING USAGE (customer-safe) ═══════
async function loadMessagingUsage() {
    try {
        const data = await api('/admin/messaging-usage');
        if (data && !data.message) {
            renderUsageCard(data);
        } else {
            // Super admin or no clinic — hide card
            const card = document.getElementById('usageCard');
            if (card) card.style.display = 'none';
        }
    } catch (e) {
        console.warn('Messaging usage load skipped:', e);
        const el = document.getElementById('usageContent');
        if (el) el.innerHTML = '<p style="color:var(--text3);font-size:0.85rem;padding:8px">Usage data unavailable</p>';
    }
}

function renderUsageCard(d) {
    // Plan badge
    const planEl = document.getElementById('usagePlan');
    if (planEl) planEl.textContent = d.plan_display_name || d.plan || '';

    const el = document.getElementById('usageContent');
    if (!el) return;

    const sent = d.messages_sent || 0;
    const quota = d.included_messages || 0;
    const remaining = d.messages_remaining || 0;
    const pct = d.usage_percent || 0;
    const isUnlimited = quota <= 0 || quota >= 999999;
    const isOver = pct > 100;
    const overCount = isOver ? sent - quota : 0;

    // Build progress bar
    const barPct = Math.min(pct, 100);
    const barClass = isOver ? 'usage-bar-fill over' : 'usage-bar-fill';

    let html = '';

    // Metrics row
    html += `<div class="usage-grid">`;
    html += `<div class="usage-metric"><div class="val">${sent.toLocaleString()}</div><div class="lbl">Messages Sent</div></div>`;
    if (!isUnlimited) {
        html += `<div class="usage-metric"><div class="val">${quota.toLocaleString()}</div><div class="lbl">Monthly Quota</div></div>`;
        html += `<div class="usage-metric"><div class="val">${remaining >= 0 ? remaining.toLocaleString() : '0'}</div><div class="lbl">Remaining</div></div>`;
        html += `<div class="usage-metric"><div class="val">${pct.toFixed(1)}%</div><div class="lbl">Usage</div></div>`;
    } else {
        html += `<div class="usage-metric"><div class="val">∞</div><div class="lbl">Unlimited Plan</div></div>`;
    }
    html += `</div>`;

    // Progress bar (skip for unlimited)
    if (!isUnlimited) {
        html += `<div class="usage-bar-wrap">`;
        html += `<div class="usage-bar-track"><div class="${barClass}" style="width:0%" data-w="${barPct}%"></div></div>`;
        html += `<div class="usage-numbers"><span>${d.period_start || ''} → ${d.period_end || ''}</span><span>${sent} / ${quota}</span></div>`;
        html += `</div>`;
    }

    // Overage badge
    if (isOver && !isUnlimited) {
        html += `<div class="overage-badge">⚠️ ${overCount} messages over quota</div><br>`;
        html += `<a class="upgrade-cta" href="#" onclick="alert('Contact support to upgrade your plan.');return false;">Upgrade Plan →</a>`;
    }

    // Daily sparkline chart
    const daily = d.daily_breakdown || [];
    if (daily.length) {
        const maxD = Math.max(...daily.map(x => x.count), 1);
        html += `<div class="daily-chart" title="Daily sends (${daily.length} days)">`;
        daily.forEach(day => {
            const h = Math.max(2, (day.count / maxD) * 40);
            html += `<div class="daily-bar" style="height:0px" data-h="${h}px" title="${day.date}: ${day.count}"></div>`;
        });
        html += `</div>`;
    }

    el.innerHTML = html;

    // Animate bar and sparkline
    requestAnimationFrame(() => {
        const fill = el.querySelector('.usage-bar-fill');
        if (fill) fill.style.width = fill.dataset.w;
        el.querySelectorAll('.daily-bar').forEach(bar => {
            bar.style.height = bar.dataset.h;
        });
    });
}

// ═══════ APPOINTMENTS ═══════
async function loadAppointments() {
    const el = document.getElementById('apptList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/appointments/upcoming?days=30');
        if (!data?.length) { el.innerHTML = emptyState('calendar', 'No upcoming appointments'); return; }
        el.innerHTML = `<table>
            <thead><tr>
                <th>Ref</th><th>Token</th><th>Patient</th><th>Doctor</th>
                <th>Department</th><th>Date</th><th>Time</th>
                <th>Status</th><th>Action</th>
            </tr></thead>
            <tbody>${data.map(a => `<tr>
                <td style="color:var(--accent2);font-weight:600;font-family:monospace">${esc(a.booking_ref) || '—'}</td>
                <td>${a.token_number ? `<span style="background:var(--card-bg, #f3f4f6);padding:3px 8px;border-radius:4px;font-weight:700;color:var(--accent,#0ea5e9);">#${a.token_number}</span>` : '<span style="color:var(--text-muted,#9ca3af);">—</span>'}</td>
                <td>${esc(a.patient_name || a.patient_phone) || '—'}</td>
                <td>${esc(a.doctor_name) || '—'}</td>
                <td>${esc(a.department) || '—'}</td>
                <td>${a.appointment_date || '—'}</td>
                <td>${a.appointment_time || '—'}</td>
                <td>${badge(a.status)}</td>
                <td>${a.status === 'confirmed' ?
                    `<div style="display:flex;gap:6px;align-items:center;">
                        ${!a.token_number ? `
                            <button
                                style="background:var(--accent,#0ea5e9);color:#fff;border:none;
                                       padding:5px 10px;border-radius:6px;cursor:pointer;
                                       font-size:12px;font-weight:600;"
                                onclick="checkInAppt('${a.id}')">
                                Check In
                            </button>
                        ` : `<span style="font-size:11px;font-weight:600;color:var(--text-muted,#6b7280);text-transform:capitalize;">${esc(a.queue_status) || 'Waiting'}</span>`}
                        <button 
                            style="background:#fee2e2;color:#dc2626;border:none;
                                   padding:5px 12px;border-radius:6px;cursor:pointer;
                                   font-size:12px;font-weight:600;"
                            onclick="cancelAppt('${a.id}')">
                            Cancel
                        </button>
                    </div>` 
                    : '—'}
                </td>

            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load appointments'); }
}

// ═══════ DOCTORS ═══════
async function loadDoctorsSilent() {
    try { doctorCache = await api('/admin/doctors') || []; updateLeaveDropdown(); } catch (e) {}
}

async function loadDoctors() {
    const el = document.getElementById('docList');
    el.innerHTML = loading();
    try {
        doctorCache = await api('/admin/doctors') || [];
        updateLeaveDropdown();
        populateDocBranchDropdown(); // refresh branch dropdown when doctors page loads
        document.getElementById('docCount').textContent = `${doctorCache.length} doctor${doctorCache.length !== 1 ? 's' : ''}`;
        if (!doctorCache.length) { el.innerHTML = emptyState('doctor', 'No doctors added yet'); return; }
        const hasBranches = doctorCache.some(d => d.branch_name);
        el.innerHTML = `<table>
            <thead><tr><th>Name</th><th>Specialization</th><th>Department</th>${hasBranches ? '<th>Branch</th>' : ''}<th>Fee</th><th>Days</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>${doctorCache.map(d => `<tr>
                <td style="font-weight:600; color:var(--text-strong)">${esc(d.name)}</td>
                <td>${esc(d.specialization) || '—'}</td>
                <td>${esc(d.department)}</td>
                ${hasBranches ? `<td><span style="font-size:0.8rem; color:var(--accent)">${esc(d.branch_name) || '—'}</span></td>` : ''}
                <td>₹${d.consultation_fee}</td>
                <td style="color:var(--text2); font-size:0.8rem">${d.available_days || 'Mon-Fri'}</td>
                <td>${d.is_active ? badge('active') : badge('inactive')}</td>
                <td>
                    ${hasPermission('DOCTORS_UPDATE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--surface); min-width:auto" onclick="editDoctor('${d.id}')">✏️</button>` : ''}
                    ${hasPermission('DOCTORS_DELETE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--red-bg); color:var(--red); min-width:auto" onclick="delDoctor('${d.id}')">🗑️</button>` : ''}
                </td>
            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load doctors'); }
}

// Populate branch dropdown in doctor form (only visible for multi-branch clinics)
let _docBranchCache = [];
async function populateDocBranchDropdown() {
    const sel = document.getElementById('f-docBranch');
    const row = document.getElementById('docBranchRow');
    try {
        const data = await api('/admin/branches');
        const branches = (data.branches || []).filter(b => b.is_active);
        _docBranchCache = branches;
        if (branches.length < 2) {
            // Single or no branches — hide the dropdown, backend auto-assigns
            row.style.display = 'none';
            sel.innerHTML = '<option value="">— Auto —</option>';
            return;
        }
        // Multi-branch: show dropdown
        row.style.display = '';
        sel.innerHTML = '<option value="">— Select Branch —</option>';
        branches.forEach(b => {
            sel.innerHTML += `<option value="${b.id}">${esc(b.name)}</option>`;
        });
    } catch (e) {
        // Non-fatal: branches feature may not be enabled
        row.style.display = 'none';
    }
}

function updateLeaveDropdown() {
    const sel = document.getElementById('f-leaveDoc');
    sel.innerHTML = '';
    const active = doctorCache.filter(d => d.is_active);
    if (!active.length) {
        const opt = document.createElement('option');
        opt.value = ''; opt.textContent = 'No doctors available';
        sel.appendChild(opt);
        return;
    }
    active.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.name; opt.textContent = d.name;
        sel.appendChild(opt);
    });
}

async function submitDoctor() {
    const id = document.getElementById('f-docId').value;
    const name = document.getElementById('f-docName').value.trim();
    const spec = document.getElementById('f-docSpec').value.trim();
    if (!name || !spec) { msg('docMsg', 'Please fill in name and specialization', true); return; }

    const mornEnabled = document.getElementById('f-docMornEnabled').checked;
    const eveEnabled = document.getElementById('f-docEveEnabled').checked;
    if (!mornEnabled && !eveEnabled) {
        msg('docMsg', 'Please enable at least one shift (Morning or Evening)', true);
        return;
    }

    try {
        const days = Array.from(document.querySelectorAll('.doc-day-cb:checked')).map(cb => cb.value).join(',');
        const payload = {
            name,
            specialization: spec,
            department: document.getElementById('f-docDept').value,
            consultation_fee: parseInt(document.getElementById('f-docFee').value) || 500,
            available_days: days || 'Mon,Tue,Wed,Thu,Fri',
            morning_start: mornEnabled ? (document.getElementById('f-docMornStart').value || null) : null,
            morning_end: mornEnabled ? (document.getElementById('f-docMornEnd').value || null) : null,
            evening_start: eveEnabled ? (document.getElementById('f-docEveStart').value || null) : null,
            evening_end: eveEnabled ? (document.getElementById('f-docEveEnd').value || null) : null,
            slot_duration_minutes: parseInt(document.getElementById('f-docDuration').value) || 30,
        };

        // Include branch_id if the dropdown is visible and has a value
        const branchSel = document.getElementById('f-docBranch');
        if (branchSel && branchSel.value) {
            payload.branch_id = branchSel.value;
        }

        if (id) {
            await apiPut(`/admin/doctors/${id}`, payload);
            msg('docMsg', '✅ Doctor updated successfully!');
        } else {
            await apiPost('/admin/doctors', payload);
            msg('docMsg', '✅ Doctor added successfully!');
        }

        resetDoctorForm();
        loadDoctors();
    } catch (e) { msg('docMsg', e.message || (id ? 'Failed to update doctor' : 'Failed to add doctor'), true); }
}

function resetDoctorForm() {
    document.getElementById('f-docId').value = '';
    document.getElementById('f-docName').value = '';
    document.getElementById('f-docSpec').value = '';
    document.getElementById('f-docDept').value = '';
    document.getElementById('f-docFee').value = '500';
    document.getElementById('f-docMornEnabled').checked = true;
    document.getElementById('f-docEveEnabled').checked = true;
    document.getElementById('f-docMornStart').value = '09:00';
    document.getElementById('f-docMornEnd').value = '12:00';
    document.getElementById('f-docEveStart').value = '17:00';
    document.getElementById('f-docEveEnd').value = '19:00';
    document.getElementById('f-docDuration').value = '30';
    document.getElementById('f-docBranch').value = '';
    document.querySelectorAll('.doc-day-cb').forEach(cb => {
        cb.checked = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].includes(cb.value);
    });
    toggleShiftFields();
    document.getElementById('docFormTitle').innerHTML = '➕ Add New Doctor';
    document.getElementById('btn-docSubmit').textContent = 'Add Doctor';
    document.getElementById('btn-docCancel').style.display = 'none';
}

window.editDoctor = function(id) {
    const d = doctorCache.find(x => x.id === id);
    if (!d) return;
    document.getElementById('f-docId').value = d.id;
    document.getElementById('f-docName').value = d.name;
    document.getElementById('f-docSpec').value = d.specialization || '';
    document.getElementById('f-docDept').value = d.department;
    document.getElementById('f-docFee').value = d.consultation_fee;

    const hasMorn = Boolean(d.morning_start && d.morning_start !== '00:00' && d.morning_start !== '00:00:00');
    const hasEve = Boolean(d.evening_start && d.evening_start !== '00:00' && d.evening_start !== '00:00:00');

    document.getElementById('f-docMornEnabled').checked = hasMorn;
    document.getElementById('f-docEveEnabled').checked = hasEve;
    document.getElementById('f-docMornStart').value = hasMorn ? d.morning_start.slice(0, 5) : '09:00';
    document.getElementById('f-docMornEnd').value = hasMorn ? d.morning_end.slice(0, 5) : '12:00';
    document.getElementById('f-docEveStart').value = hasEve ? d.evening_start.slice(0, 5) : '17:00';
    document.getElementById('f-docEveEnd').value = hasEve ? d.evening_end.slice(0, 5) : '19:00';
    document.getElementById('f-docDuration').value = d.slot_duration_minutes || 30;
    // Pre-select the doctor's current branch
    document.getElementById('f-docBranch').value = d.branch_id || '';
    const activeDays = (d.available_days || 'Mon,Tue,Wed,Thu,Fri').split(',');
    document.querySelectorAll('.doc-day-cb').forEach(cb => { cb.checked = activeDays.includes(cb.value); });
    toggleShiftFields();

    document.getElementById('docFormTitle').innerHTML = '✏️ Edit Doctor Details';
    document.getElementById('btn-docSubmit').textContent = 'Update Doctor';
    document.getElementById('btn-docCancel').style.display = 'inline-block';

    document.getElementById('f-docName').focus();
    // Scroll to form if needed
    window.scrollTo({ top: document.getElementById('pg-doctors').offsetTop, behavior: 'smooth' });
};

window.delDoctor = async function(id) {
    const ok = await confirmDialog('Are you sure you want to delete this doctor? This action cannot be undone.', { okText: 'Delete', danger: true });
    if (!ok) return;
    try {
        await apiDel(`/admin/doctors/${id}`);
        loadDoctors();
    } catch (e) { toast('Failed to delete doctor. Ensure they have no upcoming appointments first.', true); }
};

// ═══════ LEAVES ═══════
async function loadLeaves() {
    const el = document.getElementById('leaveList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/leaves');
        if (!data?.length) { el.innerHTML = emptyState('clipboard', 'No leaves scheduled'); return; }
        el.innerHTML = `<table>
            <thead><tr><th>Doctor</th><th>Date</th><th>Type</th><th>Reason</th><th></th></tr></thead>
            <tbody>${data.map(l => `<tr>
                <td style="font-weight:600">${esc(l.doctor_name)}</td>
                <td>${l.leave_date}</td>
                <td>${l.leave_type === 'full' ? 'Full Day' : l.leave_type === 'half_morning' ? '½ Morning' : '½ Evening'}</td>
                <td style="color:var(--text2)">${esc(l.reason) || '—'}</td>
                <td>${hasPermission('DOCTOR_LEAVES_DELETE') ? `<button class="btn btn-red btn-sm" onclick="delLeave('${l.id}')">Remove</button>` : ''}</td>
            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load leaves'); }
}

async function addLeave() {
    const doc = document.getElementById('f-leaveDoc').value;
    const date = document.getElementById('f-leaveDate').value;
    const endDate = document.getElementById('f-leaveEndDate').value;
    if (!doc || !date) { msg('leaveMsg', 'Please select a doctor and start date', true); return; }
    
    const payload = {
        doctor_name: doc,
        leave_date: date,
        leave_type: document.getElementById('f-leaveType').value,
        reason: document.getElementById('f-leaveReason').value || null
    };
    
    if (endDate) {
        payload.end_date = endDate;
    }
    
    try {
        await apiPost('/admin/leaves', payload);
        msg('leaveMsg', '✅ Leave added successfully!');
        document.getElementById('f-leaveReason').value = '';
        document.getElementById('f-leaveEndDate').value = '';
        loadLeaves();
    } catch (e) { msg('leaveMsg', e.message || 'Failed to add leave', true); }
}

async function delLeave(id) {
    const ok = await confirmDialog('Remove this leave entry?', { okText: 'Remove', danger: true });
    if (!ok) return;
    try { await apiDel(`/admin/leaves/${id}`); loadLeaves(); } catch (e) { toast('Failed to remove leave entry', true); }
}

// ═══════ HOLIDAYS ═══════
async function loadHolidays() {
    const el = document.getElementById('holList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/holidays');
        if (!data?.length) { el.innerHTML = emptyState('sun', 'No holidays added'); return; }
        el.innerHTML = `<table>
            <thead><tr><th>Date</th><th>Holiday</th><th></th></tr></thead>
            <tbody>${data.map(h => `<tr>
                <td style="font-weight:600">${h.holiday_date}</td>
                <td>${esc(h.name)}</td>
                <td>${hasPermission('HOLIDAYS_DELETE') ? `<button class="btn btn-red btn-sm" onclick="delHoliday('${h.holiday_date}')">Remove</button>` : ''}</td>
            </tr>`).join('')}</tbody>
        </table>`;
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load holidays'); }
}

async function addHoliday() {
    const date = document.getElementById('f-holDate').value;
    const name = document.getElementById('f-holName').value.trim();
    if (!date || !name) { msg('holMsg', 'Please enter date and holiday name', true); return; }
    try {
        const r = await fetch(`${API}/admin/holidays?holiday_date=${date}&name=${encodeURIComponent(name)}`, {
            method: 'POST', headers: authHeaders()
        });
        if (!r.ok) throw new Error();
        msg('holMsg', '✅ Holiday added successfully!');
        document.getElementById('f-holName').value = '';
        loadHolidays();
    } catch (e) { msg('holMsg', e.message || 'Failed to add holiday', true); }
}

async function delHoliday(date) {
    const ok = await confirmDialog('Remove this holiday?', { okText: 'Remove', danger: true });
    if (!ok) return;
    try { await apiDel(`/admin/holidays/${date}`); loadHolidays(); } catch (e) { toast('Failed to remove holiday', true); }
}

async function cancelAppt(id) {
    const ok = await confirmDialog('Cancel this appointment?', { okText: 'Cancel Appointment', danger: true });
    if (!ok) return;
    try {
        const r = await fetch(API + '/admin/appointments/' + id, {
            method: 'DELETE',
            headers: authHeaders()
        });
        const data = await r.json();
        if (data.success) {
            loadAppointments();
            loadDashboard();
        } else {
            toast('Failed: ' + (data.detail || data.message || 'Error'), true);
        }
    } catch(e) {
        toast('Error: ' + e.message, true);
    }
}

async function checkInAppt(id) {
    try {
        const r = await fetch(`${API}/admin/appointments/${id}/check-in?clinic_id=${CLINIC_SCOPE}`, {
            method: 'POST',
            headers: authHeaders()
        });
        const data = await r.json();
        if (r.ok && data.token_number) {
            toast('Checked in! Assigned Token #' + data.token_number);
            loadAppointments();
            loadDashboard();
        } else {
            toast('Check-in failed: ' + (data.detail || data.message || 'Error'), true);
        }
    } catch(e) {
        toast('Error: ' + e.message, true);
    }
}

// ═══════ PATIENTS ═══════
let patientCache = [];

async function loadPatients() {
    const el = document.getElementById('patientList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/patients');
        patientCache = data.patients || [];
        const total = patientCache.length;
        const optedIn = patientCache.filter(p => p.opted_in).length;
        const optedOut = total - optedIn;
        document.getElementById('s-pat-total').textContent = total;
        document.getElementById('s-pat-opted').textContent = optedIn;
        document.getElementById('s-pat-out').textContent = optedOut;
        renderPatientTable(patientCache);
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load patients'); }
}

function renderPatientTable(list) {
    const el = document.getElementById('patientList');
    if (!list.length) { el.innerHTML = emptyState('users', 'No patients found'); return; }
    el.innerHTML = `<table>
        <thead><tr><th>Name</th><th>Phone</th><th>Language</th><th>Opted In</th><th>Visits</th><th>Actions</th></tr></thead>
        <tbody>${list.map(p => `<tr>
            <td style="font-weight:600;color:var(--text-strong)">${esc(p.name) || '—'}</td>
            <td>${esc(p.phone)}</td>
            <td>${esc(p.language) || '—'}</td>
            <td>${p.opted_in ? badge('active') : badge('inactive')}</td>
            <td>${p.appointment_count ?? p.visit_count ?? 0}</td>
            <td><button class="btn btn-ghost btn-sm" data-phone="${esc(p.phone)}" data-name="${esc(p.name || '')}" onclick="openLabReportModalFor(this.dataset.phone, this.dataset.name)">📋 Upload Report</button></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

function filterPatients() {
    const q = document.getElementById('patientSearch').value.toLowerCase();
    const filtered = patientCache.filter(p =>
        (p.name || '').toLowerCase().includes(q) ||
        (p.phone || '').includes(q)
    );
    renderPatientTable(filtered);
}

// ═══════ LAB REPORTS ═══════
let labCache = [];

async function loadLabReports() {
    const el = document.getElementById('labList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/lab-reports');
        labCache = data.reports || [];
        const sent = labCache.filter(r => r.status === 'sent').length;
        const failed = labCache.filter(r => r.status === 'failed').length;
        document.getElementById('s-lab-sent').textContent = sent;
        document.getElementById('s-lab-failed').textContent = failed;
        renderLabTable(labCache);
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load lab reports'); }
}

function renderLabTable(list) {
    const el = document.getElementById('labList');
    if (!list.length) { el.innerHTML = emptyState('flask', 'No lab reports yet'); return; }
    el.innerHTML = `<table>
        <thead><tr><th>Patient Name</th><th>Phone</th><th>Report Name</th><th>Report Type</th><th>Uploaded At</th><th>AI Summary</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${list.map(r => `<tr>
            <td style="font-weight:600;color:var(--text-strong)">${esc(r.patient_name) || '—'}</td>
            <td>${esc(r.patient_phone)}</td>
            <td>${esc(r.report_name)}</td>
            <td>${esc(r.report_type) || 'General'}</td>
            <td>${r.uploaded_at ? new Date(r.uploaded_at).toLocaleString() : '—'}</td>
            <td>${r.ai_summary ? (r.has_abnormal_values ? `<span title="${r.ai_summary.replace(/"/g,'&quot;')}" style="cursor:help;font-size:1.2rem;color:var(--amber)">⚠️</span>` : `<span title="${r.ai_summary.replace(/"/g,'&quot;')}" style="cursor:help;font-size:1.2rem;color:var(--green)">✅</span>`) : '—'}</td>
            <td>${badge(r.status)}</td>
            <td><button class="btn btn-ghost btn-sm" onclick="resendReport('${r.id}')">🔁 Resend</button></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

function filterLabReports() {
    const q = document.getElementById('labSearch').value.toLowerCase();
    const filtered = labCache.filter(r =>
        (r.patient_name || '').toLowerCase().includes(q) ||
        (r.patient_phone || '').includes(q)
    );
    renderLabTable(filtered);
}

function openLabReportModal() {
    document.getElementById('m-labPhone').value = '';
    document.getElementById('m-labPhone').readOnly = false;
    document.getElementById('m-labName').value = '';
    document.getElementById('m-labReportName').value = '';
    document.getElementById('m-labReportType').value = 'General';
    document.getElementById('m-labFile').value = '';
    document.getElementById('labModalMsg').innerHTML = '';
    document.getElementById('btn-labSubmit').disabled = false;
    document.getElementById('btn-labSubmit').textContent = 'Send via WhatsApp';
    document.getElementById('labReportModal').classList.add('open');
}

function openLabReportModalFor(phone, name) {
    openLabReportModal();
    document.getElementById('m-labPhone').value = phone;
    document.getElementById('m-labPhone').readOnly = true;
    document.getElementById('m-labName').value = name;
}

function closeLabReportModal() {
    document.getElementById('labReportModal').classList.remove('open');
}

async function submitLabReport() {
    const phone = document.getElementById('m-labPhone').value.trim();
    const name = document.getElementById('m-labName').value.trim();
    const reportName = document.getElementById('m-labReportName').value.trim();
    const reportType = document.getElementById('m-labReportType').value;
    const fileInput = document.getElementById('m-labFile');

    if (!phone || !reportName || !fileInput.files.length) {
        msg('labModalMsg', 'Please fill all required fields and select a PDF file', true);
        return;
    }

    const btn = document.getElementById('btn-labSubmit');
    btn.disabled = true;
    btn.textContent = 'Sending...';

    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    fd.append('patient_phone', phone);
    fd.append('patient_name', name);
    fd.append('report_name', reportName);
    fd.append('report_type', reportType);

    try {
        const r = await fetch(API + '/admin/lab-reports/upload', {
            method: 'POST',
            headers: authHeaders(),
            body: fd
        });
        const data = await r.json();
        if (data.success) {
            msg('labModalMsg', '✅ Report sent successfully!');
            setTimeout(() => { closeLabReportModal(); loadLabReports(); }, 1200);
        } else {
            msg('labModalMsg', data.error || 'Upload failed', true);
            btn.disabled = false;
            btn.textContent = 'Send via WhatsApp';
        }
    } catch (e) {
        msg('labModalMsg', 'Upload failed: ' + e.message, true);
        btn.disabled = false;
        btn.textContent = 'Send via WhatsApp';
    }
}

async function resendReport(id) {
    const ok = await confirmDialog('Resend this report to the patient?', { okText: 'Resend' });
    if (!ok) return;
    try {
        const r = await fetch(API + '/admin/lab-reports/' + id + '/resend', {
            method: 'POST',
            headers: authHeaders()
        });
        const data = await r.json();
        if (data.success) {
            toast('Report resent successfully!');
            loadLabReports();
        } else {
            toast('Resend failed: ' + (data.detail || data.error || 'Error'), true);
        }
    } catch (e) { toast('Error: ' + e.message, true); }
}

// ═══════ LAB TESTS ═══════
let labTestsCache = [];

async function loadLabTests() {
    const el = document.getElementById('labTestList');
    if (!el) return;
    el.innerHTML = loading();
    try {
        labTestsCache = await api('/admin/lab-tests') || [];
        const countEl = document.getElementById('labTestCount');
        if (countEl) countEl.textContent = `${labTestsCache.length} test${labTestsCache.length !== 1 ? 's' : ''}`;
        renderLabTestsTable(labTestsCache);
    } catch (e) {
        el.innerHTML = emptyState('warning', 'Failed to load lab tests');
    }
}

function renderLabTestsTable(tests) {
    const el = document.getElementById('labTestList');
    if (!el) return;
    if (!tests.length) {
        el.innerHTML = emptyState('clipboard', 'No lab tests added yet');
        return;
    }
    el.innerHTML = `<table>
        <thead>
            <tr>
                <th>Test Name</th>
                <th>Sample Type</th>
                <th>Price</th>
                <th>Turnaround</th>
                <th>Fasting</th>
                <th>Status</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>${tests.map(t => `<tr>
            <td style="font-weight:600; color:var(--text-strong)">${esc(t.name)}</td>
            <td>${esc(t.sample_type) || '—'}</td>
            <td>₹${t.price_paise / 100}</td>
            <td>${t.turnaround_hours ? t.turnaround_hours + 'h' : '—'}</td>
            <td>${t.fasting_required ? '<span style="color:var(--amber)">⚠️ Yes</span>' : 'No'}</td>
            <td>${t.is_active ? badge('active') : badge('inactive')}</td>
            <td>
                ${hasPermission('LAB_TESTS_MANAGE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--surface); min-width:auto" onclick="editLabTest('${t.id}')">✏️</button>` : ''}
                ${hasPermission('LAB_TESTS_MANAGE') ? `<button class="btn" style="padding:4px 8px; font-size:0.8rem; background:var(--red-bg); color:var(--red); min-width:auto" onclick="delLabTest('${t.id}')">🗑️</button>` : ''}
            </td>
        </tr>`).join('')}</tbody>
    </table>`;
}

function filterLabTests() {
    const q = (document.getElementById('labTestSearch')?.value || '').toLowerCase().trim();
    if (!q) {
        renderLabTestsTable(labTestsCache);
        return;
    }
    const filtered = labTestsCache.filter(t => 
        (t.name || '').toLowerCase().includes(q) || 
        (t.sample_type || '').toLowerCase().includes(q)
    );
    renderLabTestsTable(filtered);
}

function openAddLabTestModal() {
    resetLabTestForm();
    const card = document.getElementById('labTestFormCard');
    if (card) {
        card.style.display = 'block';
        card.scrollIntoView({ behavior: 'smooth' });
    }
}

function cancelLabTestForm() {
    resetLabTestForm();
    const card = document.getElementById('labTestFormCard');
    if (card) card.style.display = 'none';
}

function resetLabTestForm() {
    document.getElementById('f-labTestId').value = '';
    document.getElementById('f-ltName').value = '';
    document.getElementById('f-ltSampleType').value = '';
    document.getElementById('f-ltPriceRupees').value = '';
    document.getElementById('f-ltTurnaround').value = '';
    document.getElementById('f-ltFasting').checked = false;
    document.getElementById('f-ltPrep').value = '';
    document.getElementById('labTestFormTitle').textContent = '➕ Add Lab Test';
    const msgEl = document.getElementById('labTestMsg');
    if (msgEl) msgEl.innerHTML = '';
}

window.editLabTest = function(id) {
    const test = labTestsCache.find(t => t.id === id);
    if (!test) return;
    document.getElementById('f-labTestId').value = test.id;
    document.getElementById('f-ltName').value = test.name || '';
    document.getElementById('f-ltSampleType').value = test.sample_type || '';
    document.getElementById('f-ltPriceRupees').value = test.price_paise ? test.price_paise / 100 : '';
    document.getElementById('f-ltTurnaround').value = test.turnaround_hours || '';
    document.getElementById('f-ltFasting').checked = !!test.fasting_required;
    document.getElementById('f-ltPrep').value = test.prep_instructions || '';
    document.getElementById('labTestFormTitle').textContent = '✏️ Edit Lab Test';
    const card = document.getElementById('labTestFormCard');
    if (card) {
        card.style.display = 'block';
        card.scrollIntoView({ behavior: 'smooth' });
    }
};

window.delLabTest = async function(id) {
    const test = labTestsCache.find(t => t.id === id);
    const name = test ? test.name : 'this test';
    const ok = await confirmDialog(`Are you sure you want to remove "${name}" from your catalog?`, { title: 'Delete Lab Test', danger: true, okText: 'Delete' });
    if (!ok) return;
    try {
        await apiDel('/admin/lab-tests/' + id);
        toast('Lab test deleted');
        loadLabTests();
    } catch (e) {
        toast(e.message || 'Failed to delete lab test', true);
    }
};

async function submitLabTest(e) {
    if (e && e.preventDefault) e.preventDefault();
    const id = document.getElementById('f-labTestId').value;
    const name = document.getElementById('f-ltName').value.trim();
    const sampleType = document.getElementById('f-ltSampleType').value.trim() || null;
    const priceRupees = parseInt(document.getElementById('f-ltPriceRupees').value, 10);
    const turnaround = parseInt(document.getElementById('f-ltTurnaround').value, 10) || null;
    const fasting = document.getElementById('f-ltFasting').checked;
    const prep = document.getElementById('f-ltPrep').value.trim() || null;

    if (!name) {
        toast('Please enter test name', true);
        return;
    }
    if (isNaN(priceRupees) || priceRupees <= 0) {
        toast('Please enter a valid price in rupees', true);
        return;
    }

    const payload = {
        name,
        sample_type: sampleType,
        price_rupees: priceRupees,
        turnaround_hours: turnaround,
        fasting_required: fasting,
        prep_instructions: prep,
    };

    const btn = document.getElementById('btnSaveLabTest');
    if (btn) btn.disabled = true;

    try {
        if (id) {
            await apiPut('/admin/lab-tests/' + id, payload);
            toast('Lab test updated');
        } else {
            await apiPost('/admin/lab-tests', payload);
            toast('Lab test created');
        }
        cancelLabTestForm();
        loadLabTests();
    } catch (err) {
        toast(err.message || 'Failed to save lab test', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

function openLabCsvModal() {
    const modal = document.getElementById('labCsvModal');
    if (modal) {
        modal.classList.add('open');
        document.getElementById('f-labCsvFile').value = '';
        const resEl = document.getElementById('labCsvResults');
        if (resEl) { resEl.style.display = 'none'; resEl.innerHTML = ''; }
    }
}

function closeLabCsvModal() {
    const modal = document.getElementById('labCsvModal');
    if (modal) modal.classList.remove('open');
}

async function submitLabTestCsv() {
    const fileInput = document.getElementById('f-labCsvFile');
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
        toast('Please select a CSV file', true);
        return;
    }
    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', file);

    const btn = document.getElementById('btnUploadLabCsv');
    if (btn) btn.disabled = true;
    const resEl = document.getElementById('labCsvResults');

    try {
        const resp = await fetch(API + '/admin/lab-tests/import-csv', {
            method: 'POST',
            headers: authHeaders(),
            body: formData,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Import failed');

        if (resEl) {
            resEl.style.display = 'block';
            let html = `<div style="color:var(--green); font-weight:600;">✅ Created: ${data.created}, Updated: ${data.updated}</div>`;
            if (data.errors && data.errors.length) {
                html += `<div style="color:var(--red); margin-top:6px;">⚠️ Errors (${data.errors.length}):<br>${data.errors.map(e => esc(e)).join('<br>')}</div>`;
            }
            resEl.innerHTML = html;
        }
        toast(`Import finished: ${data.created} created, ${data.updated} updated`);
        loadLabTests();
    } catch (e) {
        toast(e.message || 'Import failed', true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function submitCollectionWindow(e) {
    if (e && e.preventDefault) e.preventDefault();
    const start = document.getElementById('cw-start').value;
    const end = document.getElementById('cw-end').value;
    const days = document.getElementById('cw-days').value.trim();
    const sundayStart = document.getElementById('cw-sunday-start').value;
    const sundayEnd = document.getElementById('cw-sunday-end').value;

    if (!start || !end || !days) {
        toast('Please fill all collection window fields', true);
        return;
    }

    const payload = { start, end, days };
    if (sundayStart && sundayEnd) {
        payload.sunday_start = sundayStart;
        payload.sunday_end = sundayEnd;
    }

    try {
        await apiPut('/admin/lab-collection-window', payload);
        toast('Sample collection window saved');
    } catch (e) {
        toast(e.message || 'Failed to save collection window', true);
    }
}

// ═══════ PRESCRIPTIONS ═══════
let rxCache = [];
let rxActiveOnly = false;

async function loadPrescriptions() {
    const el = document.getElementById('rxList');
    el.innerHTML = loading();
    try {
        const data = await api('/admin/prescriptions?active_only=' + rxActiveOnly);
        rxCache = data.prescriptions || [];
        const active = rxCache.filter(r => r.is_active).length;
        document.getElementById('s-rx-active').textContent = active;
        document.getElementById('s-rx-total').textContent = rxCache.length;
        renderRxTable(rxCache);
    } catch (e) { el.innerHTML = emptyState('warning', 'Failed to load prescriptions'); }
}

function renderRxTable(list) {
    const el = document.getElementById('rxList');
    if (!list.length) { el.innerHTML = emptyState('pill', 'No prescriptions yet'); return; }
    el.innerHTML = `<table>
        <thead><tr><th>Patient Name</th><th>Phone</th><th>Medicine</th><th>Dosage</th><th>Frequency</th><th>Reminder Times</th><th>End Date</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${list.map(r => `<tr>
            <td style="font-weight:600;color:var(--text-strong)">${esc(r.patient_name) || '—'}</td>
            <td>${esc(r.patient_phone)}</td>
            <td>${esc(r.medicine_name)}</td>
            <td>${esc(r.dosage)}</td>
            <td>${esc(r.frequency)}</td>
            <td>${(r.reminder_times || []).join(', ')}</td>
            <td>${r.end_date}</td>
            <td>${r.is_active ? badge('active') : badge('inactive')}</td>
            <td>${r.is_active ? `<button class="btn btn-red btn-sm" onclick="deactivateRx('${r.id}')">Deactivate</button>` : '—'}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

function filterPrescriptions() {
    const q = document.getElementById('rxSearch').value.toLowerCase();
    const filtered = rxCache.filter(r =>
        (r.patient_name || '').toLowerCase().includes(q) ||
        (r.patient_phone || '').includes(q)
    );
    renderRxTable(filtered);
}

function toggleRx(activeOnly) {
    rxActiveOnly = activeOnly;
    document.getElementById('rxToggleAll').classList.toggle('on', !activeOnly);
    document.getElementById('rxToggleActive').classList.toggle('on', activeOnly);
    loadPrescriptions();
}

function openRxModal() {
    document.getElementById('m-rxPhone').value = '';
    document.getElementById('m-rxName').value = '';
    document.getElementById('m-rxMedicine').value = '';
    document.getElementById('m-rxDosage').value = '';
    document.getElementById('m-rxFrequency').value = 'Once daily';
    document.getElementById('m-rxStart').value = new Date().toISOString().split('T')[0];
    document.getElementById('m-rxEnd').value = '';
    document.getElementById('m-rxNotes').value = '';
    document.getElementById('rxModalMsg').innerHTML = '';
    document.getElementById('timeInputs').innerHTML = '<div class="time-row"><input type="time" value="09:00"></div>';
    document.getElementById('btn-rxSubmit').disabled = false;
    document.getElementById('btn-rxSubmit').textContent = 'Add Prescription';
    document.getElementById('rxModal').classList.add('open');
}

function closeRxModal() {
    document.getElementById('rxModal').classList.remove('open');
}

function addTimeInput() {
    const container = document.getElementById('timeInputs');
    const rows = container.querySelectorAll('.time-row');
    if (rows.length >= 4) return;
    const div = document.createElement('div');
    div.className = 'time-row';
    div.innerHTML = '<input type="time" value="12:00"><button onclick="this.parentElement.remove()">✕</button>';
    container.appendChild(div);
}

async function submitPrescription() {
    const phone = document.getElementById('m-rxPhone').value.trim();
    const name = document.getElementById('m-rxName').value.trim();
    const medicine = document.getElementById('m-rxMedicine').value.trim();
    const dosage = document.getElementById('m-rxDosage').value.trim();
    const frequency = document.getElementById('m-rxFrequency').value;
    const startDate = document.getElementById('m-rxStart').value;
    const endDate = document.getElementById('m-rxEnd').value;
    const notes = document.getElementById('m-rxNotes').value.trim();

    const timeInputs = document.getElementById('timeInputs').querySelectorAll('input[type=time]');
    const reminderTimes = Array.from(timeInputs).map(i => i.value).filter(v => v);

    if (!phone || !name || !medicine || !dosage || !endDate || !reminderTimes.length) {
        msg('rxModalMsg', 'Please fill all required fields', true);
        return;
    }

    const btn = document.getElementById('btn-rxSubmit');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const data = await apiPost('/admin/prescriptions', {
            patient_phone: phone,
            patient_name: name,
            medicine_name: medicine,
            dosage: dosage,
            frequency: frequency,
            reminder_times: reminderTimes,
            start_date: startDate,
            end_date: endDate,
            notes: notes || null
        });
        if (data.success) {
            msg('rxModalMsg', '✅ Prescription added!');
            setTimeout(() => { closeRxModal(); loadPrescriptions(); }, 1200);
        } else {
            msg('rxModalMsg', data.detail || 'Failed to add prescription', true);
            btn.disabled = false;
            btn.textContent = 'Add Prescription';
        }
    } catch (e) {
        msg('rxModalMsg', 'Failed: ' + e.message, true);
        btn.disabled = false;
        btn.textContent = 'Add Prescription';
    }
}

async function deactivateRx(id) {
    const ok = await confirmDialog('Deactivate this prescription reminder?', { okText: 'Deactivate', danger: true });
    if (!ok) return;
    try {
        const r = await fetch(API + '/admin/prescriptions/' + id + '/deactivate', {
            method: 'POST',
            headers: authHeaders()
        });
        const data = await r.json();
        if (data.success) {
            loadPrescriptions();
        } else {
            toast('Failed: ' + (data.detail || 'Error'), true);
        }
    } catch (e) { toast('Error: ' + e.message, true); }
}

// ═══════ PAYMENTS ═══════

const PAY_STATUS_BADGE = {
    confirmed: '<span style="color:var(--green);background:var(--green-bg);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">✅ Confirmed</span>',
    pending_payment: '<span style="color:var(--blue);background:var(--blue-bg);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">⏳ Pending Payment</span>',
    pending_review: '<span style="color:var(--amber);background:var(--amber-bg);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">⚠️ Pending Review</span>',
    expired: '<span style="color:var(--text3);background:var(--surface2);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">⏰ Expired</span>',
    refunded: '<span style="color:var(--pink);background:rgba(244,114,182,0.1);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">💸 Refunded</span>',
    cancelled: '<span style="color:var(--red);background:var(--red-bg);padding:3px 10px;border-radius:8px;font-size:0.78rem;font-weight:600">❌ Cancelled</span>',
};

async function loadPayments() {
    try {
        // Load stats
        const stats = await api('/admin/payments/stats');
        document.getElementById('s-pay-confirmed').textContent = stats.confirmed_count || 0;
        document.getElementById('s-pay-revenue').textContent = '₹' + (stats.confirmed_amount_rupees || 0).toLocaleString('en-IN');
        document.getElementById('s-pay-review').textContent = stats.pending_review_count || 0;
        document.getElementById('s-pay-refunded').textContent = stats.refunded_count || 0;
        document.getElementById('s-pay-expired').textContent = stats.expired_count || 0;

        // Load pending review
        const prData = await api('/admin/bookings/pending-review');
        const prCard = document.getElementById('pendingReviewCard');
        if (prData.bookings && prData.bookings.length > 0) {
            prCard.style.display = 'block';
            let prHtml = '<table><thead><tr><th>Ref</th><th>Patient</th><th>Doctor</th><th>Slot</th><th>Amount</th><th>Actions</th></tr></thead><tbody>';
            prData.bookings.forEach(b => {
                const amt = b.amount_paise ? '₹' + (b.amount_paise / 100).toFixed(0) : '—';
                prHtml += `<tr>
                    <td>${esc(b.booking_ref) || '—'}</td>
                    <td>${esc(b.patient_name) || '—'}<br><small style="color:var(--text3)">${esc(b.patient_phone) || ''}</small></td>
                    <td>${esc(b.doctor_name) || '—'}</td>
                    <td>${b.appointment_date || ''} ${b.appointment_time || ''}</td>
                    <td>${amt}</td>
                    <td>
                        <button class="btn btn-accent" style="padding:4px 12px;font-size:0.75rem" onclick="adminConfirmBooking('${b.id}')">✅ Confirm</button>
                        <button class="btn" style="padding:4px 12px;font-size:0.75rem;color:var(--red);border-color:var(--red)" onclick="adminRejectBooking('${b.id}')">❌ Reject</button>
                        <button class="btn" style="padding:4px 10px;font-size:0.75rem" onclick="showPaymentEvents('${b.id}')">📋</button>
                    </td>
                </tr>`;
            });
            prHtml += '</tbody></table>';
            document.getElementById('pendingReviewList').innerHTML = prHtml;
        } else {
            prCard.style.display = 'none';
        }

        // Load bookings
        const statusFilter = document.getElementById('payStatusFilter').value;
        const url = '/admin/bookings' + (statusFilter ? '?status=' + statusFilter : '');
        const data = await api(url);

        if (!data.bookings || data.bookings.length === 0) {
            document.getElementById('paymentsList').innerHTML = '<p style="color:var(--text3);text-align:center;padding:32px">No bookings found</p>';
            return;
        }

        let html = '<table><thead><tr><th>Ref</th><th>Patient</th><th>Doctor</th><th>Branch</th><th>Slot</th><th>Status</th><th>Amount</th><th>Payment ID</th><th>Actions</th></tr></thead><tbody>';
        data.bookings.forEach(b => {
            const badge = PAY_STATUS_BADGE[b.status] || b.status;
            const amt = b.amount_paise ? '₹' + (b.amount_paise / 100).toFixed(0) : '—';
            const payId = b.payment_id ? `<small>${b.payment_id.substring(0, 16)}...</small>` : '—';
            let actions = `<button class="btn" style="padding:4px 10px;font-size:0.75rem" onclick="showPaymentEvents('${b.id}')">📋 Trail</button>`;
            if (b.status === 'confirmed' && b.payment_id) {
                actions += ` <button class="btn" style="padding:4px 10px;font-size:0.75rem;color:var(--pink)" onclick="refundBooking('${b.id}')">💸 Refund</button>`;
            }
            html += `<tr>
                <td>${esc(b.booking_ref) || '—'}</td>
                <td>${esc(b.patient_name) || '—'}<br><small style="color:var(--text3)">${esc(b.patient_phone) || ''}</small></td>
                <td>${esc(b.doctor_name) || '—'}</td>
                <td>${esc(b.branch_name) || '—'}</td>
                <td>${b.appointment_date || ''} ${b.appointment_time || ''}</td>
                <td>${badge}</td>
                <td>${amt}</td>
                <td>${payId}</td>
                <td>${actions}</td>
            </tr>`;
        });
        html += '</tbody></table>';
        document.getElementById('paymentsList').innerHTML = html;
    } catch (e) {
        document.getElementById('paymentsList').innerHTML = emptyState('warning', 'Failed to load payments');
    }
}

async function showPaymentEvents(bookingId) {
    try {
        const data = await api('/admin/payment-events/' + bookingId);
        const events = data.events || [];
        if (events.length === 0) {
            document.getElementById('paymentEventsContent').innerHTML = '<p style="color:var(--text3)">No events recorded yet.</p>';
        } else {
            let html = '<div style="display:flex;flex-direction:column;gap:10px">';
            events.forEach(e => {
                const time = new Date(e.created_at).toLocaleString();
                const typeColors = {
                    confirmed: 'var(--green)', signature_failed: 'var(--red)',
                    mismatch_flagged: 'var(--amber)', expired: 'var(--text3)',
                    refund_initiated: 'var(--pink)', refund_completed: 'var(--pink)',
                    recovery_confirmed: 'var(--cyan)', manual_confirm: 'var(--blue)',
                };
                const color = typeColors[e.event_type] || 'var(--text2)';
                let payloadStr = '';
                try { payloadStr = typeof e.raw_payload === 'string' ? e.raw_payload : JSON.stringify(e.raw_payload, null, 2); } catch { payloadStr = String(e.raw_payload); }
                html += `<div style="background:var(--surface2);padding:12px 16px;border-radius:10px;border-left:3px solid ${color}">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                        <span style="font-weight:600;color:${color};font-size:0.85rem">${esc(e.event_type)}</span>
                        <span style="color:var(--text3);font-size:0.75rem">${time}</span>
                    </div>
                    <pre style="color:var(--text2);font-size:0.72rem;white-space:pre-wrap;word-break:break-all;margin:0;max-height:80px;overflow:auto">${esc(payloadStr)}</pre>
                </div>`;
            });
            html += '</div>';
            document.getElementById('paymentEventsContent').innerHTML = html;
        }
        document.getElementById('paymentEventsModal').style.display = 'flex';
    } catch (e) {
        toast('Error loading events: ' + e.message, true);
    }
}

async function adminConfirmBooking(bookingId) {
    const ok = await confirmDialog('Manually CONFIRM this booking? Only do this after verifying payment in Razorpay dashboard.', { title: 'Confirm booking', okText: 'Confirm Booking' });
    if (!ok) return;
    try {
        const r = await apiPost('/admin/bookings/' + bookingId + '/confirm', { admin_notes: 'Manual confirmation from admin panel' });
        if (r.success) { toast('Booking confirmed!'); loadPayments(); }
        else toast('Failed: ' + (r.detail || r.reason || 'Unknown error'), true);
    } catch (e) { toast('Error: ' + e.message, true); }
}

async function adminRejectBooking(bookingId) {
    const reason = await promptDialog('Reason for rejection (will initiate refund if payment was captured):', { title: 'Reject booking', okText: 'Reject Booking', danger: true, placeholder: 'e.g. Duplicate booking' });
    if (reason === null) return;
    try {
        const r = await apiPost('/admin/bookings/' + bookingId + '/reject', { admin_notes: reason });
        if (r.success) { toast('Booking rejected and refund initiated.'); loadPayments(); }
        else toast('Failed: ' + (r.detail || r.reason || 'Unknown error'), true);
    } catch (e) { toast('Error: ' + e.message, true); }
}

async function refundBooking(bookingId) {
    const reason = await promptDialog('Reason for refund:', { title: 'Refund booking', okText: 'Refund', danger: true, placeholder: 'e.g. Patient requested cancellation' });
    if (reason === null) return;
    try {
        const r = await apiPost('/admin/bookings/' + bookingId + '/refund', { reason: reason });
        if (r.success) { toast('Refund initiated! Refund ID: ' + (r.refund_id || 'processing')); loadPayments(); }
        else toast('Refund failed: ' + (r.detail || r.reason || 'Unknown error'), true);
    } catch (e) { toast('Error: ' + e.message, true); }
}

// ═══════ BRANCHES ═══════
let _selectedBranchId = null;

async function loadBranches() {
    try {
        const data = await api('/admin/branches');
        const branches = data.branches || [];
        document.getElementById('branchCount').textContent = branches.length + ' branches';

        if (branches.length === 0) {
            document.getElementById('branchList').innerHTML = emptyState('building', 'No branches created yet. Add your first branch below.');
            return;
        }

        let html = '<table><thead><tr><th>Locality</th><th>Address</th><th>Type</th><th>Status</th><th>Order</th><th>Actions</th></tr></thead><tbody>';
        branches.forEach(b => {
            const displayName = esc(b.short_name || b.name);
            const type = b.is_diagnostic
                ? '<span style="color:var(--cyan);font-size:0.8rem">🔬 Diagnostics</span>'
                : '<span style="color:var(--green);font-size:0.8rem">🏥 Clinic</span>';
            const status = b.is_active
                ? '<span class="badge badge-confirmed">Active</span>'
                : '<span class="badge badge-cancelled">Inactive</span>';
            const addrHtml = b.address
                ? `${esc(b.address)}${b.landmark ? '<br><small style="color:var(--text3)">📍 ' + esc(b.landmark) + '</small>' : ''}`
                : (b.landmark ? '<small style="color:var(--text3)">📍 ' + esc(b.landmark) + '</small>' : '—');
            html += `<tr>
                <td><strong>${displayName}</strong></td>
                <td>${addrHtml}</td>
                <td>${type}</td>
                <td>${status}</td>
                <td>${b.display_order}</td>
                <td>
                    ${myRole !== 'staff' ? `<button class="btn btn-ghost btn-sm" onclick="editBranch('${b.id}')">✏️</button>` : ''}
                    ${(myRole !== 'staff' || hasPermission('DOCTOR_BRANCH_ASSIGN')) ? `<button class="btn btn-ghost btn-sm" data-id="${esc(b.id)}" data-name="${esc(b.short_name || b.name)}" onclick="manageBranchDoctors(this.dataset.id, this.dataset.name)">👨‍⚕️</button>` : ''}
                    ${myRole !== 'staff' ? `<button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="deleteBranch('${b.id}')">🗑️</button>` : ''}
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        document.getElementById('branchList').innerHTML = html;
    } catch (e) {
        document.getElementById('branchList').innerHTML = emptyState('warning', 'Failed to load branches');
    }
}

async function saveBranch() {
    const id = document.getElementById('f-branchId').value;
    const locality = document.getElementById('f-branchLocality').value.trim();
    const body = {
        short_name: locality,
        name: locality,
        address: document.getElementById('f-branchAddr').value.trim() || null,
        landmark: document.getElementById('f-branchLandmark').value.trim() || null,
        maps_link: document.getElementById('f-branchMaps').value.trim() || null,
        phone: document.getElementById('f-branchPhone').value.trim() || null,
        is_diagnostic: document.getElementById('f-branchDiag').value === 'true',
        display_order: parseInt(document.getElementById('f-branchOrder').value) || 0,
    };

    if (!locality) { msg('branchMsg', 'Branch locality is required (e.g. Madhurwada, Kancharpalem)', true); return; }

    const btn = document.getElementById('btn-branchSave');
    if (btn.disabled) return; // guards against double-click / double-submit creating duplicate branches
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Saving...';

    try {
        if (id) {
            await apiPut('/admin/branches/' + id, body);
            msg('branchMsg', '✅ Branch updated!');
        } else {
            await apiPost('/admin/branches', body);
            msg('branchMsg', '✅ Branch created!');
        }
        resetBranchForm();
        loadBranches();
    } catch (e) {
        const detail = e.message || 'Unknown error';
        msg('branchMsg', detail.startsWith('Error:') ? detail : 'Error: ' + detail, true);
    } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
    }
}

async function editBranch(branchId) {
    try {
        const data = await api('/admin/branches');
        const branch = (data.branches || []).find(b => b.id === branchId);
        if (!branch) return;

        document.getElementById('f-branchId').value = branch.id;
        document.getElementById('f-branchLocality').value = branch.short_name || branch.name || '';
        document.getElementById('f-branchAddr').value = branch.address || '';
        document.getElementById('f-branchLandmark').value = branch.landmark || '';
        document.getElementById('f-branchMaps').value = branch.maps_link || '';
        document.getElementById('f-branchPhone').value = branch.phone || '';
        document.getElementById('f-branchDiag').value = branch.is_diagnostic ? 'true' : 'false';
        document.getElementById('f-branchOrder').value = branch.display_order || 0;
        document.getElementById('branchFormTitle').textContent = '✏️ Edit Branch: ' + (branch.short_name || branch.name);

        document.getElementById('branchFormCard').scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
        toast('Error loading branch: ' + e.message, true);
    }
}

async function deleteBranch(branchId) {
    const ok = await confirmDialog('Delete this branch? If it has no appointments, doctors, or connectors linked, it will be removed permanently — otherwise it will be deactivated.', { okText: 'Delete', danger: true });
    if (!ok) return;
    try {
        const res = await apiDel('/admin/branches/' + branchId);
        toast(res.deleted ? '✅ Branch deleted' : 'ℹ️ ' + (res.message || 'Branch deactivated'));
        loadBranches();
    } catch (e) {
        toast('Error: ' + e.message, true);
    }
}

function resetBranchForm() {
    const elId = document.getElementById('f-branchId');
    if (elId) elId.value = '';
    const elLocality = document.getElementById('f-branchLocality');
    if (elLocality) elLocality.value = '';
    const elAddr = document.getElementById('f-branchAddr');
    if (elAddr) elAddr.value = '';
    const elLandmark = document.getElementById('f-branchLandmark');
    if (elLandmark) elLandmark.value = '';
    const elMaps = document.getElementById('f-branchMaps');
    if (elMaps) elMaps.value = '';
    const elPhone = document.getElementById('f-branchPhone');
    if (elPhone) elPhone.value = '';
    const elDiag = document.getElementById('f-branchDiag');
    if (elDiag) elDiag.value = 'false';
    const elOrder = document.getElementById('f-branchOrder');
    if (elOrder) elOrder.value = '0';
    const elTitle = document.getElementById('branchFormTitle');
    if (elTitle) elTitle.textContent = '➕ Add New Branch';
}

const ROLE_PRESETS = {
    'STAFF': [],
    'RECEPTIONIST': [],
    'FRONT_DESK': [],
    'APPOINTMENT_MANAGER': [],
    'LAB_OPERATOR': [],
    'PHARMACY_OPERATOR': [],
    'DOCTOR_SCHEDULE_MANAGER': [
        'DOCTORS_UPDATE', 'DOCTOR_BRANCH_ASSIGN', 'DOCTOR_LEAVES_CREATE',
        'DOCTOR_LEAVES_DELETE', 'HOLIDAYS_CREATE', 'HOLIDAYS_DELETE'
    ],
    'BRANCH_MANAGER': [
        'DOCTORS_UPDATE', 'DOCTOR_BRANCH_ASSIGN', 'DOCTOR_LEAVES_CREATE',
        'DOCTOR_LEAVES_DELETE', 'HOLIDAYS_CREATE', 'HOLIDAYS_DELETE',
        'DOCTORS_CREATE', 'DOCTORS_DELETE', 'STAFF_VIEW'
    ],
    'CUSTOM_ROLE': []
};

async function populateStaffBranchDropdowns() {
    try {
        const data = await api('/admin/branches');
        const branches = (data.branches || []).filter(b => b.is_active);
        const selCreate = document.getElementById('f-staffBranch');
        const selEdit = document.getElementById('m-editStaffBranch');
        if (selCreate) {
            selCreate.innerHTML = '<option value="">All Branches (Tenant-wide)</option>';
            branches.forEach(b => {
                selCreate.innerHTML += `<option value="${b.id}">${esc(b.name)}</option>`;
            });
        }
        if (selEdit) {
            selEdit.innerHTML = '<option value="">All Branches (Tenant-wide)</option>';
            branches.forEach(b => {
                selEdit.innerHTML += `<option value="${b.id}">${esc(b.name)}</option>`;
            });
        }
    } catch (e) {}
}

function onStaffRoleChange() {
    const role = document.getElementById('f-staffRole').value;
    const defaults = ROLE_PRESETS[role] || [];
    document.querySelectorAll('.staff-perm-cb').forEach(cb => {
        cb.checked = defaults.includes(cb.value);
    });
}

function onEditStaffRoleChange() {
    const role = document.getElementById('m-editStaffRole').value;
    const defaults = ROLE_PRESETS[role] || [];
    document.querySelectorAll('.edit-staff-perm-cb').forEach(cb => {
        cb.checked = defaults.includes(cb.value);
    });
}

let _staffCache = [];
async function loadStaff() {
    try {
        await populateStaffBranchDropdowns();
        const data = await api('/admin/staff');
        const staff = data.staff || [];
        _staffCache = staff;
        document.getElementById('staffCount').textContent = staff.length + ' account' + (staff.length === 1 ? '' : 's');

        if (staff.length === 0) {
            document.getElementById('staffList').innerHTML = emptyState('users', 'No staff accounts yet. Create one below.');
            return;
        }

        let html = '<table><thead><tr><th>Username</th><th>Role</th><th>Branch</th><th>Permissions</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead><tbody>';
        staff.forEach(s => {
            const status = s.is_active
                ? '<span class="badge badge-confirmed">Active</span>'
                : '<span class="badge badge-cancelled">Inactive</span>';
            const roleBadge = `<span class="badge badge-completed">${esc(s.staff_role || 'STAFF')}</span>`;
            const branchLabel = s.branch_id ? `<span style="font-size:0.8rem; color:var(--accent)">Branch Scoped</span>` : '<span style="font-size:0.8rem; color:var(--text3)">All Branches</span>';
            const perms = (s.permissions && s.permissions.length) ? `<span title="${esc(s.permissions.join(', '))}" style="font-size:0.75rem; color:var(--text2);">${s.permissions.length} granted</span>` : '<span style="font-size:0.75rem; color:var(--text3);">Default</span>';

            html += `<tr>
                <td><strong>${esc(s.username)}</strong></td>
                <td>${roleBadge}</td>
                <td>${branchLabel}</td>
                <td>${perms}</td>
                <td>${status}</td>
                <td>${esc((s.created_at || '').slice(0, 10))}</td>
                <td>
                    ${hasPermission('STAFF_UPDATE') ? `<button class="btn btn-ghost" style="padding:3px 8px; font-size:0.78rem; margin-right:4px;" onclick="openEditStaff('${s.id}')">Edit</button>
                    <button class="btn btn-ghost" style="padding:3px 8px; font-size:0.78rem;" onclick="toggleStaff('${s.id}')">${s.is_active ? 'Deactivate' : 'Activate'}</button>` : ''}
                </td>
            </tr>`;
        });
        html += '</tbody></table>';
        document.getElementById('staffList').innerHTML = html;
    } catch (e) {
        document.getElementById('staffList').innerHTML = emptyState('warning', 'Failed to load staff: ' + esc(e.message));
    }
}

function generateStaffPassword() {
    const bytes = new Uint8Array(9);
    crypto.getRandomValues(bytes);
    const pwd = btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 12);
    document.getElementById('f-staffPassword').value = pwd;
}

async function createStaff() {
    const username = document.getElementById('f-staffUsername').value.trim();
    const password = document.getElementById('f-staffPassword').value;
    const staff_role = document.getElementById('f-staffRole').value;
    const branch_id = document.getElementById('f-staffBranch').value || null;
    const extra_permissions = Array.from(document.querySelectorAll('.staff-perm-cb:checked')).map(cb => cb.value);

    if (!username || username.length < 3) { msg('staffMsg', 'Username must be at least 3 characters', true); return; }
    if (!password || password.length < 8) { msg('staffMsg', 'Password must be at least 8 characters', true); return; }

    try {
        await apiPost('/admin/staff', {
            username,
            password,
            staff_role,
            branch_id,
            extra_permissions
        });
        msg('staffMsg', `✅ Staff login created — Username: <strong>${esc(username)}</strong>, Password: <strong>${esc(password)}</strong>. Copy these now and hand them to your staff member.`);
        document.getElementById('f-staffUsername').value = '';
        document.getElementById('f-staffPassword').value = '';
        document.querySelectorAll('.staff-perm-cb').forEach(cb => cb.checked = false);
        loadStaff();
    } catch (e) {
        msg('staffMsg', 'Error: ' + e.message, true);
    }
}

function openEditStaff(staffId) {
    const s = _staffCache.find(x => x.id === staffId);
    if (!s) return;
    document.getElementById('m-editStaffId').value = s.id;
    document.getElementById('m-editStaffUsername').value = s.username;
    document.getElementById('m-editStaffRole').value = s.staff_role || 'STAFF';
    document.getElementById('m-editStaffBranch').value = s.branch_id || '';
    document.getElementById('m-editStaffStatus').value = s.is_active ? 'true' : 'false';
    const held = s.permissions || [];
    document.querySelectorAll('.edit-staff-perm-cb').forEach(cb => {
        cb.checked = held.includes(cb.value);
    });
    document.getElementById('editStaffMsg').innerHTML = '';
    document.getElementById('editStaffModal').classList.add('open');
}

function closeEditStaffModal() {
    document.getElementById('editStaffModal').classList.remove('open');
}

async function submitEditStaff() {
    const staffId = document.getElementById('m-editStaffId').value;
    const staff_role = document.getElementById('m-editStaffRole').value;
    const branch_id = document.getElementById('m-editStaffBranch').value || null;
    const is_active = document.getElementById('m-editStaffStatus').value === 'true';
    const extra_permissions = Array.from(document.querySelectorAll('.edit-staff-perm-cb:checked')).map(cb => cb.value);

    try {
        await apiPut('/admin/staff/' + staffId, {
            staff_role,
            branch_id,
            is_active,
            extra_permissions
        });
        closeEditStaffModal();
        toast('Staff account updated successfully!');
        loadStaff();
    } catch (e) {
        msg('editStaffMsg', 'Error: ' + e.message, true);
    }
}

async function toggleStaff(staffId) {
    try {
        await apiPut('/admin/staff/' + staffId + '/toggle', {});
        loadStaff();
    } catch (e) {
        toast('Error: ' + e.message, true);
    }
}

async function manageBranchDoctors(branchId, branchName) {
    _selectedBranchId = branchId;
    document.getElementById('branchDoctorsCard').style.display = 'block';
    document.getElementById('branchDoctorsTitle').textContent = '👨‍⚕️ Doctors at: ' + branchName;

    // Load assigned doctors
    try {
        const data = await api('/admin/branches/' + branchId + '/doctors');
        const assignments = data.doctor_branches || [];

        if (assignments.length === 0) {
            document.getElementById('branchDoctorsList').innerHTML = '<p style="color:var(--text3);padding:12px">No doctors assigned yet.</p>';
        } else {
            let html = '<table><thead><tr><th>Doctor</th><th>Department</th><th>Session</th><th>Actions</th></tr></thead><tbody>';
            assignments.forEach(a => {
                const doc = a.doctors || {};
                const sessionBadge = {
                    both: '<span style="color:var(--green)">Both</span>',
                    morning: '<span style="color:var(--amber)">Morning</span>',
                    evening: '<span style="color:var(--blue)">Evening</span>',
                }[a.session] || a.session;

                html += `<tr>
                    <td><strong>${esc(doc.name) || 'Unknown'}</strong><br><small style="color:var(--text3)">${esc(doc.specialization) || ''}</small></td>
                    <td>${esc(doc.department) || '—'}</td>
                    <td>${sessionBadge}</td>
                    <td>${(myRole !== 'staff' || hasPermission('DOCTOR_BRANCH_ASSIGN')) ? `<button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="removeDoctorFromBranch('${branchId}', '${a.doctor_id}')">Remove</button>` : ''}</td>
                </tr>`;
            });
            html += '</tbody></table>';
            document.getElementById('branchDoctorsList').innerHTML = html;
        }
    } catch (e) {
        document.getElementById('branchDoctorsList').innerHTML = emptyState('warning', 'Failed to load assigned doctors');
    }

    // Load all doctors for the assignment dropdown
    try {
        const docs = await api('/admin/doctors');
        const select = document.getElementById('f-assignDoctor');
        select.innerHTML = '<option value="">Select doctor...</option>';
        (Array.isArray(docs) ? docs : []).forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = `${d.name} (${d.department})`;
            select.appendChild(opt);
        });
    } catch (e) {}

    document.getElementById('branchDoctorsCard').scrollIntoView({ behavior: 'smooth' });
}

async function assignDoctorToBranch() {
    const doctorId = document.getElementById('f-assignDoctor').value;
    const session = document.getElementById('f-assignSession').value;
    if (!doctorId || !_selectedBranchId) { toast('Please select a doctor', true); return; }

    try {
        await apiPost('/admin/branches/' + _selectedBranchId + '/doctors', {
            doctor_id: doctorId,
            session: session,
        });
        // Reload the doctor list for this branch
        const branchName = document.getElementById('branchDoctorsTitle').textContent.replace('👨‍⚕️ Doctors at: ', '');
        manageBranchDoctors(_selectedBranchId, branchName);
    } catch (e) {
        toast('Error assigning doctor: ' + (e.message || 'Unknown error'), true);
    }
}

async function removeDoctorFromBranch(branchId, doctorId) {
    const ok = await confirmDialog('Remove this doctor from the branch?', { okText: 'Remove', danger: true });
    if (!ok) return;
    try {
        await apiDel('/admin/branches/' + branchId + '/doctors/' + doctorId);
        const branchName = document.getElementById('branchDoctorsTitle').textContent.replace('👨‍⚕️ Doctors at: ', '');
        manageBranchDoctors(branchId, branchName);
    } catch (e) {
        toast('Error: ' + e.message, true);
    }
}

// ═══════ PAYMENT SETTINGS ═══════
async function loadProfile() {
    try {
        const data = await api('/admin/profile');
        document.getElementById('f-profileName').value = data.name || '';
        document.getElementById('f-profileAddress').value = data.hospital_address || '';
        document.getElementById('f-profileMaps').value = data.hospital_maps_link || '';
        document.getElementById('f-profileEmergency').value = data.hospital_emergency_number || '';
    } catch (e) {
        msg('profileMsg', 'Error loading profile: ' + e.message, true);
    }
}

async function saveProfile() {
    const name = document.getElementById('f-profileName').value.trim();
    if (!name) { msg('profileMsg', 'Hospital / bot name is required', true); return; }

    const body = {
        name,
        hospital_address: document.getElementById('f-profileAddress').value.trim(),
        hospital_maps_link: document.getElementById('f-profileMaps').value.trim(),
        hospital_emergency_number: document.getElementById('f-profileEmergency').value.trim(),
    };

    try {
        await apiPut('/admin/profile', body);
        msg('profileMsg', '✅ Profile saved!');
    } catch (e) {
        msg('profileMsg', 'Error: ' + e.message, true);
    }
}

async function loadPaymentSettings() {
    try {
        const data = await api('/admin/settings/payment');
        document.getElementById('f-payKeyId').value = data.razorpay_key_id || '';
        document.getElementById('f-payKeySecret').placeholder =
            data.razorpay_key_secret_masked ? 'Saved: ' + data.razorpay_key_secret_masked : 'Leave blank to keep existing';
        document.getElementById('f-payWebhookSecret').placeholder =
            data.razorpay_webhook_secret_masked ? 'Saved: ' + data.razorpay_webhook_secret_masked : 'Leave blank to keep existing';

        const mode = data.payment_mode || 'none';
        document.getElementById('f-payMode' + mode.charAt(0).toUpperCase() + mode.slice(1)).checked = true;
        document.getElementById('f-payPercent').value = data.payment_deposit_percent || '';
        document.getElementById('f-payPercentRow').style.display = mode === 'partial' ? 'block' : 'none';
    } catch (e) {
        msg('paySettingsMsg', 'Error loading payment settings: ' + e.message, true);
    }
}

async function savePaymentSettings() {
    const mode = document.querySelector('input[name="payMode"]:checked')?.value || 'none';
    const body = { payment_mode: mode };

    const keyId = document.getElementById('f-payKeyId').value.trim();
    const keySecret = document.getElementById('f-payKeySecret').value.trim();
    const webhookSecret = document.getElementById('f-payWebhookSecret').value.trim();
    if (keyId) body.razorpay_key_id = keyId;
    if (keySecret) body.razorpay_key_secret = keySecret;
    if (webhookSecret) body.razorpay_webhook_secret = webhookSecret;

    if (mode === 'partial') {
        const percent = parseInt(document.getElementById('f-payPercent').value, 10);
        if (!percent || percent < 1 || percent > 99) {
            msg('paySettingsMsg', 'Enter a deposit percentage between 1 and 99', true);
            return;
        }
        body.payment_deposit_percent = percent;
    }

    try {
        await apiPut('/admin/settings/payment', body);
        msg('paySettingsMsg', '✅ Payment settings saved!');
        document.getElementById('f-payKeySecret').value = '';
        document.getElementById('f-payWebhookSecret').value = '';
        loadPaymentSettings();
    } catch (e) {
        msg('paySettingsMsg', 'Error: ' + e.message, true);
    }
}

// ═══════ REPORT CONNECTOR (MocDoc) ═══════
let _connectorId = null;
let _connectorBranchesLoaded = false;
let _connectorTypes = [];

function _connBranchParam() {
    const branchId = document.getElementById('f-connectorBranch').value;
    return branchId ? '&branch_id=' + branchId : '';
}

async function loadConnectorsPage() {
    const showBranchPicker = myFeatures === null || myFeatures.includes('multi_branch');
    document.getElementById('connectorBranchCard').style.display = showBranchPicker ? 'block' : 'none';

    if (showBranchPicker && !_connectorBranchesLoaded) {
        try {
            const data = await api('/admin/branches');
            const select = document.getElementById('f-connectorBranch');
            const branches = (data.branches || []).filter(b => b.is_active);
            branches.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b.id;
                opt.textContent = b.name;
                select.appendChild(opt);
            });
            // Default to this admin's own branch, else the branch they last
            // saved connector credentials under, so the form doesn't appear
            // empty after logout/reload just because the picker reset.
            const remembered = myBranchId || localStorage.getItem('mediassist_connector_branch');
            if (remembered && branches.some(b => b.id === remembered)) {
                select.value = remembered;
            }
        } catch (e) {}
        _connectorBranchesLoaded = true;
    }

    if (_connectorTypes.length === 0) {
        await loadConnectorTypes();
    }
    await loadConnectorCredentials();
    await loadConnectorAuditLog();
    await loadFailedReports();
}

async function loadConnectorTypes() {
    try {
        const data = await api('/admin/connectors/types');
        _connectorTypes = data.types || [];
        const select = document.getElementById('f-connType');
        const previousValue = select.value;
        select.innerHTML = '';
        _connectorTypes.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.type;
            opt.textContent = t.display_name;
            select.appendChild(opt);
        });
        if (previousValue) select.value = previousValue;
    } catch (e) {
        _connectorTypes = [];
    }
}

function _currentSchema() {
    const type = document.getElementById('f-connType').value || 'mocdoc';
    const entry = _connectorTypes.find(t => t.type === type);
    return (entry && entry.schema) || [];
}

function _renderConnFields(cfg) {
    const container = document.getElementById('connFieldsContainer');
    const schema = _currentSchema();
    container.innerHTML = schema.map(f => {
        const isSecret = f.type === 'password';
        let placeholder = f.placeholder;
        if (isSecret && cfg.password_set) placeholder = 'Saved — leave blank to keep existing';
        if (f.key === 'username' && cfg.username_masked) placeholder = 'Saved: ' + cfg.username_masked;
        const value = (!isSecret && f.key !== 'username') ? (cfg[f.key] || '') : '';
        return `<div class="field"><label>${esc(f.label)}</label>` +
            `<input type="${isSecret ? 'password' : 'text'}" id="f-connField-${f.key}" placeholder="${esc(placeholder)}" value="${esc(value)}"></div>`;
    }).join('');
}

async function loadConnectorCredentials() {
    try {
        const data = await api(`/admin/connectors?clinic_id=${CLINIC_SCOPE}` + _connBranchParam());
        const conn = (data.connectors || [])[0];
        _connectorId = conn ? conn.id : null;
        const cfg = (conn && conn.config) || {};

        if (conn && conn.connector_type) {
            document.getElementById('f-connType').value = conn.connector_type;
        }
        _renderConnFields(cfg);

        document.getElementById('f-connAlertPhone').value = cfg.admin_alert_phone || '';
        document.getElementById('f-connPollMinutes').value = cfg.poll_interval_minutes || 10;
        document.getElementById('f-connEnabled').checked = conn ? !!conn.is_enabled : false;

        const statusEl = document.getElementById('connectorStatus');
        if (conn) {
            const lastRun = conn.last_run_at ? new Date(conn.last_run_at).toLocaleString() : 'never';
            const lastOk = conn.last_success_at ? new Date(conn.last_success_at).toLocaleString() : 'never';
            statusEl.textContent = (conn.is_enabled ? '' : 'Automatic polling OFF · ') +
                `Last run: ${lastRun} · Last success: ${lastOk}` +
                (conn.last_error ? ` · Last error: ${conn.last_error}` : '');
        } else {
            statusEl.textContent = 'No connector configured yet';
        }
    } catch (e) {
        msg('connectorMsg', 'Error loading connector: ' + e.message, true);
    }
}

async function saveConnectorCredentials() {
    const branchId = document.getElementById('f-connectorBranch').value;
    const connectorType = document.getElementById('f-connType').value || 'mocdoc';
    const body = {
        connector_type: connectorType,
        branch_id: branchId || null,
        admin_alert_phone: document.getElementById('f-connAlertPhone').value.trim() || null,
        poll_interval_minutes: parseInt(document.getElementById('f-connPollMinutes').value, 10) || null,
        is_enabled: document.getElementById('f-connEnabled').checked,
    };
    _currentSchema().forEach(f => {
        const el = document.getElementById('f-connField-' + f.key);
        if (!el) return;
        const val = el.value.trim();
        if (f.type === 'password') {
            if (val) body.password = val;
        } else if (f.key === 'username') {
            if (val) body.username = val;
        } else {
            body[f.key] = val || null;
        }
    });

    const btn = document.querySelector('#pg-connectors .btn-accent');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

    try {
        const res = await apiPut(`/admin/connectors?clinic_id=${CLINIC_SCOPE}`, body);
        toast('✅ Connector credentials saved!');
        if (branchId) localStorage.setItem('mediassist_connector_branch', branchId);
        // Small delay so the user sees the toast before the page refreshes
        setTimeout(() => loadConnectorsPage(), 500);
    } catch (e) {
        const errMsg = e.message || 'Unknown error';
        toast('❌ Save failed: ' + errMsg, true);
        msg('connectorMsg', 'Error: ' + errMsg, true);
        console.error('saveConnectorCredentials failed:', e);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
    }
}

function _renderConnectorTestResult(r, success) {
    const statusLine = success
        ? `<strong style="color:#2e7d32">✅ Login successful</strong> — ${r.reports_found || 0} report(s) found on the portal`
        : `<strong style="color:#c62828">⚠️ ${esc(r.error_message || 'Test failed')}</strong>`;
    const sample = r.sample || [];
    let sampleHtml = '';
    if (sample.length > 0) {
        sampleHtml = '<table style="margin-top:8px;width:100%"><thead><tr><th>Patient</th><th>Phone</th><th>VAM ID</th><th>Report</th></tr></thead><tbody>' +
            sample.map(s => `<tr><td>${esc(s.patient_name_masked || '')}</td><td>${esc(s.patient_phone_masked || '')}</td><td>${esc(s.vam_id || '—')}</td><td>${esc(s.report_name || '')}</td></tr>`).join('') +
            '</tbody></table>';
    }
    return `<div class="card" style="margin-top:12px;padding:12px">${statusLine}${sampleHtml}</div>`;
}

async function testConnector() {
    const id = _connectorId;
    if (!id) { toast('Save credentials first, then test.', true); return; }
    const btn = document.getElementById('btn-connTest');
    const resultEl = document.getElementById('connTestResult');
    resultEl.innerHTML = '';
    btn.disabled = true; btn.textContent = '⏳ Starting test...';
    try {
        await apiPost(`/admin/connectors/${id}/test`, {});
        btn.textContent = '⏳ Testing (polling...)';
        const result = await _pollConnectorStatus(id);
        if (result.status === 'done') {
            const r = result.result || {};
            toast(result.success ? `✅ Login OK — ${r.reports_found || 0} report(s) found` : `⚠️ ${r.error_message || 'Test failed'}`, !result.success);
            resultEl.innerHTML = _renderConnectorTestResult(r, result.success);
        } else if (result.status === 'error') {
            const r = (result.result || {});
            toast(`⚠️ ${r.error_message || 'Test failed'}`, true);
            resultEl.innerHTML = _renderConnectorTestResult(r, false);
        } else {
            toast('⚠️ Test timed out — check logs for details', true);
        }
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '🔍 Test Connection';
        loadConnectorsPage();
    }
}

async function runConnectorNow() {
    const id = _connectorId;
    if (!id) { toast('Save credentials first.', true); return; }
    const ok = await confirmDialog('Run the connector now instead of waiting for the next scheduled poll?', { okText: 'Run Now' });
    if (!ok) return;
    const btn = document.getElementById('btn-connRunNow');
    btn.disabled = true; btn.textContent = '⏳ Starting run...';
    try {
        await apiPost(`/admin/connectors/${id}/run-now`, {});
        btn.textContent = '⏳ Running (polling...)';
        const result = await _pollConnectorStatus(id);
        if (result.status === 'done') {
            const r = result.result || {};
            toast(result.success ? `✅ Run complete — ${r.reports_uploaded || 0} uploaded, ${r.reports_failed || 0} failed` : `⚠️ ${r.error_message || 'Run failed'}`, !result.success);
        } else if (result.status === 'error') {
            toast(`⚠️ ${(result.result || {}).error_message || 'Run failed'}`, true);
        } else {
            toast('⚠️ Run timed out — check logs for details', true);
        }
        loadConnectorsPage();
    } catch (e) {
        toast('Error: ' + e.message, true);
    } finally {
        btn.disabled = false; btn.textContent = '▶️ Run Now';
    }
}

async function _pollConnectorStatus(connectorId, maxWaitSec = 300) {
    const interval = 5000; // poll every 5 seconds
    const maxPolls = Math.ceil((maxWaitSec * 1000) / interval);
    for (let i = 0; i < maxPolls; i++) {
        await new Promise(r => setTimeout(r, interval));
        try {
            const data = await api(`/admin/connectors/${connectorId}/test-status?clinic_id=${CLINIC_SCOPE}`);
            if (data.status === 'done' || data.status === 'error') return data;
        } catch (e) { /* network blip — keep polling */ }
    }
    return { status: 'timeout' };
}

async function loadConnectorAuditLog() {
    const el = document.getElementById('connectorAuditList');
    if (!_connectorId) {
        el.innerHTML = emptyState('folder', 'Save credentials above to start syncing reports.');
        return;
    }
    try {
        const data = await api('/admin/connectors/' + _connectorId + '/audit-log?limit=20');
        const logs = data.audit_log || [];
        if (logs.length === 0) {
            el.innerHTML = emptyState('folder', 'No runs yet.');
            return;
        }
        let html = '<table><thead><tr><th>When</th><th>Status</th><th>Found</th><th>Uploaded</th><th>Failed</th><th>Duration</th><th>Error</th></tr></thead><tbody>';
        logs.forEach(l => {
            const statusBadge = l.run_status === 'success'
                ? '<span class="badge badge-confirmed">Success</span>'
                : l.run_status === 'partial'
                    ? '<span class="badge badge-pending">Partial</span>'
                    : '<span class="badge badge-cancelled">Failed</span>';
            html += `<tr>
                <td>${new Date(l.created_at).toLocaleString()}</td>
                <td>${statusBadge}</td>
                <td>${l.reports_found ?? 0}</td>
                <td>${l.reports_uploaded ?? 0}</td>
                <td>${l.reports_failed ?? 0}</td>
                <td>${l.duration_ms ? (l.duration_ms / 1000).toFixed(1) + 's' : '—'}</td>
                <td><small style="color:var(--text3)">${esc(l.error_message) || '—'}</small></td>
            </tr>`;
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = emptyState('warning', 'Failed to load run history');
    }
}

async function loadFailedReports() {
    const el = document.getElementById('failedReportsList');
    try {
        const data = await api(`/admin/connectors/failed-reports?clinic_id=${CLINIC_SCOPE}&unresolved_only=true` + _connBranchParam());
        const rows = data.failed_reports || [];
        document.getElementById('failedReportCount').textContent = rows.length + ' unresolved';

        if (rows.length === 0) {
            el.innerHTML = emptyState('success', 'No undelivered reports — all clear.');
            return;
        }
        let html = '<table><thead><tr><th>Patient</th><th>Report ID</th><th>Failures</th><th>Last Error</th><th>Last Attempt</th><th>Actions</th></tr></thead><tbody>';
        rows.forEach(r => {
            html += `<tr>
                <td><strong>${esc(r.patient_name) || 'Unknown'}</strong><br><small style="color:var(--text3)">${esc(r.vam_id)}</small></td>
                <td>${esc(r.external_report_id)}</td>
                <td>${r.failure_count}</td>
                <td><small style="color:var(--text3)">${esc(r.last_error) || '—'}</small></td>
                <td>${r.last_attempt_at ? new Date(r.last_attempt_at).toLocaleString() : '—'}</td>
                <td><button class="btn btn-ghost btn-sm" onclick="resolveFailedReport('${r.id}')">Mark Resolved</button></td>
            </tr>`;
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = emptyState('warning', 'Failed to load failed reports');
    }
}

async function resolveFailedReport(failedReportId) {
    const ok = await confirmDialog('Mark this report as resolved? Use this once staff has manually re-sent it to the patient.', { okText: 'Mark Resolved' });
    if (!ok) return;
    try {
        await apiPost(`/admin/connectors/failed-reports/${failedReportId}/resolve?clinic_id=${CLINIC_SCOPE}`, {});
        loadFailedReports();
    } catch (e) {
        toast('Error: ' + e.message, true);
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Diagnostic Center Operations & Report Triage
// ═══════════════════════════════════════════════════════════════════════════

let _diagQueueFilter = 'all';
let _diagQueueData = null;

async function loadDiagnosticDashboard() {
    const clinicDash = document.getElementById('clinicDashboardContent');
    const diagDash = document.getElementById('diagDashboardContent');
    if (clinicDash) clinicDash.style.display = 'none';
    if (diagDash) diagDash.style.display = 'block';

    try {
        const [stats, queue, branchesResp] = await Promise.all([
            api('/admin/diagnostic/stats'),
            api('/admin/reports/queue'),
            api('/admin/branches').catch(() => ({ branches: [] })),
        ]);
        const allBranches = branchesResp.branches || [];

        // Render Stats
        const rep = stats.reports_today || {};
        document.getElementById('s-diag-found').textContent = rep.total || 0;
        document.getElementById('s-diag-sent').textContent = rep.sent || 0;
        document.getElementById('s-diag-review').textContent = rep.needs_review || 0;
        document.getElementById('s-diag-failed').textContent = rep.failed || 0;

        // Render Connector Status
        const conn = stats.connector;
        const statusDot = document.getElementById('diagStatusDot');
        const statusTitle = document.getElementById('diagStatusTitle');
        const statusSub = document.getElementById('diagStatusSub');

        if (conn) {
            // Only label which branch this status is for when there's more
            // than one active branch — otherwise it's just noise.
            const connBranch = allBranches.find(b => b.id === conn.branch_id);
            const branchSuffix = (allBranches.filter(b => b.is_active).length > 1 && connBranch)
                ? ` (${connBranch.short_name || connBranch.name})` : '';
            const runningTag = conn.is_running_now ? ' ⏳ (Executing run now...)' : '';

            if (conn.health === 'active' || conn.health === 'healthy') {
                statusDot.style.background = 'var(--green)';
                statusDot.style.boxShadow = '0 0 10px var(--green)';
                statusTitle.textContent = 'Report Connector: Active & Healthy' + branchSuffix + runningTag;
                statusTitle.style.color = '#fff';
            } else if (conn.health === 'degraded' || conn.health === 'warning') {
                statusDot.style.background = 'var(--amber)';
                statusDot.style.boxShadow = '0 0 10px var(--amber)';
                statusTitle.textContent = 'Report Connector: Running with Errors' + branchSuffix + runningTag;
                statusTitle.style.color = 'var(--amber)';
            } else if (conn.health === 'stalled') {
                statusDot.style.background = 'var(--red)';
                statusDot.style.boxShadow = '0 0 10px var(--red)';
                statusTitle.textContent = 'Report Connector: Stalled / Inactive' + branchSuffix;
                statusTitle.style.color = 'var(--red)';
            } else if (conn.health === 'never_run') {
                statusDot.style.background = 'var(--text3)';
                statusDot.style.boxShadow = 'none';
                statusTitle.textContent = 'Report Connector: Enabled (Awaiting Initial Run)' + branchSuffix;
                statusTitle.style.color = 'var(--text2)';
            } else {
                statusDot.style.background = 'var(--text3)';
                statusDot.style.boxShadow = 'none';
                statusTitle.textContent = 'Report Connector: Disabled' + branchSuffix;
                statusTitle.style.color = 'var(--text2)';
            }
            const lastRunStr = conn.last_run_at ? new Date(conn.last_run_at).toLocaleTimeString() : 'Never';
            if (conn.health === 'disabled') {
                // A timestamp here only ever means a manual Run Now — saying
                // "Last run" alone reads as if it is still polling.
                statusSub.textContent = `Automatic polling is OFF — turn on "Connector Enabled" in Connector Settings · Last manual run: ${lastRunStr}`;
            } else {
                statusSub.textContent = `Last run: ${lastRunStr}` + (conn.last_error ? ` · Error: ${conn.last_error}` : '');
                if (conn.next_run_at) {
                    const nextRun = new Date(conn.next_run_at);
                    const mins = Math.max(0, Math.round((nextRun - new Date()) / 60000));
                    statusSub.textContent += ` · Next run in ~${mins}m`;
                }
            }
            if (conn.connector_count > 1) {
                statusSub.textContent += conn.unhealthy_count
                    ? ` · ${conn.unhealthy_count} of ${conn.connector_count} connectors need attention`
                    : ` · all ${conn.connector_count} connectors healthy`;
            }
        } else {
            statusDot.style.background = 'var(--text3)';
            statusDot.style.boxShadow = 'none';
            statusTitle.textContent = 'Report Connector: Not Configured';
            statusSub.textContent = 'Configure MocDoc credentials in Report Connector settings.';
        }

        // Render 90-Day Retention notice
        const expiringCount = stats.expiring_retention_count || 0;
        if (expiringCount > 0) {
            document.getElementById('diagRetentionExpiringNotice').style.display = 'block';
            document.getElementById('diagExpiringCount').textContent = expiringCount;
        } else {
            document.getElementById('diagRetentionExpiringNotice').style.display = 'none';
        }

        // Render Needs-Review Queue
        renderDiagReviewQueue(queue.needs_review || []);

        // Render Failed Deliveries Queue
        renderDiagFailedQueue(queue.failed_reports || [], queue.connector_failures || []);

        // Render WhatsApp Delivery Log
        loadDiagDeliveries();

    } catch (e) {
        console.error('Failed to load diagnostic dashboard:', e);
    }
}

function renderDiagReviewQueue(items) {
    const el = document.getElementById('diagReviewQueue');
    document.getElementById('diagReviewCountBadge').textContent = items.length + ' Pending';
    document.getElementById('diagReviewCountBadge').className = items.length > 0 ? 'badge badge-pending' : 'badge badge-confirmed';

    if (!items || items.length === 0) {
        el.innerHTML = emptyState('success', '✨ All clear! No reports pending verification.');
        return;
    }

    let html = '<table><thead><tr><th>Patient</th><th>Phone</th><th>Report Name</th><th>Conflict / Reason</th><th>Actions</th></tr></thead><tbody>';
    items.forEach(r => {
        const phone = r.patient_phone || 'Missing';
        const name = r.patient_name || 'Unknown';
        const reason = r.error_message || 'Patient match ambiguity / name mismatch';
        html += `<tr>
            <td><strong>${esc(name)}</strong></td>
            <td><code>${esc(phone)}</code></td>
            <td>${esc(r.report_name || r.report_type || 'Report')}</td>
            <td><span style="color:var(--amber); font-size:0.82rem;">⚠️ ${esc(reason)}</span></td>
            <td>
                <button class="btn btn-accent btn-sm" data-id="${esc(r.id)}" data-name="${esc(name)}" data-phone="${esc(phone)}" data-report="${esc(r.report_name || '')}" onclick="openResolveMatchModal(this.dataset.id, this.dataset.name, this.dataset.phone, this.dataset.report)">Resolve & Send</button>
            </td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
}

function renderDiagFailedQueue(labFailures, connectorFailures) {
    const el = document.getElementById('diagFailedQueue');
    const totalFailed = (labFailures.length || 0) + (connectorFailures.length || 0);
    document.getElementById('diagFailedCountBadge').textContent = totalFailed + ' Failed';

    if (totalFailed === 0) {
        el.innerHTML = emptyState('success', '✨ All clear! Zero failed deliveries.');
        return;
    }

    let html = '<table><thead><tr><th>Patient</th><th>Type / ID</th><th>Failure Details</th><th>Time</th><th>Actions</th></tr></thead><tbody>';

    labFailures.forEach(r => {
        const dateStr = r.uploaded_at ? new Date(r.uploaded_at).toLocaleString() : '—';
        html += `<tr>
            <td><strong>${esc(r.patient_name || 'Unknown')}</strong><br><small style="color:var(--text3)">${esc(r.patient_phone)}</small></td>
            <td><span class="badge badge-failed">WhatsApp Send</span><br><small style="color:var(--text3)">${esc(r.report_name)}</small></td>
            <td class="cell-wrap"><small style="color:var(--red)">${esc(r.error_message || 'Delivery rejected')}</small></td>
            <td><small style="color:var(--text3)">${dateStr}</small></td>
            <td>
                <button class="btn btn-ghost btn-sm" onclick="retryFailedReport('${r.id}')">🔄 Retry</button>
            </td>
        </tr>`;
    });

    connectorFailures.forEach(r => {
        const dateStr = r.last_attempt_at ? new Date(r.last_attempt_at).toLocaleString() : '—';
        html += `<tr>
            <td><strong>${esc(r.patient_name || 'Unknown')}</strong><br><small style="color:var(--text3)">VAM: ${esc(r.vam_id)}</small></td>
            <td><span class="badge badge-pending">Connector Download</span><br><small style="color:var(--text3)">${esc(r.external_report_id)}</small></td>
            <td class="cell-wrap"><small style="color:var(--amber)">${esc(r.last_error || 'Download failed')}</small></td>
            <td><small style="color:var(--text3)">${dateStr}</small></td>
            <td>
                <button class="btn btn-ghost btn-sm" onclick="resolveFailedReport('${r.id}')">Mark Resolved</button>
            </td>
        </tr>`;
    });

    html += '</tbody></table>';
    el.innerHTML = html;
}

let _allDiagDeliveries = [];
let _diagDeliveryFilter = 'all';

function getDeliveryItemState(d) {
    if (!d) return 'pending';
    const status = (d.status || '').toLowerCase();
    const delStatus = (d.delivery_status || '').toLowerCase();
    const state = (d.state || '').toLowerCase();

    // 1. Delivered / Sent has top priority if status is sent or webhook confirmed
    if (
        status === 'sent' ||
        state === 'delivered' ||
        delStatus === 'delivered' ||
        delStatus === 'read' ||
        delStatus === 'sent'
    ) {
        return 'delivered';
    }

    // 2. Needs Review / Unmatched
    if (status === 'needs_review' || state === 'needs_review') {
        return 'needs_review';
    }

    // 3. Explicit Failed
    if (status === 'failed' || state === 'failed' || delStatus === 'failed') {
        return 'failed';
    }

    return 'pending';
}


function filterDiagDeliveries(state) {
    _diagDeliveryFilter = state || 'all';
    ['all', 'delivered', 'pending', 'failed', 'needs_review'].forEach(s => {
        const btn = document.getElementById('df-' + s);
        if (btn) {
            if (s === _diagDeliveryFilter) {
                btn.className = 'btn btn-sm diag-filter-btn active';
            } else {
                btn.className = 'btn btn-sm btn-ghost diag-filter-btn';
            }
        }
    });
    applyDiagDeliveryFilter();
}

function applyDiagDeliveryFilter() {
    const el = document.getElementById('diagDeliveryLog');
    if (!el) return;

    if (!_allDiagDeliveries || _allDiagDeliveries.length === 0) {
        loadDiagDeliveries();
        return;
    }

    let filtered = _allDiagDeliveries;
    const filter = (_diagDeliveryFilter || 'all').toLowerCase();

    if (filter === 'all') {
        filtered = _allDiagDeliveries;
    } else {
        filtered = _allDiagDeliveries.filter(d => getDeliveryItemState(d) === filter);
    }

    renderDiagDeliveries(filtered);
}

async function loadDiagDeliveries() {
    const el = document.getElementById('diagDeliveryLog');
    if (!el) return;
    try {
        const res = await api('/admin/lab-reports/deliveries');
        _allDiagDeliveries = res.deliveries || [];
        applyDiagDeliveryFilter();
    } catch (e) {
        el.innerHTML = emptyState('warning', 'Failed to load delivery log.');
    }
}

function renderDiagDeliveries(items) {
    const el = document.getElementById('diagDeliveryLog');
    if (!el) return;
    if (!items || items.length === 0) {
        el.innerHTML = emptyState('info', 'No delivery events match the selected filter.');
        return;
    }

    let html = '<table><thead><tr><th>Time</th><th>Patient</th><th>Phone</th><th>Report Details</th><th>Source</th><th>Status / Delivery Confirmation</th><th>Actions</th></tr></thead><tbody>';
    items.forEach(d => {
        const timeStr = d.sent_at ? new Date(d.sent_at).toLocaleString() : (d.uploaded_at ? new Date(d.uploaded_at).toLocaleString() : '—');
        const itemState = getDeliveryItemState(d);
        let badgeHtml = '';

        if (itemState === 'failed') {
            const err = d.delivery_error || d.error_message || 'Delivery failed';
            badgeHtml = `<span class="badge badge-failed" title="${esc(err)}">❌ Failed</span><br><small style="color:var(--red); font-size:0.75rem;">${esc(err)}</small>`;
        } else if (itemState === 'needs_review') {
            badgeHtml = `<span class="badge badge-pending" title="Unmatched or requires review">🟠 Unmatched</span>`;
        } else if (itemState === 'delivered') {
            const delSt = (d.delivery_status || '').toLowerCase();
            if (delSt === 'read') {
                badgeHtml = `<span class="badge badge-confirmed" title="Meta confirmed read by patient">✅ Read</span>`;
            } else if (delSt === 'delivered') {
                badgeHtml = `<span class="badge badge-confirmed" title="Meta confirmed delivery to device">✅ Delivered</span>`;
            } else {
                badgeHtml = `<span class="badge badge-confirmed" style="background:rgba(0,200,115,0.15); color:var(--green);" title="Delivered / Sent to WhatsApp">✅ Delivered</span>`;
            }
        } else {
            badgeHtml = `<span class="badge badge-pending" title="Pending">🕓 Pending</span>`;
        }



        const sourceBadge = d.source === 'mocdoc' 
            ? `<span class="badge" style="background:rgba(108,99,255,0.15); color:var(--accent2)">MocDoc</span>` 
            : `<span class="badge" style="background:var(--surface2); color:var(--text3)">Manual</span>`;

        html += `<tr>
            <td><small style="color:var(--text3)">${timeStr}</small></td>
            <td><strong>${esc(d.patient_name)}</strong></td>
            <td><code>${esc(d.patient_phone)}</code></td>
            <td><strong>${esc(d.report_name)}</strong><br><small style="color:var(--text3)">${esc(d.report_type)}</small></td>
            <td>${sourceBadge}</td>
            <td>${badgeHtml}</td>
            <td>
                ${d.state === 'failed' ? `<button class="btn btn-ghost btn-sm" onclick="retryFailedReport('${d.id}')">🔄 Retry</button>` : ''}
                ${d.state === 'needs_review' ? `<button class="btn btn-accent btn-sm" data-id="${esc(d.id)}" data-name="${esc(d.patient_name)}" data-phone="${esc(d.patient_phone)}" data-report="${esc(d.report_name)}" onclick="openResolveMatchModal(this.dataset.id, this.dataset.name, this.dataset.phone, this.dataset.report)">Resolve</button>` : ''}
            </td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
}

// ═══════ REPORT AUTOMATION QUEUE PAGE ═══════
async function loadDiagnosticQueuePage() {
    const el = document.getElementById('diagReportsQueueList');
    try {
        const filter = _diagQueueFilter === 'all' ? '' : `?status_filter=${_diagQueueFilter}`;
        const queue = await api('/admin/reports/queue' + filter);
        _diagQueueData = queue;
        renderDiagQueuePageTable(queue);
    } catch (e) {
        el.innerHTML = emptyState('warning', 'Failed to load report automation queue.');
    }
}

function setDiagQueueFilter(filter) {
    _diagQueueFilter = filter;
    document.getElementById('diagFilterAll').className = 'toggle-btn ' + (filter === 'all' ? 'on' : '');
    document.getElementById('diagFilterReview').className = 'toggle-btn ' + (filter === 'needs_review' ? 'on' : '');
    document.getElementById('diagFilterFailed').className = 'toggle-btn ' + (filter === 'failed' ? 'on' : '');
    loadDiagnosticQueuePage();
}

function renderDiagQueuePageTable(queue) {
    const el = document.getElementById('diagReportsQueueList');
    const items = [];
    if (_diagQueueFilter === 'all' || _diagQueueFilter === 'needs_review') {
        (queue.needs_review || []).forEach(r => items.push({ ...r, _queue_type: 'needs_review' }));
    }
    if (_diagQueueFilter === 'all' || _diagQueueFilter === 'failed') {
        (queue.failed_reports || []).forEach(r => items.push({ ...r, _queue_type: 'failed_report' }));
        (queue.connector_failures || []).forEach(r => items.push({ ...r, _queue_type: 'connector_failure' }));
    }

    if (items.length === 0) {
        el.innerHTML = emptyState('success', '✨ All clear! No items in this queue.');
        return;
    }

    let html = '<table><thead><tr><th>Patient</th><th>Phone</th><th>Report</th><th>Status / Issue</th><th>Date</th><th>Actions</th></tr></thead><tbody>';
    items.forEach(r => {
        const name = r.patient_name || 'Unknown';
        const phone = r.patient_phone || r.vam_id || '—';
        const report = r.report_name || r.external_report_id || 'Report';
        const dateStr = r.uploaded_at || r.last_attempt_at ? new Date(r.uploaded_at || r.last_attempt_at).toLocaleDateString() : '—';

        let statusHtml = '';
        let actionHtml = '';

        if (r._queue_type === 'needs_review') {
            statusHtml = `<span class="badge badge-pending">Needs Review</span><br><small style="color:var(--amber)">${esc(r.error_message || 'Name mismatch')}</small>`;
            actionHtml = `<button class="btn btn-accent btn-sm" data-id="${esc(r.id)}" data-name="${esc(name)}" data-phone="${esc(phone)}" data-report="${esc(report)}" onclick="openResolveMatchModal(this.dataset.id, this.dataset.name, this.dataset.phone, this.dataset.report)">Resolve & Send</button>`;
        } else if (r._queue_type === 'failed_report') {
            statusHtml = `<span class="badge badge-failed">Delivery Failed</span><br><small style="color:var(--red)">${esc(r.error_message || 'Rejected')}</small>`;
            actionHtml = `<button class="btn btn-ghost btn-sm" onclick="retryFailedReport('${r.id}')">🔄 Retry</button>`;
        } else {
            statusHtml = `<span class="badge badge-pending">Fetch Failed</span><br><small style="color:var(--amber)">${esc(r.last_error || 'HMIS error')}</small>`;
            actionHtml = `<button class="btn btn-ghost btn-sm" onclick="resolveFailedReport('${r.id}')">Mark Resolved</button>`;
        }

        html += `<tr>
            <td><strong>${esc(name)}</strong></td>
            <td><code>${esc(phone)}</code></td>
            <td>${esc(report)}</td>
            <td>${statusHtml}</td>
            <td><small style="color:var(--text3)">${dateStr}</small></td>
            <td>${actionHtml}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
}

function filterDiagReports() {
    const q = (document.getElementById('diagReportSearch').value || '').toLowerCase().trim();
    if (!_diagQueueData) return;
    if (!q) {
        renderDiagQueuePageTable(_diagQueueData);
        return;
    }
    const filtered = {
        needs_review: (_diagQueueData.needs_review || []).filter(r =>
            (r.patient_name || '').toLowerCase().includes(q) || (r.patient_phone || '').includes(q) || (r.report_name || '').toLowerCase().includes(q)
        ),
        failed_reports: (_diagQueueData.failed_reports || []).filter(r =>
            (r.patient_name || '').toLowerCase().includes(q) || (r.patient_phone || '').includes(q) || (r.report_name || '').toLowerCase().includes(q)
        ),
        connector_failures: (_diagQueueData.connector_failures || []).filter(r =>
            (r.patient_name || '').toLowerCase().includes(q) || (r.vam_id || '').toLowerCase().includes(q) || (r.external_report_id || '').toLowerCase().includes(q)
        ),
    };
    renderDiagQueuePageTable(filtered);
}

// ═══════ RESOLVE MATCH MODAL ACTIONS ═══════
function openResolveMatchModal(reportId, patientName, patientPhone, reportName) {
    document.getElementById('m-resolveReportId').value = reportId;
    document.getElementById('m-resolveReportName').value = reportName || 'Lab Report';
    document.getElementById('m-resolvePatientName').value = patientName && patientName !== 'Unknown' ? patientName : '';
    document.getElementById('m-resolvePhone').value = patientPhone && !patientPhone.includes('MISSING') ? patientPhone : '';
    document.getElementById('m-resolveSendNow').checked = true;
    document.getElementById('resolveModalMsg').innerHTML = '';
    document.getElementById('resolveMatchModal').classList.add('open');
}

function closeResolveMatchModal() {
    document.getElementById('resolveMatchModal').classList.remove('open');
}

async function submitResolveMatch() {
    const reportId = document.getElementById('m-resolveReportId').value;
    const phone = document.getElementById('m-resolvePhone').value.trim();
    const name = document.getElementById('m-resolvePatientName').value.trim();
    const sendNow = document.getElementById('m-resolveSendNow').checked;

    if (!phone) {
        msg('resolveModalMsg', 'Please enter a valid phone number', true);
        return;
    }

    const btn = document.getElementById('btn-resolveSubmit');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        await apiPost(`/admin/reports/${reportId}/resolve-match`, {
            patient_phone: phone,
            patient_name: name || null,
            send_now: sendNow,
        });

        toast('✅ Report resolved and updated!');
        closeResolveMatchModal();
        if (myPlan === 'diagstream') {
            loadDiagnosticDashboard();
        }
        loadDiagnosticQueuePage();
    } catch (e) {
        msg('resolveModalMsg', 'Error: ' + (e.message || 'Failed to resolve report'), true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save & Deliver';
    }
}

async function retryFailedReport(reportId) {
    const ok = await confirmDialog('Retry sending this medical report via WhatsApp?', { okText: 'Retry Now' });
    if (!ok) return;

    try {
        await apiPost(`/admin/reports/${reportId}/resend`, {});
        toast('✅ Report delivery retried successfully!');
        if (myPlan === 'diagstream') {
            loadDiagnosticDashboard();
        }
        loadDiagnosticQueuePage();
    } catch (e) {
        toast('Retry error: ' + (e.message || 'Delivery rejected'), true);
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// In-App Broadcast Notifications
// ═══════════════════════════════════════════════════════════════════════════

let notifPollingInterval = null;

function startNotificationPolling() {
    stopNotificationPolling();
    notifPollingInterval = setInterval(fetchNotificationCount, 60000);
}

function stopNotificationPolling() {
    if (notifPollingInterval) {
        clearInterval(notifPollingInterval);
        notifPollingInterval = null;
    }
}

async function fetchNotificationCount() {
    if (!auth) return;
    try {
        const res = await fetch(API + '/admin/notifications/unread-count', {
            headers: authHeaders()
        });
        if (!res.ok) return;
        const data = await res.json();
        const badge = document.getElementById('notifBadge');
        const count = data.unread_count || 0;
        if (count > 0) {
            badge.innerText = count > 99 ? '99+' : count;
            badge.style.display = 'inline-block';
        } else {
            badge.style.display = 'none';
        }
    } catch (e) {
        // Silently ignore background polling errors
    }
}

function toggleNotifDrawer() {
    const drawer = document.getElementById('notifDrawer');
    if (drawer.style.display === 'none' || !drawer.style.display) {
        drawer.style.display = 'flex';
        fetchNotificationsList();
    } else {
        drawer.style.display = 'none';
    }
}

function closeNotifDrawer() {
    const drawer = document.getElementById('notifDrawer');
    if (drawer) drawer.style.display = 'none';
}

async function fetchNotificationsList() {
    const listEl = document.getElementById('notifList');
    listEl.innerHTML = '<div style="text-align:center; padding:24px; color:var(--text3); font-size:0.85rem;"><div class="spin"></div>Loading broadcasts...</div>';

    try {
        const res = await fetch(API + '/admin/notifications?limit=30', {
            headers: authHeaders()
        });
        if (!res.ok) {
            listEl.innerHTML = '<div style="text-align:center; padding:20px; color:var(--red); font-size:0.85rem;">Failed to load alerts.</div>';
            return;
        }

        const data = await res.json();
        const notifications = data.notifications || [];

        if (notifications.length === 0) {
            listEl.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text3); font-size:0.85rem;">✨ No broadcast alerts at this time.</div>';
            return;
        }

        listEl.innerHTML = notifications.map(n => {
            const isUnread = !n.is_read;
            const bg = isUnread ? 'rgba(108,99,255,0.08)' : 'var(--surface2)';
            const border = isUnread ? '1px solid rgba(108,99,255,0.3)' : '1px solid var(--border)';
            const dateStr = n.created_at ? new Date(n.created_at).toLocaleDateString('en-IN', {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
            }) : '';

            return `
                <div style="background:${bg}; border:${border}; border-radius:12px; padding:12px 14px; display:flex; flex-direction:column; gap:6px;">
                    <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                        <strong style="font-size:0.88rem; color:${isUnread ? '#fff' : 'var(--text)'};">${esc(n.title)}</strong>
                        ${isUnread ? '<span style="background:var(--accent); color:#fff; font-size:0.65rem; font-weight:700; padding:1px 6px; border-radius:10px;">NEW</span>' : ''}
                    </div>
                    <p style="font-size:0.82rem; color:var(--text2); line-height:1.45; white-space:pre-wrap;">${esc(n.message)}</p>
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:4px; font-size:0.75rem; color:var(--text3);">
                        <span>${dateStr}</span>
                        ${isUnread ? `<button onclick="markNotificationRead('${n.id}')" style="background:none; border:none; color:var(--accent2); font-weight:600; cursor:pointer; font-size:0.75rem;">Mark as read</button>` : '<span style="color:var(--text3);">Read</span>'}
                    </div>
                </div>
            `;
        }).join('');

    } catch (e) {
        listEl.innerHTML = '<div style="text-align:center; padding:20px; color:var(--red); font-size:0.85rem;">Network error loading alerts.</div>';
    }
}

async function markNotificationRead(notifId) {
    try {
        const res = await fetch(API + `/admin/notifications/${notifId}/read`, {
            method: 'PATCH',
            headers: authHeaders()
        });
        if (res.ok) {
            fetchNotificationCount();
            fetchNotificationsList();
        }
    } catch (e) {
        toast('Failed to mark notification as read', true);
    }
}

async function markAllNotificationsRead() {
    try {
        const res = await fetch(API + '/admin/notifications/mark-all-read', {
            method: 'POST',
            headers: authHeaders()
        });
        if (res.ok) {
            toast('All notifications marked as read.');
            fetchNotificationCount();
            fetchNotificationsList();
        }
    } catch (e) {
        toast('Failed to mark all as read', true);
    }
}
