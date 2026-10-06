"""READ-ONLY: capture MocDoc's expanded row + download modal for one VAM ID.

Answers one question before the partial-report fix is written: when only some
of an order's tests are approved, which tests does the download modal list,
and are pending ones unchecked/disabled or absent?

Never clicks "Select" (#dwnld-sel), so nothing is downloaded, printed or
marked delivered. Takes no connector lock and writes nothing to the database.
Output (contains PHI — delete after review) goes to the gitignored
.connector_sessions/inspect_<VAM>/ folder.

    python scripts/inspect_mocdoc_modal.py --clinic-id <uuid> --vam-id VAM-55492 [--branch-id <uuid>]
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.config import settings  # noqa: E402
from app.database import supabase  # noqa: E402
from app.utils.connector_crypto import decrypt_password  # noqa: E402
from connectors.mocdoc import selectors as S  # noqa: E402
from connectors.mocdoc.worker import MocDocConnector  # noqa: E402

# Facts about every row in the modal, read in the page. Nothing is filtered
# out (header, "Download By ..." rows included) so a human sees it all.
_MODAL_ROWS_JS = """() => {
  const m = document.getElementById('download-modal');
  if (!m) return null;
  return Array.from(m.querySelectorAll('tr')).map(tr => {
    const cb = tr.querySelector('input[type=checkbox]');
    return {
      text: (tr.innerText || '').trim(),
      has_checkbox: !!cb,
      checked: cb ? cb.checked : null,
      disabled: cb ? cb.disabled : null,
      cb_attrs: cb ? Object.fromEntries(Array.from(cb.attributes).map(a => [a.name, a.value])) : null,
      tr_class: tr.className,
    };
  });
}"""


def _load_config(clinic_id: str, branch_id: str | None) -> dict:
    q = (
        supabase.table("integration_connectors").select("config")
        .eq("clinic_id", clinic_id).eq("connector_type", "mocdoc")
    )
    q = q.eq("branch_id", branch_id) if branch_id else q.is_("branch_id", "null")
    raw = q.single().execute().data["config"]
    config = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if config.get("password_encrypted"):
        config["password"] = decrypt_password(
            config["password_encrypted"], settings.connector_encryption_key
        )
    return config


async def main(clinic_id: str, vam_id: str, branch_id: str | None, partial: set | None = None) -> int:
    out = ROOT / ".connector_sessions" / f"inspect_{vam_id}"
    out.mkdir(parents=True, exist_ok=True)

    conn = MocDocConnector(
        clinic_id=clinic_id,
        config=_load_config(clinic_id, branch_id),
        medassist_url="http://unused.invalid",  # never submits
        integration_secret="",
        session_dir=tempfile.mkdtemp(prefix="mocdoc_inspect_"),  # don't touch prod cookies
        branch_id=branch_id,
    )
    try:
        if not await conn.authenticate():
            print("LOGIN FAILED")
            return 1
        page = conn._page

        # Navigate to lab orders and click Pending Print tab
        from urllib.parse import quote
        lab_url = f"{conn.base_url}{S.LAB_REPORTS_URL_TEMPLATE.format(clinic_slug=quote(conn.clinic_slug, safe=''))}"
        await page.goto(lab_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await conn._dismiss_all_modals()
        await page.evaluate(
            "(id) => { const t = document.getElementById(id); if (t) t.click(); }",
            S.PENDING_PRINT_TAB_ID,
        )
        await page.wait_for_timeout(3000)
        await conn._dismiss_all_modals()

        # Filter the DataTable to this VAM so pagination can't hide the row.
        await page.evaluate(
            """(v) => { const i = document.querySelector('.dataTables_filter input');
                        if (i) { i.value = v; i.dispatchEvent(new Event('input', {bubbles:true}));
                                 i.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true})); } }""",
            vam_id,
        )
        await page.wait_for_timeout(3000)

        rows = page.locator(S.REPORT_ROWS)
        target = None
        for i in range(await rows.count()):
            r = rows.nth(i)
            if "showorders" in (await r.get_attribute("class") or ""):
                continue
            if vam_id in await r.inner_text():
                target = r
                break
        if target is None:
            print(f"{vam_id} NOT FOUND in Pending Print — the order may sit in another tab.")
            (out / "tabs.html").write_text(
                await page.evaluate("(id) => { const t = document.getElementById(id); return ((t && t.closest('ul')) || document.body).outerHTML; }", S.PENDING_PRINT_TAB_ID),
                encoding="utf-8",
            )
            return 2

        (out / "patient_row.html").write_text(await target.evaluate("n => n.outerHTML"), encoding="utf-8")
        await target.locator(S.VIEW_BUTTON).first.click()
        await page.wait_for_timeout(4000)

        expanded = target.locator("xpath=following-sibling::tr[contains(@class,'showorders')][1]")
        await expanded.wait_for(state="attached", timeout=10000)
        (out / "expanded_row.html").write_text(await expanded.evaluate("n => n.outerHTML"), encoding="utf-8")
        await page.screenshot(path=str(out / "expanded_row.png"), full_page=True)

        icons = expanded.locator("a.downloadresult")
        n_icons = await icons.count()
        print(f"download-result icons in expanded row: {n_icons}")
        if n_icons == 0:
            print("No download icon — nothing approved yet. Expanded row saved.")
            return 0

        # Open the modal exactly as production does — then stop.
        await icons.first.evaluate("n => n.click()")
        await page.locator(S.DOWNLOAD_SELECT_BUTTON).first.wait_for(state="visible", timeout=10000)
        await page.wait_for_timeout(1500)

        (out / "modal.html").write_text(
            await page.evaluate("() => (document.getElementById('download-modal') || {}).outerHTML || ''"),
            encoding="utf-8",
        )
        modal_rows = await page.evaluate(_MODAL_ROWS_JS)
        (out / "modal_rows.json").write_text(json.dumps(modal_rows, indent=2), encoding="utf-8")
        await page.screenshot(path=str(out / "modal.png"), full_page=True)

        print("\nModal rows:")
        for r in modal_rows or []:
            print(f"  checkbox={r['has_checkbox']!s:5} checked={r['checked']!s:5} "
                  f"disabled={r['disabled']!s:5} | {r['text'][:70]}")

        if partial:
            return await _partial_download_check(conn, out, partial)

        await conn._close_download_modal()  # Close, never Select
        await conn._click_hide(target)
        print(f"\nSaved to {out}  (contains PHI — delete after review)")
        return 0
    finally:
        await conn.cleanup()


async def _partial_download_check(conn, out: Path, wanted: set) -> int:
    """--partial: tick only `wanted`, click Select the way production does,
    keep the PDF LOCALLY (never submitted), and report which of the modal's
    tests the PDF actually contains. Proves MocDoc honours a partial selection
    and not the still-ticked "select all" header."""
    from app.utils.pdf_reader import extract_text_from_pdf

    offered = {t["name"] for t in await conn._read_modal_tests()}
    missing = wanted - offered
    if missing:
        print(f"Not in this modal: {sorted(missing)}")
        await conn._close_download_modal()
        return 3
    if not await conn._select_tests(wanted):
        print("SELECTION DID NOT STICK — production would abort here (safe).")
        await conn._close_download_modal()
        return 4
    header = await conn._page.evaluate(
        "() => { const h = document.querySelector('#download-modal input.dwnld-item-all'); return h ? h.checked : null; }"
    )
    print(f"Ticked only {sorted(wanted)}; select-all header checked={header}")

    pdf = await conn._handle_download_modal("partial_check")  # no meta → no plan, just Select
    if not pdf:
        print("No PDF downloaded (bill pending / download failed) — see log above.")
        return 5
    (out / "partial.pdf").write_bytes(pdf)
    text = " ".join(extract_text_from_pdf(pdf).upper().split())
    (out / "partial.txt").write_text(text, encoding="utf-8")

    print(f"\nPDF: {len(pdf)} bytes. Which modal tests appear in it:")
    for name in sorted(offered):
        print(f"  {'WANTED ' if name in wanted else 'other  '} {'IN PDF ' if name in text else 'absent '} {name}")
    leaked = sorted(n for n in offered - wanted if n in text)
    got = sorted(n for n in wanted if n in text)
    print(f"\nwanted present: {got}\nunticked tests present: {leaked}")
    print(f"Saved to {out}  (contains PHI — delete after review)")
    return 0 if got and not leaked else 6


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clinic-id", required=True)
    ap.add_argument("--vam-id", required=True)
    ap.add_argument("--branch-id")
    ap.add_argument("--partial", help='Comma-separated tests to download ALONE, e.g. "LIPID PROFILE,URIC ACID". '
                                      "Clicks Select; PDF stays on this machine, never sent.")
    a = ap.parse_args()
    from connectors.mocdoc.worker import _norm_test_name
    partial = {_norm_test_name(t) for t in a.partial.split(",")} if a.partial else None
    sys.exit(asyncio.run(main(a.clinic_id, a.vam_id, a.branch_id, partial)))
