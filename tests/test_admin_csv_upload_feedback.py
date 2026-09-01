"""Execute the admin panel's CSV import feedback under node.

Staff imported a ~1000-row diagnostic catalogue, clicked "Upload & Import",
and saw nothing at all: the handler only set `btn.disabled = true`, so a
multi-second import looked like a dead button and they assumed it had not
registered. The fix is a reported phase for every step, which is only worth
anything if it actually fires on the success, rejection AND failure paths --
so those paths get run rather than eyeballed.

Follows tests/test_admin_panel_scope_helper.py: functions are extracted from
admin/index.html and executed under node, no build step, skipped where node
is unavailable.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parents[1] / "admin" / "index.html"

FUNCTIONS = (
    "esc",
    "_labCsvStatus",
    "_labCsvBusy",
    "_labCsvProgressBar",
    "_labCsvFileSize",
    "onLabCsvFileChosen",
    "submitLabTestCsv",
)


def _extract(name: str) -> str:
    source = PANEL.read_text(encoding="utf-8")
    match = re.search(rf"(?:async )?function {re.escape(name)}\(.*?\n\}}", source, re.S)
    assert match, f"{name}() not found in admin/index.html - did it get renamed?"
    return match.group(0)


HARNESS = textwrap.dedent(
    """
    // ---- minimal DOM ----
    const els = {};
    for (const id of ['labCsvResults', 'btnUploadLabCsv', 'btnCloseLabCsv', 'f-labCsvFile']) {
        els[id] = { id, style: {}, innerHTML: '', textContent: '', disabled: false, value: '', files: [] };
    }
    globalThis.document = { getElementById: (id) => els[id] || null };

    // ---- panel globals the handler leans on ----
    const toasts = [];
    globalThis.toast = (text, err) => toasts.push({ text, err: !!err });
    globalThis.API = '';
    globalThis.withScope = (p) => p + '?clinic_id=c1';
    globalThis.authHeaders = () => ({ Authorization: 'Basic xxx' });
    let loadCalls = 0;
    globalThis.loadLabTests = () => { loadCalls++; };
    globalThis.FormData = class { constructor() { this.parts = []; } append(k, v) { this.parts.push([k, v]); } };

    // ---- scripted XHR ----
    let SCRIPT = {};
    const seen = [];   // status markup captured at each phase, in order
    globalThis.XMLHttpRequest = class {
        constructor() { this.upload = {}; this.status = 0; this.responseText = ''; }
        open(method, url) { this.method = method; this.url = url; }
        setRequestHeader(k, v) { (this.headers = this.headers || {})[k] = v; }
        send() {
            setTimeout(() => {
                if (SCRIPT.progress) {
                    for (const [loaded, total] of SCRIPT.progress) {
                        this.upload.onprogress({ lengthComputable: true, loaded, total });
                        seen.push(els.labCsvResults.innerHTML);
                    }
                }
                if (SCRIPT.networkError) { this.onerror(); return; }
                this.upload.onload();
                seen.push(els.labCsvResults.innerHTML);
                this.status = SCRIPT.status;
                this.responseText = SCRIPT.responseText;
                this.onload();
            }, 0);
        }
    };

    function reset(script) {
        SCRIPT = script;
        seen.length = 0;
        toasts.length = 0;
        loadCalls = 0;
        els.labCsvResults.innerHTML = '';
        els.labCsvResults.style.display = 'none';
        els.btnUploadLabCsv.disabled = false;
        els.btnUploadLabCsv.textContent = 'Upload & Import';
        els['f-labCsvFile'].disabled = false;
        els['f-labCsvFile'].files = [{ name: 'catalogue.csv', size: 204800 }];
    }

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
        "admin panel CSV upload feedback misbehaved:\n"
        + (proc.stdout or "")
        + (proc.stderr or "")
    )


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def test_every_phase_reports_itself_on_a_successful_import():
    proc = _run(
        """
        reset({
            progress: [[51200, 204800], [204800, 204800]],
            status: 200,
            responseText: JSON.stringify({ created: 940, updated: 42, total_imported: 982 }),
        });
        await submitLabTestCsv();

        // The button must never sit silently disabled: it says what it is doing.
        check('no uploading toast', toasts.some(t => /Upload started/i.test(t.text)), JSON.stringify(toasts));
        check('no 25% phase', /25%/.test(seen[0]), seen[0]);
        check('no 100% phase', /100%/.test(seen[1]), seen[1]);
        check('no server-processing phase', /Validating and importing/i.test(seen[2]), seen[2]);

        const done = els.labCsvResults.innerHTML;
        check('no completion counts', /940/.test(done) && /42/.test(done) && /982/.test(done), done);
        check('completion not marked success', /Import complete/i.test(done), done);
        check('results panel hidden', els.labCsvResults.style.display === 'block');
        check('success toast missing', toasts.some(t => !t.err && /Import successful/i.test(t.text)));
        check('catalogue not refreshed', loadCalls === 1);
        check('button left disabled', els.btnUploadLabCsv.disabled === false);
        check('button label not restored', els.btnUploadLabCsv.textContent === 'Upload & Import',
              els.btnUploadLabCsv.textContent);
        check('close button left disabled', els.btnCloseLabCsv.disabled === false);
        check('file input left disabled', els['f-labCsvFile'].disabled === false);
        """
    )
    _assert_ok(proc)


def test_button_and_close_are_locked_while_the_import_is_in_flight():
    proc = _run(
        """
        reset({ progress: [[1, 2]], status: 200, responseText: '{"created":1,"updated":0,"total_imported":1}' });
        const p = submitLabTestCsv();
        check('button not disabled during upload', els.btnUploadLabCsv.disabled === true);
        check('button label not changed during upload', /Uploading/.test(els.btnUploadLabCsv.textContent),
              els.btnUploadLabCsv.textContent);
        check('close enabled mid-upload', els.btnCloseLabCsv.disabled === true);
        check('file input enabled mid-upload', els['f-labCsvFile'].disabled === true);
        await p;
        """
    )
    _assert_ok(proc)


def test_validation_rejection_says_nothing_was_saved_and_lists_the_bad_rows():
    proc = _run(
        """
        reset({
            status: 422,
            responseText: JSON.stringify({
                message: 'Import Rejected',
                errors: [{ row: 7, column: 'price_rupees', problem: 'Not a number', expected: 'A positive number' }],
            }),
        });
        await submitLabTestCsv();

        const html = els.labCsvResults.innerHTML;
        check('rejection not shown', /Import Rejected/.test(html), html);
        check('atomicity not stated', /nothing was saved/i.test(html), html);
        check('bad row not listed', /price_rupees/.test(html) && />7</.test(html), html);
        check('no error toast', toasts.some(t => t.err), JSON.stringify(toasts));
        check('catalogue refreshed after a rejected import', loadCalls === 0);
        check('button left disabled', els.btnUploadLabCsv.disabled === false);
        """
    )
    _assert_ok(proc)


def test_network_failure_is_reported_instead_of_hanging_on_the_progress_bar():
    proc = _run(
        """
        reset({ networkError: true });
        await submitLabTestCsv();

        const html = els.labCsvResults.innerHTML;
        check('network failure not reported', /Network error/i.test(html), html);
        check('still showing a progress bar', !/Uploading catalogue\\.csv/.test(html), html);
        check('retry safety not stated', /safely retry/i.test(html), html);
        check('no error toast', toasts.some(t => t.err), JSON.stringify(toasts));
        check('button left disabled after failure', els.btnUploadLabCsv.disabled === false);
        check('file input left disabled after failure', els['f-labCsvFile'].disabled === false);
        """
    )
    _assert_ok(proc)


def test_http_error_without_a_json_body_still_names_the_status():
    proc = _run(
        """
        reset({ status: 502, responseText: '<html>Bad Gateway</html>' });
        await submitLabTestCsv();
        const html = els.labCsvResults.innerHTML;
        check('status not surfaced', /502/.test(html), html);
        """
    )
    _assert_ok(proc)


def test_oversized_and_missing_files_are_refused_before_any_request():
    proc = _run(
        """
        reset({ status: 200, responseText: '{}' });
        els['f-labCsvFile'].files = [{ name: 'huge.csv', size: 6 * 1024 * 1024 }];
        await submitLabTestCsv();
        check('oversize not explained', /5 MB/.test(els.labCsvResults.innerHTML), els.labCsvResults.innerHTML);
        check('oversize file size not shown', /6\\.00 MB/.test(els.labCsvResults.innerHTML));
        check('no oversize toast', toasts.some(t => t.err));

        reset({ status: 200, responseText: '{}' });
        els['f-labCsvFile'].files = [];
        await submitLabTestCsv();
        check('missing file not explained', /No file selected/i.test(els.labCsvResults.innerHTML),
              els.labCsvResults.innerHTML);
        """
    )
    _assert_ok(proc)


def test_choosing_a_file_confirms_the_browser_took_it():
    proc = _run(
        """
        reset({ status: 200, responseText: '{}' });
        onLabCsvFileChosen();
        const html = els.labCsvResults.innerHTML;
        check('filename not echoed', /catalogue\\.csv/.test(html), html);
        check('size not echoed', /200 KB/.test(html), html);
        """
    )
    _assert_ok(proc)


def test_status_text_is_escaped_so_a_filename_cannot_inject_markup():
    proc = _run(
        """
        reset({ status: 200, responseText: '{}' });
        els['f-labCsvFile'].files = [{ name: '<img src=x onerror=alert(1)>.csv', size: 1024 }];
        onLabCsvFileChosen();
        check('filename injected raw markup', !/<img /.test(els.labCsvResults.innerHTML),
              els.labCsvResults.innerHTML);
        """
    )
    _assert_ok(proc)
