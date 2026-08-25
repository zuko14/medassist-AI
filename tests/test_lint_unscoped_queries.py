"""CI Query Scoping Linter Test Suite (W1.2).

Ensures that every raw Supabase table access in `app/routers/**` is either:
1. Routed through `scoped_query(table_name, clinic_id, select_fields)`, or
2. Explicitly annotated with `# unscoped: <specific reason>` on the immediately
   preceding line or the query line itself.

Prevents multi-tenant isolation bypasses and unannotated query regressions in CI.
"""

import pathlib
import pytest


def test_lint_all_router_queries_are_scoped_or_annotated():
    """Fail if any unannotated raw supabase.table call exists in app/routers/."""
    routers_dir = pathlib.Path("app/routers")
    assert routers_dir.exists(), f"Directory not found: {routers_dir}"

    unannotated_calls = []

    for py_file in sorted(routers_dir.rglob("*.py")):
        lines = py_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "supabase.table(" in line:
                prev_line = lines[i - 1] if i > 0 else ""
                if "# unscoped:" not in prev_line and "# unscoped:" not in line:
                    unannotated_calls.append(
                        f"{py_file}:{i + 1}: {line.strip()[:100]}"
                    )

    if unannotated_calls:
        error_msg = (
            f"Found {len(unannotated_calls)} unannotated raw supabase.table() call(s) in app/routers/**.\n"
            f"Every query must use scoped_query() or have an explicit '# unscoped: <reason>' annotation:\n"
            + "\n".join(f"  - {call}" for call in unannotated_calls)
        )
        pytest.fail(error_msg)
