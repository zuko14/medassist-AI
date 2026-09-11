"""Execute the admin panel's tenant-scoped branch state under node.

Two defects, both invisible to a syntax check:

  * The connectors page built its branch dropdown by APPENDING to the select
    and then latching a "loaded" flag. That was safe only while the dropdown
    was filled exactly once per page load. It is now rebuilt on a tenant
    switch, so appending would stack the new clinic's branches underneath the
    previous clinic's and leave an admin choosing between two tenants' rows.

  * The sample collection window form carries 07:00-11:00 in its markup as a
    placeholder, and nothing ever read the saved hours back. An admin who
    opened the page to change only the operating days therefore saved those
    placeholders over the real hours, silently. The loader now fills the form
    from the server, and a FAILED read must disable saving rather than leave
    the placeholders armed.

Follows tests/test_admin_panel_scope_helper.py: functions are extracted from
admin/index.html and executed under node, no build step, skipped where node is
unavailable.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parents[1] / "admin" / "index.html"

FUNCTIONS = (
    "labTestBranchName",
    "_fillBranchSelect",
    "resetConnectorBranchState",
    "loadConnectorsPage",
    "_cwScopeText",
    "_cwFormEnabled",
    "_cwClearButton",
    "loadCollectionWindow",
    "changeCollectionWindowBranch",
)


def _extract(name: str) -> str:
    source = PANEL.read_text(encoding="utf-8")
    match = re.search(rf"(?:async )?function {re.escape(name)}\(.*?\n\}}", source, re.S)
    assert match, f"{name}() not found in admin/index.html - did it get renamed?"
    return match.group(0)


HARNESS = textwrap.dedent(
    """
    // ---- minimal DOM ----
    // A <select> is modelled faithfully enough for the property under test:
    // assigning innerHTML REPLACES its options, appendChild adds one.
    function makeEl(id) {
        return {
            id,
            style: {},
            value: '',
            disabled: false,
            textContent: '',
            options: [],
            set innerHTML(v) { this._html = v; this.options = v ? [{ value: '', textContent: v }] : []; },
            get innerHTML() { return this._html || ''; },
            appendChild(o) { this.options.push(o); },
        };
    }

    const els = {};
    for (const id of [
        'connectorBranchCard', 'f-connectorBranch',
        'cwBranchField', 'f-cwBranch', 'cwScopeNote', 'btnSaveCollectionWindow',
        'btnClearCollectionWindow',
        'cw-start', 'cw-end', 'cw-days', 'cw-sunday-start', 'cw-sunday-end',
    ]) { els[id] = makeEl(id); }

    globalThis.document = {
        getElementById: (id) => els[id] || null,
        createElement: () => ({ value: '', textContent: '' }),
    };
    globalThis.localStorage = { getItem: () => null, setItem: () => {} };

    // ---- panel globals ----
    let myFeatures = ['multi_branch'];
    let myBranchId = null;
    let labTestShowBranchCol = true;
    let labTestBranches = [];
    let cwBranchId = null;
    let cwSource = null;
    let _connectorId = null;
    let _connectorBranchesLoaded = false;
    let _connectorTypes = [{ type: 'mocdoc' }];

    let API_RESPONSES = {};
    let API_FAIL = null;
    const apiCalls = [];
    globalThis.api = async (path) => {
        apiCalls.push(path);
        if (API_FAIL) throw new Error(API_FAIL);
        const key = Object.keys(API_RESPONSES).find(k => path.startsWith(k));
        return key ? API_RESPONSES[key] : {};
    };
    globalThis.loadConnectorTypes = async () => {};
    globalThis.loadConnectorCredentials = async () => {};
    globalThis.loadConnectorAuditLog = async () => {};
    globalThis.loadFailedReports = async () => {};

    const failures = [];
    function check(label, cond, detail) {
        if (!cond) failures.push(label + (detail ? ' :: ' + detail : ''));
    }

    (async () => {
    __BODY__

    if (failures.length) { console.log(failures.join('\\n')); process.exit(1); }
    })().catch((e) => { console.log('threw: ' + ((e && e.stack) || e)); process.exit(1); });
    """
)


def _run(body: str):
    script = "\n".join(_extract(n) for n in FUNCTIONS) + "\n" + HARNESS.replace(
        "__BODY__", textwrap.indent(textwrap.dedent(body), "    ")
    )
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=90)


def _assert_ok(proc):
    assert proc.returncode == 0, (
        "admin panel branch state misbehaved:\n" + (proc.stdout or "") + (proc.stderr or "")
    )


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


# -- Connectors branch dropdown -----------------------------------------------


def test_connector_branches_are_rebuilt_not_appended_across_tenants():
    """Two tenants' branches must never coexist in one dropdown."""
    proc = _run(
        """
        API_RESPONSES = { '/admin/branches': { branches: [
            { id: 'a1', name: 'Clinic A Kukatpally', is_active: true },
            { id: 'a2', name: 'Clinic A Madhapur', is_active: true },
        ] } };
        await loadConnectorsPage();
        const first = els['f-connectorBranch'].options.length;
        check('clinic A branches missing', first === 3, 'options=' + first);

        // Tenant switch: the reset runs, then the page is opened again.
        resetConnectorBranchState();
        check('reset left stale options', els['f-connectorBranch'].options.length === 1,
              'options=' + els['f-connectorBranch'].options.length);
        check('reset did not clear the loaded flag', _connectorBranchesLoaded === false);

        API_RESPONSES = { '/admin/branches': { branches: [
            { id: 'b1', name: 'Clinic B Gachibowli', is_active: true },
        ] } };
        await loadConnectorsPage();
        const opts = els['f-connectorBranch'].options.map(o => o.textContent);
        check('clinic A branches survived the switch',
              !opts.some(t => /Clinic A/.test(t)), opts.join(','));
        check('clinic B branch missing', opts.some(t => /Gachibowli/.test(t)), opts.join(','));
        check('options were appended, not rebuilt', opts.length === 2, opts.join(','));
        """
    )
    _assert_ok(proc)


def test_inactive_branches_are_not_offered():
    proc = _run(
        """
        API_RESPONSES = { '/admin/branches': { branches: [
            { id: 'a1', name: 'Open Centre', is_active: true },
            { id: 'a2', name: 'Closed Centre', is_active: false },
        ] } };
        await loadConnectorsPage();
        const opts = els['f-connectorBranch'].options.map(o => o.textContent);
        check('closed branch offered', !opts.some(t => /Closed/.test(t)), opts.join(','));
        """
    )
    _assert_ok(proc)


# -- Sample collection window -------------------------------------------------


def test_saved_hours_are_loaded_into_the_form():
    proc = _run(
        """
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '08:00', end: '14:00', days: 'Mon,Tue',
                              sunday_start: '09:00', sunday_end: '11:00' },
            source: 'clinic',
        } };
        await loadCollectionWindow();
        check('start not loaded', els['cw-start'].value === '08:00', els['cw-start'].value);
        check('end not loaded', els['cw-end'].value === '14:00', els['cw-end'].value);
        check('days not loaded', els['cw-days'].value === 'Mon,Tue', els['cw-days'].value);
        check('sunday start not loaded', els['cw-sunday-start'].value === '09:00');
        check('sunday end not loaded', els['cw-sunday-end'].value === '11:00');
        check('save left disabled', els.btnSaveCollectionWindow.disabled === false);
        """
    )
    _assert_ok(proc)


def test_a_failed_read_disables_saving_and_leaves_the_form_untouched():
    """The whole point: placeholders must never be savable as real hours."""
    proc = _run(
        """
        els['cw-start'].value = '07:00';   // the markup's placeholder
        els['cw-days'].value = 'Mon,Tue,Wed,Thu,Fri,Sat,Sun';
        API_FAIL = 'Request failed (503)';
        await loadCollectionWindow();
        check('save still armed after a failed read',
              els.btnSaveCollectionWindow.disabled === true);
        check('form was overwritten on failure', els['cw-start'].value === '07:00');
        check('failure not explained', /Saving is disabled/.test(els.cwScopeNote.textContent),
              els.cwScopeNote.textContent);
        """
    )
    _assert_ok(proc)


def test_branch_scope_is_sent_and_described():
    proc = _run(
        """
        labTestBranches = [{ id: 'kpl', short_name: 'Kukatpally', name: 'Kukatpally Centre' }];
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '07:00', end: '21:00', days: 'Mon' }, source: 'clinic',
        } };
        els['f-cwBranch'].value = 'kpl';
        await changeCollectionWindowBranch();

        check('branch_id not sent', apiCalls.some(p => /branch_id=kpl/.test(p)), apiCalls.join(','));
        check('inheritance not explained',
              /follows the shared hours/.test(els.cwScopeNote.textContent),
              els.cwScopeNote.textContent);
        check('branch not named', /Kukatpally/.test(els.cwScopeNote.textContent),
              els.cwScopeNote.textContent);

        // Once an override exists the wording has to flip, or the admin cannot
        // tell whether they are editing shared or branch-only hours.
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '08:00', end: '14:00', days: 'Mon' }, source: 'branch',
        } };
        await loadCollectionWindow();
        check('override not announced', /has its own hours/.test(els.cwScopeNote.textContent),
              els.cwScopeNote.textContent);
        """
    )
    _assert_ok(proc)


def test_single_location_clinic_never_sees_the_branch_picker():
    proc = _run(
        """
        labTestShowBranchCol = false;
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '07:00', end: '11:00', days: 'Mon' }, source: 'default',
        } };
        await loadCollectionWindow();
        check('branch picker shown', els.cwBranchField.style.display === 'none',
              els.cwBranchField.style.display);
        check('branch_id sent by a single-location clinic',
              !apiCalls.some(p => /branch_id/.test(p)), apiCalls.join(','));
        check('scope note not empty', els.cwScopeNote.textContent === '',
              els.cwScopeNote.textContent);
        """
    )
    _assert_ok(proc)


def test_use_shared_hours_is_offered_only_where_there_is_an_override():
    """Clearing the shared record would swap in a default, not restore."""
    proc = _run(
        """
        labTestBranches = [{ id: 'kpl', short_name: 'Kukatpally' }];
        const clearBtn = els.btnClearCollectionWindow;

        // Shared hours selected: nothing to clear.
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '07:00', end: '21:00', days: 'Mon' }, source: 'clinic',
        } };
        await loadCollectionWindow();
        check('clear offered on the shared record', clearBtn.style.display === 'none',
              clearBtn.style.display);

        // Branch selected but still inheriting: still nothing to clear.
        cwBranchId = 'kpl';
        await loadCollectionWindow();
        check('clear offered on an inheriting branch', clearBtn.style.display === 'none',
              clearBtn.style.display);

        // Branch with its own hours: now it can go back to inheriting.
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '08:00', end: '14:00', days: 'Mon' }, source: 'branch',
        } };
        await loadCollectionWindow();
        check('clear not offered on an override', clearBtn.style.display === 'inline-flex',
              clearBtn.style.display);
        """
    )
    _assert_ok(proc)


def test_a_failed_read_hides_the_clear_button_too():
    """Source is unknown after a failure, so neither action is safe to offer."""
    proc = _run(
        """
        labTestBranches = [{ id: 'kpl', short_name: 'Kukatpally' }];
        cwBranchId = 'kpl';
        API_RESPONSES = { '/admin/lab-collection-window': {
            lab_collection: { start: '08:00', end: '14:00', days: 'Mon' }, source: 'branch',
        } };
        await loadCollectionWindow();
        check('setup: clear should be visible',
              els.btnClearCollectionWindow.style.display === 'inline-flex');

        API_FAIL = 'Request failed (503)';
        await loadCollectionWindow();
        check('clear still offered after a failed read',
              els.btnClearCollectionWindow.style.display === 'none',
              els.btnClearCollectionWindow.style.display);
        check('save still armed after a failed read',
              els.btnSaveCollectionWindow.disabled === true);
        """
    )
    _assert_ok(proc)
