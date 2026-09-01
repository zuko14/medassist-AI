"""Execute the admin panel's withScope() helper against its edge cases.

withScope() sits on the path of every /admin call the panel makes: it is what
attaches ?clinic_id to ~80 request sites that previously sent nothing, which
was how a super_admin's requests reached the server with no tenant scope and
were widened to every clinic (KRIYA-TENANT-001, 2026-09-01).

The rest of the panel is only syntax-checked, but this one function is
security-relevant logic, so it gets run rather than eyeballed. It is extracted
from admin/index.html and executed under node — no build step, no JS test
framework, and the test skips itself where node is unavailable.

Two cases matter most:
  * The sentinel must never be transmitted. Sending clinic_id=default would
    reintroduce the exact string the server now rejects.
  * A path that already carries clinic_id must be left alone, or the handful
    of call sites that build it by hand would end up with two conflicting
    values.
"""

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parents[1] / "admin" / "index.html"

CASES = [
    # (clinic scope, input path, expected output)
    ("abc-123", "/admin/doctors", "/admin/doctors?clinic_id=abc-123"),
    ("abc-123", "/admin/stats?days=30", "/admin/stats?days=30&clinic_id=abc-123"),
    ("abc-123", "/admin/x?clinic_id=zzz", "/admin/x?clinic_id=zzz"),
    ("abc-123", "/admin/x?a=1&clinic_id=z", "/admin/x?a=1&clinic_id=z"),
    ("a b&c", "/admin/d", "/admin/d?clinic_id=a%20b%26c"),
    # A parameter that merely looks like clinic_id must not suppress scoping.
    ("abc-123", "/admin/x?my_clinic_idx=1", "/admin/x?my_clinic_idx=1&clinic_id=abc-123"),
    # Non-scopes must never be transmitted.
    ("default", "/admin/doctors", "/admin/doctors"),
    ("", "/admin/doctors", "/admin/doctors"),
    (None, "/admin/doctors", "/admin/doctors"),
]


def _extract_with_scope() -> str:
    source = PANEL.read_text(encoding="utf-8")
    match = re.search(r"function withScope\(path\) \{.*?\n\}", source, re.S)
    assert match, "withScope() not found in admin/index.html - did it get renamed?"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_with_scope_attaches_exactly_one_valid_clinic_id():
    script = _extract_with_scope() + textwrap.dedent(
        """
        const cases = __CASES__;
        const failures = [];
        for (const [scope, path, want] of cases) {
            CLINIC_SCOPE = scope;
            const got = withScope(path);
            if (got !== want) {
                failures.push(
                    "scope=" + JSON.stringify(scope) +
                    " path=" + path +
                    "\\n  got : " + got +
                    "\\n  want: " + want
                );
            }
        }
        if (failures.length) {
            console.log(failures.join("\\n"));
            process.exit(1);
        }
        """
    ).replace("__CASES__", json.dumps(CASES))

    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, (
        "admin panel withScope() misbehaved:\n"
        + (proc.stdout or "")
        + (proc.stderr or "")
    )


def test_panel_api_helpers_all_route_through_with_scope():
    """Every api helper must scope its request.

    api()/apiPost()/apiPut()/apiDel() are the four funnels the panel uses. If
    one is added or reverted to a bare `fetch(API + path)`, its call sites
    silently go out unscoped again - which is precisely how the doctor list
    and the branch dropdown leaked across tenants.
    """
    source = PANEL.read_text(encoding="utf-8")
    for helper in ("api", "apiPost", "apiPut", "apiDel"):
        match = re.search(rf"async function {helper}\(.*?\n\}}", source, re.S)
        assert match, f"{helper}() not found in admin/index.html"
        assert "withScope(path)" in match.group(0), (
            f"{helper}() issues a request without withScope(path); its call "
            f"sites would send no clinic_id and the server would have to guess."
        )
