"""Execute the admin panel's Appointments filter and dashboard tile links under node.

The properties a syntax check cannot see:

  * the query each filter sends — "Upcoming" must stay the page's original
    30-day appointment-date view, and "Last 30 days by booking date" must send
    period_days so the server cuts on the dashboard tiles' own boundary;
  * calendar arithmetic across month, year and leap-year edges;
  * a slow response for an OLD filter must not overwrite the table after the
    admin has already moved on to a newer one;
  * Check In / Cancel stay on today's and future bookings only — a past
    booking in the history view is not actionable;
  * the New Patients tile filters on the period_start /admin/stats reported,
    compared exactly as get_dashboard_stats() compares it.

Follows tests/test_admin_panel_branch_state.py: functions are extracted from
admin/index.html and executed under node, skipped where node is unavailable.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from app.routers.admin import _APPOINTMENT_LIST_MAX_SPAN_DAYS

PANEL = Path(__file__).resolve().parents[1] / "admin" / "index.html"

FUNCTIONS = (
    "istToday",
    "addDaysIso",
    "shiftMonthIso",
    "monthBounds",
    "fmtApptDate",
    "fmtApptMonth",
    "fmtBookedAt",
    "apptStatusLabel",
    "fmtApptTime",
    "apptSearchTerm",
    "applyApptSearch",
    "onApptSearchKey",
    "resetApptFilters",
    "apptQuery",
    "apptCaptionText",
    "apptStatusChipList",
    "syncApptControls",
    "renderApptChips",
    "renderApptPager",
    "loadAppointments",
    "renderAppointments",
    "setApptStatus",
    "pageAvailable",
    "openApptsFromDashboard",
    "openPatientsFromDashboard",
    "isNewPatient",
)

CONSTANTS = (
    "APPT_PAGE_SIZE",
    "APPT_MAX_SPAN_DAYS",
    "APPT_FILTER_DEFAULT",
    "APPT_STATUS_ORDER",
    "APPT_MONTHS",
    "APPT_MONTHS_LONG",
    "APPT_WEEKDAYS",
)


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def _extract(name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\(.*?\n\}}", _source(), re.S)
    assert match, f"{name}() not found in admin/index.html - did it get renamed?"
    return match.group(0)


def _constant(name: str) -> str:
    match = re.search(rf"^const {name} = .*;", _source(), re.M)
    assert match, f"const {name} not found in admin/index.html"
    return match.group(0)


HARNESS = textwrap.dedent(
    """
    function makeEl(id) {
        return { id, hidden: false, value: '', disabled: false, textContent: '', innerHTML: '',
                 title: '', options: [], dataset: {}, classList: { toggle() {} },
                 setAttribute() {}, scrollIntoView() {} };
    }
    const els = {};
    for (const id of ['apptList', 'apptFilterMsg', 'apptCaption', 'apptStatusChips', 'apptPager',
                      'apptDateField', 'apptMonthField', 'apptRangeField', 'apptDate', 'apptFrom',
                      'apptTo', 'apptMonth', 'apptYear', 'apptReset', 'apptSearch']) { els[id] = makeEl(id); }
    const navLinks = { appointments: { style: { display: '' } }, patients: { style: { display: '' } } };
    globalThis.document = {
        getElementById: (id) => els[id] || null,
        querySelectorAll: () => [],
        querySelector: (sel) => {
            const m = /data-page="([a-z]+)"/.exec(sel);
            return m ? navLinks[m[1]] || null : null;
        },
    };

    // ---- panel helpers the code under test leans on ----
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const emptyState = (_icon, text) => `<empty>${text}</empty>`;
    const loading = () => '<loading>';
    const badge = (s) => `<badge>${s}</badge>`;
    const refundCell = () => '';
    const bookingSubjectCell = (a) => esc(a.doctor_name);
    const APPT_STATUS_DOT = {};

    const apptFilter = { ...APPT_FILTER_DEFAULT };
    let apptLoadSeq = 0;
    let apptSearchTimer = null;
    let dashPeriodStart = null;
    let patientNewSince = null;
    const wentTo = [];
    globalThis.go = (page) => { wentTo.push(page); };

    // api() whose responses the test releases by hand, in any order.
    const pending = [];
    globalThis.api = (path) => new Promise((resolve, reject) => pending.push({ path, resolve, reject }));
    const tick = () => new Promise(r => setTimeout(r, 0));
    const params = (path) => Object.fromEntries(new URLSearchParams(path.split('?')[1]));

    const failures = [];
    function check(label, cond, detail) {
        if (!cond) failures.push(label + (detail !== undefined ? ' :: ' + detail : ''));
    }

    (async () => {
    __BODY__

    if (failures.length) { console.log(failures.join('\\n')); process.exit(1); }
    })().catch((e) => { console.log('threw: ' + ((e && e.stack) || e)); process.exit(1); });
    """
)


def _run(body: str):
    script = (
        "\n".join(_constant(c) for c in CONSTANTS)
        + "\n"
        + "\n".join(_extract(n) for n in FUNCTIONS)
        + "\n"
        + HARNESS.replace("__BODY__", textwrap.indent(textwrap.dedent(body), "    "))
    )
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=90)


def _assert_ok(proc):
    assert proc.returncode == 0, (
        "admin panel appointments filter misbehaved:\n" + (proc.stdout or "") + (proc.stderr or "")
    )


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def test_panel_span_limit_matches_the_server():
    assert _constant("APPT_MAX_SPAN_DAYS").startswith(
        f"const APPT_MAX_SPAN_DAYS = {_APPOINTMENT_LIST_MAX_SPAN_DAYS};"
    )


def test_each_filter_sends_the_query_the_server_expects():
    proc = _run(
        """
        const T = '2026-09-13';
        const q = (f) => Object.fromEntries(new URLSearchParams(apptQuery({ ...APPT_FILTER_DEFAULT, ...f }, T).qs));

        // The page's original view, unchanged — even if "Booked on" was left selected.
        let p = q({ preset: 'upcoming', basis: 'booked' });
        check('upcoming window', p.date_from === T && p.date_to === '2026-10-13', JSON.stringify(p));
        check('upcoming basis', p.date_basis === 'visit', p.date_basis);
        check('upcoming paging', p.limit === '50' && p.offset === '0', JSON.stringify(p));

        p = q({ preset: 'last30', basis: 'booked', status: 'confirmed' });
        check('dashboard window uses period_days', p.period_days === '30' && !p.date_from && !p.date_to, JSON.stringify(p));
        check('dashboard status', p.status === 'confirmed' && p.date_basis === 'booked', JSON.stringify(p));

        p = q({ preset: 'last30', basis: 'visit' });
        check('last 30 by visit date', p.date_from === '2026-08-14' && p.date_to === T && !p.period_days, JSON.stringify(p));

        p = q({ preset: 'date', basis: 'booked', date: '2026-09-05' });
        check('single booked day', p.date_from === '2026-09-05' && p.date_to === '2026-09-05' && p.date_basis === 'booked', JSON.stringify(p));

        p = q({ preset: 'month', month: '2026-02' });
        check('february', p.date_from === '2026-02-01' && p.date_to === '2026-02-28', JSON.stringify(p));
        p = q({ preset: 'month', month: '2028-02' });
        check('leap february', p.date_to === '2028-02-29', p.date_to);
        p = q({ preset: 'month', month: '2026-12' });
        check('december', p.date_to === '2026-12-31', p.date_to);

        check('inverted range refused', !!apptQuery({ ...APPT_FILTER_DEFAULT, preset: 'range', from: '2026-09-10', to: '2026-09-01' }, T).error);
        check('missing end refused', !!apptQuery({ ...APPT_FILTER_DEFAULT, preset: 'range', from: '2026-09-10', to: null }, T).error);
        check('367 days refused', !!apptQuery({ ...APPT_FILTER_DEFAULT, preset: 'range', from: '2026-01-01', to: '2027-01-02' }, T).error);
        check('366 days allowed', !apptQuery({ ...APPT_FILTER_DEFAULT, preset: 'range', from: '2028-01-01', to: '2028-12-31' }, T).error);

        check('day rolls the year', addDaysIso('2026-12-31', 1) === '2027-01-01');
        check('day rolls back into leap day', addDaysIso('2028-03-01', -1) === '2028-02-29');
        check('month rolls the year back', shiftMonthIso('2026-01', -1) === '2025-12');
        check('month rolls the year forward', shiftMonthIso('2026-12', 1) === '2027-01');
        check('today is a calendar date', /^\\d{4}-\\d{2}-\\d{2}$/.test(istToday()), istToday());

        const booked = apptCaptionText({ ...APPT_FILTER_DEFAULT, preset: 'date', basis: 'booked', date: '2026-09-05' }, T);
        check('caption: booked day', booked === 'Bookings made on Sat, 5 Sep 2026', booked);
        const month = apptCaptionText({ ...APPT_FILTER_DEFAULT, preset: 'month', month: '2026-02' }, T);
        check('caption: month', month === 'Appointments scheduled in February 2026', month);
        // 20:45 UTC is 02:15 the NEXT day in IST.
        check('booked-at is shown in IST', fmtBookedAt('2026-09-04T20:45:00.123456+00:00') === '5 Sep 2026, 2:15 am', fmtBookedAt('2026-09-04T20:45:00.123456+00:00'));
        check('bad timestamp', fmtBookedAt('nope') === '—' && fmtBookedAt(null) === '—');
        check('caption: upcoming unchanged', apptCaptionText(APPT_FILTER_DEFAULT, T) === 'Upcoming appointments in the next 30 days');

        const chips = apptStatusChipList({ cancelled: 2, confirmed: 0, completed: 5, zzz: 1 }, 'confirmed');
        check('chip order, zero shown only when selected', chips.join() === 'confirmed,completed,cancelled,zzz', chips.join());
        """
    )
    _assert_ok(proc)


def test_a_stale_response_never_overwrites_a_newer_filter():
    proc = _run(
        """
        const page = (ref) => ({ appointments: [{ id: ref, booking_ref: ref, status: 'completed', appointment_date: '2020-01-01' }],
                                  total: 1, window_total: 1, summary: { completed: 1 }, limit: 50, offset: 0 });
        const first = loadAppointments();              // slow: the old filter
        apptFilter.status = 'completed';
        const second = loadAppointments();             // the admin moved on
        check('two requests', pending.length === 2, pending.length);
        pending[1].resolve(page('NEW-REF'));
        await second;
        pending[0].resolve(page('OLD-REF'));
        await first;
        check('newer filter not rendered', els.apptList.innerHTML.includes('NEW-REF'));
        check('stale response overwrote the table', !els.apptList.innerHTML.includes('OLD-REF'));
        """
    )
    _assert_ok(proc)


def test_actions_only_on_today_and_future_bookings():
    proc = _run(
        """
        const today = istToday();
        const run = loadAppointments();
        pending[0].resolve({ total: 3, window_total: 3, summary: { confirmed: 3 }, limit: 50, offset: 0, appointments: [
            { id: 'past-1', status: 'confirmed', appointment_date: addDaysIso(today, -1) },
            { id: 'today-1', status: 'confirmed', appointment_date: today },
            { id: 'next-1', status: 'confirmed', appointment_date: addDaysIso(today, 1) },
        ] });
        await run;
        const html = els.apptList.innerHTML;
        check('past booking offered Cancel', !html.includes("cancelAppt('past-1')"));
        check('past booking offered Check In', !html.includes("checkInAppt('past-1')"));
        check("today's booking lost Cancel", html.includes("cancelAppt('today-1')"));
        check("today's booking lost Check In", html.includes("checkInAppt('today-1')"));
        check('future booking lost Cancel', html.includes("cancelAppt('next-1')"));
        """
    )
    _assert_ok(proc)


def test_a_page_past_the_end_falls_back_to_the_first_page():
    proc = _run(
        """
        apptFilter.offset = 50;
        const run = loadAppointments();
        check('did not ask for page 2', params(pending[0].path).offset === '50', pending[0].path);
        pending[0].resolve({ appointments: [], total: 3, window_total: 3, summary: { confirmed: 3 }, limit: 50, offset: 50 });
        await tick(); await tick();
        check('did not retry', pending.length === 2, pending.length);
        check('retry is not page 1', pending[1] && params(pending[1].path).offset === '0', pending[1] && pending[1].path);
        pending[1].resolve({ appointments: [{ id: 'a', booking_ref: 'REF-1', status: 'completed' }], total: 3, window_total: 3, summary: {}, limit: 50, offset: 0 });
        await run;
        check('first page not rendered', els.apptList.innerHTML.includes('REF-1'));
        """
    )
    _assert_ok(proc)


def test_a_failed_load_says_so_instead_of_showing_no_appointments():
    proc = _run(
        """
        const run = loadAppointments();
        pending[0].reject(new Error('500'));
        await run;
        check('failure hidden as empty', els.apptList.innerHTML.includes('Failed to load appointments'), els.apptList.innerHTML);
        """
    )
    _assert_ok(proc)


def test_dashboard_tiles_open_the_matching_lists():
    proc = _run(
        """
        apptFilter.preset = 'month'; apptFilter.month = '2025-01'; apptFilter.offset = 100;
        openApptsFromDashboard('cancelled');
        check('did not navigate', wentTo.join() === 'appointments', wentTo.join());
        check('tile window', apptFilter.preset === 'last30' && apptFilter.basis === 'booked', JSON.stringify(apptFilter));
        check('tile status', apptFilter.status === 'cancelled', apptFilter.status);
        check('tile kept stale paging', apptFilter.offset === 0, apptFilter.offset);
        openApptsFromDashboard(null);
        check('total tile kept a status', apptFilter.status === null, apptFilter.status);

        dashPeriodStart = '2026-08-14';
        openPatientsFromDashboard(true);
        check('new patients filter', patientNewSince === '2026-08-14', patientNewSince);
        // Same string comparison as get_dashboard_stats(): created_at >= period_start.
        check('patient on the boundary day is not new', isNewPatient({ created_at: '2026-08-14T00:00:01.123456+00:00' }));
        check('older patient counted as new', !isNewPatient({ created_at: '2026-08-13T23:59:59+00:00' }));
        check('patient without created_at counted as new', !isNewPatient({}));
        openPatientsFromDashboard(false);
        check('total patients kept the filter', patientNewSince === null && isNewPatient({}));

        navLinks.appointments.style.display = 'none';   // plan without the page
        wentTo.length = 0;
        openApptsFromDashboard('confirmed');
        check('opened a page the plan hides', wentTo.length === 0, wentTo.join());
        """
    )
    _assert_ok(proc)


def test_search_spans_all_dates_and_keeps_the_status_chip():
    proc = _run(
        """
        const T = '2026-09-13';
        const q = (f) => Object.fromEntries(new URLSearchParams(apptQuery({ ...APPT_FILTER_DEFAULT, ...f }, T).qs));

        // A search ignores whatever date view was open: it must find the patient anywhere.
        let p = q({ preset: 'month', month: '2025-01', q: 'Chaitanya', status: 'cancelled' });
        check('search sends q', p.q === 'Chaitanya', JSON.stringify(p));
        check('search sends no dates', !p.date_from && !p.date_to && !p.period_days && !p.date_basis, JSON.stringify(p));
        check('search keeps the status chip', p.status === 'cancelled', JSON.stringify(p));
        check('search pages', p.limit === '50' && p.offset === '0', JSON.stringify(p));
        check('caption says all dates', apptCaptionText({ ...APPT_FILTER_DEFAULT, q: 'Ravi' }, T) === 'Search results for “Ravi” across all dates');

        // Under two letters/digits is not a search: the server would 422 it.
        check('one letter is not a search', apptSearchTerm(' a ') === null);
        check('punctuation is not a search', apptSearchTerm('.,*%') === null);
        check('trimmed term', apptSearchTerm('  98765 ') === '98765');
        check('hindi name with vowel signs', apptSearchTerm('रा') === 'रा');

        // Typing a term loads page 1 of the search; Escape clears it.
        apptFilter.offset = 100;
        els.apptSearch.value = ' MC-2026-2001 ';
        applyApptSearch();
        check('search loaded', pending.length === 1 && params(pending[0].path).q === 'MC-2026-2001', pending[0] && pending[0].path);
        check('search reset paging', apptFilter.offset === 0, apptFilter.offset);
        applyApptSearch();
        check('same term reloaded', pending.length === 1, pending.length);
        pending[0].resolve({ appointments: [], total: 0, window_total: 0, summary: {}, limit: 50, offset: 0 });
        await tick();
        check('no-match message names the term', els.apptList.innerHTML.includes('match “MC-2026-2001”'), els.apptList.innerHTML);
        check('reset offered while searching', els.apptReset.hidden === false);

        onApptSearchKey({ key: 'Escape', target: els.apptSearch, preventDefault() {} });
        check('escape cleared the search', apptFilter.q === null && els.apptSearch.value === '', JSON.stringify(apptFilter));
        check('escape reloaded the date view', pending.length === 2 && !params(pending[1].path).q, pending[1] && pending[1].path);

        // The search term is escaped wherever it is echoed back.
        apptFilter.q = '<img src=x onerror=alert(1)>';
        const run = loadAppointments();
        pending[pending.length - 1].resolve({ appointments: [], total: 0, window_total: 0, summary: {}, limit: 50, offset: 0 });
        await run;
        check('term echoed unescaped', !els.apptList.innerHTML.includes('<img'), els.apptList.innerHTML);

        // Dashboard tiles open their own list, not the last search.
        openApptsFromDashboard('confirmed');
        check('tile kept the search', apptFilter.q === null, apptFilter.q);
        apptFilter.q = 'x1';
        resetApptFilters();
        check('reset kept the search', apptFilter.q === null);
        """
    )
    _assert_ok(proc)


def test_rows_show_phone_and_readable_date_and_time():
    proc = _run(
        """
        const run = loadAppointments();
        pending[0].resolve({ total: 2, window_total: 2, summary: { completed: 2 }, limit: 50, offset: 0, appointments: [
            { id: 'a', patient_name: 'Chaitanya Kumar', patient_phone: '919876543210', status: 'completed',
              appointment_date: '2026-08-26', appointment_time: '14:05:00' },
            { id: 'b', patient_name: null, patient_phone: '918888888888', status: 'completed',
              appointment_date: '2026-08-26', appointment_time: '00:30:00' },
        ] });
        await run;
        const html = els.apptList.innerHTML;
        check('phone under name', html.includes('Chaitanya Kumar<br><small class="appt-phone">919876543210</small>'), html);
        check('phone not repeated when it is the only identity', (html.match(/918888888888/g) || []).length === 1);
        check('readable date', html.includes('Wed, 26 Aug 2026'), html);
        check('12-hour time', html.includes('2:05 PM') && html.includes('12:30 AM'), html);
        check('odd time passes through escaped', fmtApptTime('<b>') === '&lt;b&gt;' && fmtApptTime(null) === '—');
        """
    )
    _assert_ok(proc)
