"""Kriya Diagnostic OS — B2B money (migration 132, DX-48…DX-52).

Rate cards (+ CSV import), the append-only ledger (charges are posted by the visit
functions in SQL), payments, statements (B2B-YYYY-NNNNN + PDF + WhatsApp to the
account desk), receivables ageing, holds (manual + the daily dx_b2b_hold_evaluation
job), pre-printed barcode stock and the partner portal's reads.
Design: docs/diagnostic-os/DATA-MODEL.md §9, ADR-DX-04/09/11/20, W5, W14, SECURITY §5.

Every amount, total and check is computed in SQL or plain integer arithmetic. Zero-LLM
(tests/test_dx_no_llm.py). No referral commission is computed anywhere (ADR-DX-11).
"""

import asyncio
import csv
import io
import logging
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services import dx_pdf
from app.services.dx import _actor, raise_for, rpc

logger = logging.getLogger(__name__)

BUCKET = "lab-reports"                       # existing private bucket, <clinic>/dx-statements/…
DEBITS = ("opening_balance", "charge")
STATEMENT_LINK_SECONDS = 7 * 24 * 3600
ACCOUNT_PUBLIC = ("id, code, name, kind, contact_name, contact_phone, desk_whatsapp, report_delivery, "
                  "default_billing_mode, white_label, status, hold_kind, hold_reason, rate_card_id, credit_limit_paise, "
                  "credit_days, gstin, billing_address, statement_cycle, copay_mode, copay_value")


async def _rows(query) -> list[dict]:
    try:
        res = await sb(query)
    except Exception as e:
        raise_for(e)
    return list(res.data or [])


async def _one(query, label: str) -> dict:
    rows = await _rows(query.limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    return rows[0]


async def _in(table: str, clinic_id: str, cols: str, column: str, ids) -> list[dict]:
    ids = sorted({str(i) for i in ids if i})
    out: list[dict] = []
    for i in range(0, len(ids), 100):
        out += await _rows(scoped_query(table, clinic_id, cols).in_(column, ids[i:i + 100]))
    return out


# ── accounts, balance, ledger ───────────────────────────────────────────────

async def get_account(clinic_id: str, account_id: str, cols: str = "*") -> dict:
    return await _one(scoped_query("dx_b2b_accounts", clinic_id, cols).eq("id", account_id), "B2B account")


async def balance(clinic_id: str, account_id: str) -> dict:
    rows = await rpc("dx_b2b_balance", {"p_clinic_id": clinic_id, "p_account_id": account_id})
    return (rows or [{}])[0] if isinstance(rows, list) else rows


async def account_summary(clinic_id: str, account_id: str, cols: str = "*") -> dict:
    acc = await get_account(clinic_id, account_id, cols)
    card = None
    if acc.get("rate_card_id"):
        cards = await _rows(scoped_query("dx_rate_cards", clinic_id, "id, name, kind, valid_from, valid_to, is_active")
                            .eq("id", acc["rate_card_id"]).limit(1))
        card = cards[0] if cards else None
    return {"account": acc, "balance": await balance(clinic_id, account_id), "rate_card": card}


async def balances(clinic_id: str) -> dict[str, dict]:
    """Balance per account for the accounts table (one ageing call computes them all)."""
    return {r["account_id"]: r for r in await ageing(clinic_id)}


async def ledger(clinic_id: str, account_id: str, statement_id: Optional[str] = None) -> list[dict]:
    """Every ledger row of the account, oldest first, with the running balance and the
    visit / patient / test it is for."""
    q = scoped_query("dx_b2b_ledger", clinic_id).eq("account_id", account_id)
    if statement_id:
        q = q.eq("statement_id", statement_id)
    rows = await _rows(q.order("created_at").order("id"))
    visits = {v["id"]: v for v in await _in("dx_accessions", clinic_id, "id, accession_number, patient_snapshot",
                                            "id", [r.get("accession_id") for r in rows])}
    items = {i["id"]: i for i in await _in("dx_accession_items", clinic_id, "id, item_name", "id",
                                           [r.get("accession_item_id") for r in rows])}
    running = 0
    for r in rows:
        running += r["amount_paise"] if r["entry_type"] in DEBITS else -r["amount_paise"]
        v = visits.get(r.get("accession_id")) or {}
        r.update(running_balance_paise=running, accession_number=v.get("accession_number"),
                 patient_name=(v.get("patient_snapshot") or {}).get("name"),
                 item_name=(items.get(r.get("accession_item_id")) or {}).get("item_name"))
    return rows


async def record_payment(clinic_id: str, account_id: str, body: dict, user) -> dict:
    aid, name = _actor(user)
    await get_account(clinic_id, account_id, "id")
    return await rpc("dx_b2b_record_payment", {
        "p_clinic_id": clinic_id, "p_account_id": account_id, "p_amount_paise": body["amount_paise"],
        "p_mode": body["mode"], "p_ref": body.get("reference"), "p_idempotency_key": body["idempotency_key"],
        "p_actor_id": aid, "p_actor_name": name})


async def set_hold(clinic_id: str, account_id: str, status: str, reason: Optional[str], user) -> dict:
    aid, name = _actor(user)
    return await rpc("dx_b2b_set_hold", {"p_clinic_id": clinic_id, "p_account_id": account_id, "p_status": status,
                                         "p_hold_kind": "manual" if status == "on_hold" else None, "p_reason": reason,
                                         "p_actor_id": aid, "p_actor_name": name})


async def ageing(clinic_id: str) -> list[dict]:
    return list(await rpc("dx_b2b_ageing", {"p_clinic_id": clinic_id}) or [])


# ── rate cards ──────────────────────────────────────────────────────────────

async def list_rate_cards(clinic_id: str) -> list[dict]:
    cards = await _rows(scoped_query("dx_rate_cards", clinic_id).order("name"))
    counts: dict[str, int] = {}
    for i in await _in("dx_rate_card_items", clinic_id, "rate_card_id", "rate_card_id", [c["id"] for c in cards]):
        counts[i["rate_card_id"]] = counts.get(i["rate_card_id"], 0) + 1
    return [{**c, "item_count": counts.get(c["id"], 0)} for c in cards]


async def get_rate_card(clinic_id: str, card_id: str) -> dict:
    card = await _one(scoped_query("dx_rate_cards", clinic_id).eq("id", card_id), "Rate card")
    items = await _rows(scoped_query("dx_rate_card_items", clinic_id, "id, lab_test_id, price_paise")
                        .eq("rate_card_id", card_id))
    tests = {t["id"]: t for t in await _in("lab_tests", clinic_id, "id, name, test_code, price_paise", "id",
                                           [i["lab_test_id"] for i in items])}
    for i in items:
        t = tests.get(i["lab_test_id"]) or {}
        i.update(test_name=t.get("name"), test_code=t.get("test_code"), mrp_paise=t.get("price_paise"))
    return {**card, "items": sorted(items, key=lambda i: (i.get("test_name") or ""))}


async def save_rate_card(clinic_id: str, body: dict, card_id: Optional[str] = None) -> dict:
    if card_id:
        rows = await _rows(supabase.table("dx_rate_cards").update(body).eq("clinic_id", clinic_id).eq("id", card_id))
        if not rows:
            raise HTTPException(status_code=404, detail="Rate card not found.")
        return rows[0]
    # unscoped: insert_scoped_by_payload
    return (await _rows(supabase.table("dx_rate_cards").insert({**body, "clinic_id": clinic_id})))[0]


async def set_rate_card_items(clinic_id: str, card_id: str, items: list[dict]) -> dict:
    """Upsert prices by test (one row per card x test). price_paise None removes the test."""
    await _one(scoped_query("dx_rate_cards", clinic_id, "id").eq("id", card_id), "Rate card")
    tests = {t["id"] for t in await _in("lab_tests", clinic_id, "id", "id", [i["lab_test_id"] for i in items])}
    missing = [i["lab_test_id"] for i in items if i["lab_test_id"] not in tests]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "dx_test_not_found", "message": "Test not found.",
                                                     "tests": missing[:20]})
    existing = {r["lab_test_id"]: r["id"] for r in await _rows(
        scoped_query("dx_rate_card_items", clinic_id, "id, lab_test_id").eq("rate_card_id", card_id))}
    added = updated = removed = 0
    new_rows = []
    for i in items:
        row_id = existing.get(i["lab_test_id"])
        if i.get("price_paise") is None:
            if row_id:
                await _rows(supabase.table("dx_rate_card_items").delete().eq("clinic_id", clinic_id).eq("id", row_id))
                removed += 1
        elif row_id:
            await _rows(supabase.table("dx_rate_card_items").update({"price_paise": i["price_paise"]})
                        .eq("clinic_id", clinic_id).eq("id", row_id))
            updated += 1
        else:
            new_rows.append({"clinic_id": clinic_id, "rate_card_id": card_id, "lab_test_id": i["lab_test_id"],
                             "price_paise": i["price_paise"]})
    if new_rows:
        # unscoped: insert_scoped_by_payload
        await _rows(supabase.table("dx_rate_card_items").insert(new_rows))
        added = len(new_rows)
    return {"added": added, "updated": updated, "removed": removed}


def parse_rate_csv(data: bytes) -> tuple[list[tuple[str, int]], list[str]]:
    """CSV with a test_code column and either price (rupees, up to 2 decimals) or price_paise.
    Returns ([(TEST_CODE, paise)], errors). Pure."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["The file must be UTF-8 CSV."]
    reader = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
    if "test_code" not in cols or not ({"price", "price_paise"} & set(cols)):
        return [], ["Columns needed: test_code and price (rupees) or price_paise."]
    out, errors, seen = [], [], set()
    for n, row in enumerate(reader, start=2):
        code = (row.get(cols["test_code"]) or "").strip().upper()
        if not code:
            continue
        raw = (row.get(cols.get("price_paise", "")) or "").strip() if "price_paise" in cols else ""
        try:
            if raw:
                paise = int(raw)
            else:
                rupees = (row.get(cols["price"]) or "").strip().replace(",", "")
                whole, _, frac = rupees.partition(".")
                if not whole.isdigit() or (frac and (not frac.isdigit() or len(frac) > 2)):
                    raise ValueError
                paise = int(whole) * 100 + int((frac + "00")[:2])
            if not 0 <= paise <= 100_000_000:
                raise ValueError
        except ValueError:
            errors.append(f"Row {n}: price for {code} is not a valid amount.")
            continue
        if code in seen:
            errors.append(f"Row {n}: {code} appears twice.")
            continue
        seen.add(code)
        out.append((code, paise))
    if not out and not errors:
        errors.append("The file has no rows.")
    return out, errors


async def import_rate_csv(clinic_id: str, card_id: str, data: bytes, dry_run: bool = False) -> dict:
    """All or nothing: any bad row or unknown test code refuses the whole file (money)."""
    rows, errors = parse_rate_csv(data)
    tests = await _rows(scoped_query("lab_tests", clinic_id, "id, test_code").is_("branch_id", "null"))
    by_code = {(t.get("test_code") or "").upper(): t["id"] for t in tests if t.get("test_code")}
    errors += [f"Unknown test code {code}." for code, _ in rows if code not in by_code]
    if errors:
        raise HTTPException(status_code=422, detail={"code": "dx_rate_import_invalid", "errors": errors[:50],
                                                     "message": "Nothing was imported: fix the rows listed."})
    if dry_run:
        return {"rows": len(rows), "applied": False}
    out = await set_rate_card_items(clinic_id, card_id, [{"lab_test_id": by_code[c], "price_paise": p} for c, p in rows])
    return {"rows": len(rows), "applied": True, **out}


# ── statements ──────────────────────────────────────────────────────────────

async def get_statement(clinic_id: str, statement_id: str) -> dict:
    return await _one(scoped_query("dx_b2b_statements", clinic_id).eq("id", statement_id), "Statement")


async def list_statements(clinic_id: str, account_id: str) -> list[dict]:
    return await _rows(scoped_query("dx_b2b_statements", clinic_id).eq("account_id", account_id)
                       .order("issued_at", desc=True))


async def render_statement(clinic_id: str, statement: dict) -> bytes:
    from app.services.tenant import get_clinic_by_id
    acc = await get_account(clinic_id, statement["account_id"])
    clinic = await get_clinic_by_id(clinic_id)
    rows = await ledger(clinic_id, statement["account_id"], statement["id"])
    pdf, _, _ = dx_pdf.render_statement_pdf(statement, acc, rows, {"name": clinic.get("name"),
                                                                   "address": clinic.get("address")})
    return pdf


async def issue_statement(clinic_id: str, account_id: str, period_from: str, period_to: str, user) -> dict:
    """dx_b2b_issue_statement (locks, totals, number) then the PDF stored under the clinic. A storage
    failure never undoes the statement: the PDF is rendered again on download."""
    aid, name = _actor(user)
    await get_account(clinic_id, account_id, "id")
    st = await rpc("dx_b2b_issue_statement", {"p_clinic_id": clinic_id, "p_account_id": account_id,
                                              "p_from": period_from, "p_to": period_to, "p_actor_id": aid,
                                              "p_actor_name": name})
    try:
        pdf = await render_statement(clinic_id, st)
        path = f"{clinic_id}/dx-statements/{st['statement_number']}.pdf"
        await asyncio.to_thread(supabase.storage.from_(BUCKET).upload, path, pdf,
                                {"content-type": "application/pdf", "upsert": "true"})
        rows = await _rows(supabase.table("dx_b2b_statements").update({"pdf_storage_path": path})
                           .eq("clinic_id", clinic_id).eq("id", st["id"]))
        st = rows[0] if rows else st
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - the statement stands; the PDF is re-rendered on demand
        logger.error(f"DX_STATEMENT_PDF_FAILED statement={st['id']} type={type(e).__name__}")
    return st


async def statement_pdf(clinic_id: str, statement: dict) -> bytes:
    if statement.get("pdf_storage_path") and statement.get("status") == "issued":
        try:
            return await asyncio.to_thread(supabase.storage.from_(BUCKET).download, statement["pdf_storage_path"])
        except Exception as e:  # noqa: BLE001 - fall back to a fresh render of the same rows
            logger.warning(f"DX_STATEMENT_DOWNLOAD_FAILED statement={statement['id']} type={type(e).__name__}")
    return await render_statement(clinic_id, statement)


async def void_statement(clinic_id: str, statement_id: str, reason: str, user) -> dict:
    aid, name = _actor(user)
    await get_statement(clinic_id, statement_id)
    return await rpc("dx_b2b_void_statement", {"p_clinic_id": clinic_id, "p_statement_id": statement_id,
                                               "p_reason": reason, "p_actor_id": aid, "p_actor_name": name})


async def send_statement(clinic: dict, statement: dict) -> dict:
    """W14: summary + a 7-day private link to the PDF, to the account's desk WhatsApp (in-session text;
    outside the 24 h window the send fails and the desk shares the PDF instead). Never raises."""
    from app.services.whatsapp import whatsapp_service
    clinic_id = str(clinic["id"])
    try:
        acc = await get_account(clinic_id, statement["account_id"], "name, desk_whatsapp, contact_phone, credit_days")
        phone = acc.get("desk_whatsapp") or acc.get("contact_phone")
        if not phone:
            return {"sent": False, "reason": "no_desk_phone"}
        if statement.get("status") != "issued":
            return {"sent": False, "reason": "statement_void"}
        link = None
        if statement.get("pdf_storage_path"):
            signed = await asyncio.to_thread(supabase.storage.from_(BUCKET).create_signed_url,
                                             statement["pdf_storage_path"], STATEMENT_LINK_SECONDS)
            link = (signed or {}).get("signedURL") or (signed or {}).get("signed_url")
        closing = int(statement["closing_paise"])
        text = (f"{clinic.get('name') or 'Lab'}: statement {statement['statement_number']} for {acc['name']}, "
                f"{dx_pdf.ist_date(statement['period_from'])} to {dx_pdf.ist_date(statement['period_to'])}. "
                f"Tests {dx_pdf._rupees(statement['total_charges_paise'])}, payments and credits "
                f"{dx_pdf._rupees(statement['total_credits_paise'])}, closing balance {dx_pdf._rupees(closing)}"
                + (f", due within {acc['credit_days']} days" if closing > 0 and acc.get("credit_days") is not None else "")
                + "." + (f"\nPDF: {link}" if link else ""))
        ok = bool(await whatsapp_service.send_text(clinic, phone, text, _source="dx_b2b_statement"))
        return {"sent": ok, "reason": None if ok else "whatsapp_send_failed"}
    except Exception as e:  # noqa: BLE001
        logger.error(f"DX_STATEMENT_SEND_FAILED statement={statement.get('id')} type={type(e).__name__}")
        return {"sent": False, "reason": "send_failed"}


# ── holds job ───────────────────────────────────────────────────────────────

async def hold_evaluation() -> int:
    """dx_b2b_hold_evaluation (daily 02:10 IST): every DX clinic, in SQL. Returns the number of changes."""
    # unscoped: platform_sweep
    clinics = await _rows(supabase.table("clinics").select("id").in_("dx_state", ["ACTIVE", "WIND_DOWN"]))
    changed = 0
    for c in clinics:
        cid = str(c["id"])
        try:
            out = await rpc("dx_b2b_evaluate_holds", {"p_clinic_id": cid}) or []
        except Exception as e:  # noqa: BLE001 - one clinic must not stop the sweep
            logger.error(f"DX_B2B_HOLD_EVAL_FAILED clinic={cid} type={type(e).__name__}")
            continue
        for ch in out:
            changed += 1
            logger.warning(f"DX_B2B_HOLD clinic={cid} account={ch['account_id']} status={ch['status']} "
                           f"kind={ch.get('hold_kind')} balance_paise={ch.get('balance_paise')} "
                           f"overdue_paise={ch.get('overdue_paise')}")
    return changed


# ── barcode stock ───────────────────────────────────────────────────────────

async def list_stock(clinic_id: str) -> list[dict]:
    return await _rows(scoped_query("dx_barcode_stock", clinic_id).order("prefix").order("range_from"))


async def issue_stock(clinic_id: str, body: dict, user) -> dict:
    if body.get("account_id"):
        await get_account(clinic_id, body["account_id"], "id")
    if body.get("site_id"):
        await _one(scoped_query("dx_sites", clinic_id, "id").eq("id", body["site_id"]), "Site")
    row = {k: body.get(k) for k in ("account_id", "site_id", "prefix", "range_from", "range_to")}
    # unscoped: insert_scoped_by_payload
    return (await _rows(supabase.table("dx_barcode_stock").insert({**row, "clinic_id": clinic_id,
                                                                   "issued_by": _actor(user)[0]})))[0]


# ── partner portal reads (every query also filtered by the login's account) ─

PARTNER_VISIT_COLUMNS = ("id, accession_number, status, priority, external_ref, patient_snapshot, billing_mode, "
                         "created_at")


async def partner_visits(clinic_id: str, account_id: str, limit: int = 200) -> list[dict]:
    return await _rows(scoped_query("dx_accessions", clinic_id, PARTNER_VISIT_COLUMNS)
                       .eq("b2b_account_id", account_id).order("created_at", desc=True).limit(limit))


async def partner_visit(clinic_id: str, account_id: str, accession_id: str) -> dict:
    return await _one(scoped_query("dx_accessions", clinic_id, PARTNER_VISIT_COLUMNS + ", b2b_account_id")
                      .eq("b2b_account_id", account_id).eq("id", accession_id), "Visit")


async def partner_releases(clinic_id: str, account_id: str, accession_ids: list[str]) -> list[dict]:
    if not accession_ids:
        return []
    rows: list[dict] = []
    for i in range(0, len(accession_ids), 100):
        rows += await _rows(scoped_query("dx_report_releases", clinic_id, "id, accession_id, release_no, kind, status, "
                                         "created_at").in_("accession_id", accession_ids[i:i + 100]))
    return rows
