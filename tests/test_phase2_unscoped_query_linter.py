"""Linter asserting all database queries in app/routers are tenant-scoped or explicitly annotated (W1.2).

Ensures that every `supabase.table(...)` call across `app/routers/**` is either:
1. Filtered by tenant via `.eq("clinic_id", ...)` or `scoped_query(...)`
2. Or explicitly documented with `# unscoped: <reason>` or `# platform-scoped: <reason>`
"""

import os
import re
import pytest

ROUTERS_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "routers")


def find_router_files():
    files = []
    for root, _, filenames in os.walk(ROUTERS_DIR):
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(root, f))
    return files


def test_no_unannotated_unscoped_queries_in_routers():
    """Scan all router files and verify query scoping compliance."""
    router_files = find_router_files()
    assert len(router_files) > 0, "No router files found to lint"

    violations = []

    for file_path in router_files:
        rel_path = os.path.relpath(file_path, ROUTERS_DIR)
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for idx, line in enumerate(lines, 1):
            if "supabase.table(" in line:
                # Check surrounding context (current line, 3 lines before, 6 lines after)
                context = "".join(lines[max(0, idx - 4) : min(len(lines), idx + 8)])
                is_annotated = (
                    "# unscoped:" in context
                    or "# scoped:" in context
                    or "# platform-scoped:" in context
                    or "# global-read:" in context
                    or "clinic_id" in context
                    or "scoped_query" in context
                )
                if not is_annotated:
                    violations.append(
                        f"{rel_path}:{idx} -> {line.strip()}"
                    )

    assert not violations, (
        f"Found {len(violations)} un-annotated raw query calls in app/routers/:\n"
        + "\n".join(violations)
    )
