# Lab Pipeline — End-to-End Production Hardening Plan

Source: forensic audit 2026-08-22 (verdict 🔴 Critical Gaps, weighted 68%).
Target: unattended production operation + admin-visible delivery proof and liveness.

Sequenced so each phase is independently shippable and independently revertable.
Ship in order — later phases depend on schema from earlier ones.

---

## PHASE 0 — Pre-flight (start NOW, blocks Phase 2)

Long-lead items with external dependencies. Nothing here is code.

| # | Action | Owner | Blocks |
|---|---|---|---|
| 0.1 | Submit Meta **Utility template** `lab_report_ready` with a DOCUMENT header + 2 body vars (patient name, report name). Approval takes 1–24h. | Ops | Phase 2 |
| 0.2 | Confirm Supabase Storage bucket `lab-reports` is **private** (dashboard → Storage → lab-reports → Public = OFF). Not assertable from code. | Ops | — |
| 0.3 | Confirm `CONNECTOR_ENCRYPTION_KEY` is set in BOTH Render services (web + worker). Absent key = connector saves fail closed. | Ops | — |
| 0.4 | Record a baseline: current pending-print row count at the busiest branch. If >90, Phase 5 pagination becomes P0 not P2. | Ops | Phase 5 priority |

---

## PHASE 1 — Patient-safety defects (P0, ship first, same PR)

These three can each deliver the wrong data to a real patient. No new schema.

### 1.1 Reject non-PDF at the ingestion choke point

**File:** `app/routers/integrations.py` (~line 111)

Every intake path — admin upload, MocDoc connector, future connectors — routes
through this one endpoint. One guard covers all callers.

```python
    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # A MocDoc session timeout / error page downloads as a valid-looking .pdf.
    # Without this it is summarised to "", sent to the patient as their report,
    # and recorded delivered — never retried. Reject so the connector records a
    # failure and retries next poll.
    if not file_bytes.startswith(b"%PDF"):
        logger.error(
            f"INVALID_PDF for {external_report_id}: missing %PDF header "
            f"({len(file_bytes)} bytes, starts {file_bytes[:16]!r})"
        )
        raise HTTPException(
            status_code=400,
            detail="Downloaded file is not a valid PDF (missing %PDF header)",
        )
```

Also make the silent swallow loud — `app/utils/pdf_reader.py`:

```python
    except Exception as e:
        logger.warning(f"PDF text extraction failed ({type(e).__name__}: {e})")
        return ""
```

Keep the `""` return: by this point the bytes are a verified PDF, so a text
failure is a scanned/image report where the fallback message is correct.

### 1.2 Scope report parsing to the expanded row (PHI cross-delivery)

**File:** `connectors/mocdoc/worker.py` lines 862–901

`inner_text("body")` matches the FIRST `No:` anywhere on the page. If a prior
row failed to collapse (`_click_hide` is best-effort inside `except: pass`), we
read patient A's `report_no`, locate patient A's row, download patient A's PDF —
while `meta` still carries patient B's name and phone.

```python
        # Scope every read to the row we just expanded — see PHI cross-delivery
        # defect. Never read inner_text("body") here.
        expanded_row = target_row.locator(
            "xpath=following-sibling::tr[contains(@class,'showorders')][1]"
        )
        try:
            await expanded_row.wait_for(state="attached", timeout=10000)
            row_text = await expanded_row.inner_text()
        except Exception as e:
            logger.error(f"EXPANDED_ROW_NOT_FOUND for {vam_id}: {e}")
            await self._click_hide(target_row)
            return None

        test_details = _parse_test_details(row_text)
        report_no = test_details.get("report_no")
        full_id = f"{vam_id}_{report_no}" if report_no else vam_id

        meta.external_report_id = full_id
        meta.report_no = report_no
        meta.sample_id = test_details.get("sample_id")

        if full_id in self._processed_ids:
            logger.info(f"Already processed this run: {full_id}")
            await self._click_hide(target_row)
            return None

        test_name_match = re.search(
            r"([A-Z][A-Z\s\-\d]+(?:\d+P)?)\s+No:\s*\d+", row_text
        )
        meta.report_name = (
            test_name_match.group(1).strip() if test_name_match else "Lab Report"
        )
```

Then delete the `tr.showorders:has-text(...)` re-lookup at old line 901 and take
`a.downloadresult` from `expanded_row` directly.

### 1.3 True E.164 normalization at the parse boundary

**File:** `connectors/mocdoc/worker.py` lines 51–57

Blindly prepending `+` turns `9876543210` into `+9876543210`, which
`validate_phone` accepts (strips `+`, counts 10 digits) — so it passes the match
gate and goes to Meta as the recipient.

```python
    phone_match = re.search(r"Mobile:\s*(\+?\d{10,15})", cell_text)
    phone = phone_match.group(1) if phone_match else None
    if phone:
        from app.utils.validators import normalize_phone
        phone = normalize_phone(phone.lstrip("+"))
```

**File:** `connectors/runner.py` after the match gate (~line 449) — the gate
already computes the canonical form; stop discarding it:

```python
                if match_result.normalized_phone:
                    meta.patient_phone = match_result.normalized_phone
```

### Phase 1 checks

`tests/test_mocdoc_worker.py`:

```python
def test_parse_test_details_scoped_to_single_row():
    from connectors.mocdoc.worker import _parse_test_details
    assert _parse_test_details(
        "COMPLETE BLOOD COUNT - 3P No: 22222 SampleID: 260700002222"
    )["report_no"] == "22222"

def test_bare_ten_digit_mobile_gets_country_code():
    from connectors.mocdoc.worker import _parse_patient_cell
    assert _parse_patient_cell(
        "Mr.Ramesh\nID: VAM-40011 Mobile: 9876543210"
    )["phone"] == "+919876543210"
```

`tests/test_integrations_pdf_guard.py`: POST a non-PDF body → expect 400, and
assert no `lab_reports` row was written.

---

## PHASE 2 — Delivery guarantee (P0, needs 0.1 approved)

Today a walk-in patient scraped from MocDoc who has never messaged the bot
**never receives their report**: `_can_send_freeform` returns `True` when no
conversation row exists, the freeform send fires, Meta rejects 131047.

### 2.1 Close the fail-open

**File:** `app/services/whatsapp.py` ~line 478

```python
            conv = await get_conversation(clinic["id"], phone)
            if not conv:
                # Never messaged us => no customer-service window was ever
                # opened. Meta rejects freeform here (131047). Returning True
                # is why MocDoc walk-ins never received reports.
                return False
            expires_at = conv.get("session_expires_at")
            if not expires_at:
                return False
```

The `except` at the bottom stays fail-open — a transient DB error should not
block a report to a patient who IS inside the window.

### 2.2 Template fallback in the report path

**File:** `app/services/lab_reports.py`, wrapping the send block (lines 100–152)

Branch on the window BEFORE sending. Template path: upload media → send template
with document header. Freeform path: existing code unchanged.

```python
            if not await whatsapp_service._can_send_freeform(clinic, patient_phone):
                template = settings.lab_report_template_name
                if not template:
                    raise ValueError(
                        "Outside 24h window and LAB_REPORT_TEMPLATE_NAME unset — "
                        "cannot deliver to this patient"
                    )
                media_id = await whatsapp_service.upload_media(
                    clinic, file_bytes, filename, content_type
                )
                if not media_id:
                    raise ValueError("Failed to upload media to WhatsApp")
                sent_ok = await whatsapp_service.send_template(
                    clinic, patient_phone, template_name=template,
                    components=[
                        {"type": "header", "parameters": [{
                            "type": "document",
                            "document": {"id": media_id, "filename": filename},
                        }]},
                        {"type": "body", "parameters": [
                            {"type": "text", "text": patient_name},
                            {"type": "text", "text": report_name},
                        ]},
                    ],
                    _source="lab_reports", _capture=capture,
                )
                if not sent_ok:
                    raise ValueError("WhatsApp rejected the utility template send")
            else:
                ...  # existing freeform path
```

Add `lab_report_template_name: str = ""` to `app/config.py`.
Apply the same branch in `resend_report` — a manual resend hits the same wall.

Until 0.1 is approved this converts a **silent misdelivery into a loud failure**
that appears in the Phase 3 dashboard. That is already an improvement.

### 2.3 Retry with backoff + 429 handling

**File:** `app/services/whatsapp.py` `_make_request` (~lines 100–119)

Current: 2 attempts, no delay, no 429 handling — an immediate retry against a
rate-limited endpoint compounds it.

```python
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt == 2:
                        resp.raise_for_status()
                    delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(
                        f"Meta {resp.status_code}, retrying in {delay}s "
                        f"(attempt {attempt + 1}/3)"
                    )
                    await asyncio.sleep(min(delay, 30))
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.RequestError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
```

4xx other than 429 must NOT retry — they are permanent (bad number, bad
template) and retrying burns the Meta quota.

### 2.4 Per-phone lock on outbound delivery

`app/services/message_queue.py` locks only the INBOUND webhook path. A connector
delivering a report while the patient is mid-conversation interleaves messages.

**File:** `app/services/lab_reports.py`, wrap the whole send block:

```python
        from app.services.message_queue import acquire_phone_lock_with_timeout
        async with acquire_phone_lock_with_timeout(patient_phone):
            ...  # media upload + text/template + document
```

Reuse the existing helper — do not write a second locking mechanism.

---

## PHASE 3 — Delivery confirmation + Admin panel (the user-facing ask)

Two things the admin must see: **did this specific patient actually get it**, and
**is the automation alive right now**.

### 3.1 Schema

`migrations/0XX_lab_report_delivery_receipts.sql`

```sql
-- Meta delivery receipts, per report. The outbound_message_ledger is
-- append-only by design (migration 032), so receipts land here instead.
ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS whatsapp_message_id  TEXT,
    ADD COLUMN IF NOT EXISTS delivery_status      TEXT,   -- sent|delivered|read|failed
    ADD COLUMN IF NOT EXISTS delivery_error       TEXT,
    ADD COLUMN IF NOT EXISTS delivery_updated_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_lab_reports_wamid
    ON lab_reports(whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL;
```

`status` keeps its current meaning (did OUR send call succeed).
`delivery_status` is the new truth (what Meta actually did with it).

### 3.2 Capture the wamid

`send_text` / `send_document` / `send_template` return `bool` and throw the wamid
away. Smallest non-breaking change — an optional out-dict on the two methods the
report path uses:

**File:** `app/services/whatsapp.py`

```python
    async def send_document(
        self, clinic, phone, media_id, filename, caption="",
        _source="conversation", _capture: Optional[dict] = None,
    ) -> bool:
        ...
            meta_msg_id = self._extract_meta_message_id(result)
            if _capture is not None:
                _capture["meta_message_id"] = meta_msg_id
```

Same two lines in `send_template`. No call site changes.

**File:** `app/services/lab_reports.py` — `capture = {}` before the send,
then in the row dict:

```python
            "whatsapp_message_id": capture.get("meta_message_id"),
            "delivery_status": "sent" if sent_ok else "failed",
```

### 3.3 Process Meta `statuses` callbacks

`app/models/message.py:43` declares `statuses` and **nothing ever reads it**.
Meta's async `failed` callbacks (131047, 131026) are discarded today.

**File:** `app/routers/webhook.py`, inside the `for change in ...` loop:

```python
                if change.value.statuses:
                    for status in change.value.statuses:
                        background_tasks.add_task(record_delivery_status, status)
```

```python
async def record_delivery_status(status: dict) -> None:
    """Persist a Meta delivery receipt (sent/delivered/read/failed).

    Without this a report reads status='sent' while Meta already reported it
    undeliverable — invisible to staff.
    """
    wamid, state = status.get("id"), status.get("status")
    if not wamid or not state:
        return
    err = (status.get("errors") or [{}])[0]
    try:
        supabase.table("lab_reports").update({
            "delivery_status": state,
            "delivery_error": err.get("title"),
            "delivery_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("whatsapp_message_id", wamid).execute()
        if state == "failed":
            logger.error(f"Meta delivery FAILED {wamid}: {err}")
    except Exception as e:
        logger.warning(f"Could not record delivery status for {wamid}: {e}")
```

Never let a receipt failure 500 the webhook — Meta retries and would loop.

### 3.4 Admin API — delivery log

**File:** `app/routers/admin.py`, new endpoint beside `GET /lab-reports`:

`GET /admin/lab-reports/deliveries?clinic_id=&branch_id=&days=7&state=all|delivered|failed|pending`

Returns per report, newest first:

```json
{
  "id": "...", "patient_name": "Ramesh K", "patient_phone": "+91XXXXXX3210",
  "report_name": "COMPLETE BLOOD COUNT", "source": "mocdoc",
  "external_report_id": "VAM-40011_22222",
  "sent_at": "2026-08-22T09:02:11Z", "delivery_status": "delivered",
  "delivery_updated_at": "2026-08-22T09:02:14Z", "delivery_error": null,
  "match_confidence": 0.94, "match_source": "phone_exact"
}
```

Rules:

- Phone masked via `mask_phone()` in the list. Full number only on the detail
  row for `REPORTS_VIEW` holders, and that view writes an `admin_audit_logs`
  entry — PHI access must be attributable (DPDP).
- Derive a single `state` for the UI badge:
  `read`/`delivered` → ✅ Delivered · `sent` → 🕓 Sent, awaiting receipt ·
  `failed` (either column) → ❌ Failed + reason · `needs_review` → 🟠 Unmatched.
- `sent` with `delivery_updated_at` older than 30 min → ⚠️ *No receipt* — that
  is the silent-failure signal the audit found had no surface at all.

### 3.5 Admin API — live/active status

**File:** `app/routers/admin.py`, fix `health` in `GET /diagnostic/stats` (~3455).
Current logic is `is_enabled AND not last_error` — it reports "healthy" for a
connector whose worker process died hours ago.

```python
            poll_minutes = (c.get("config") or {}).get("poll_interval_minutes", 10)
            stale_after = timedelta(minutes=poll_minutes * 3)
            age = None
            if last_run_at:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(
                    last_run_at.replace("Z", "+00:00")
                )
            if not is_enabled:
                health = "disabled"          # grey  — OFF
            elif age is None:
                health = "never_run"         # grey  — NEVER RUN
            elif age > stale_after:
                health = "stalled"           # red   — NOT RUNNING (worker dead)
            elif last_error:
                health = "degraded"          # amber — RUNNING WITH ERRORS
            else:
                health = "active"            # green — ACTIVE
```

Add to the payload: `seconds_since_last_run`, `is_running_now` (from the
existing `_connector_tasks` / DB lock lease), `reports_today`, and
`consecutive_failures`. `stalled` is the case that has never been detectable.

### 3.6 Admin UI — `admin/index.html`

Diagnostic Center page, two additions:

**(a) Automation status strip** — top of page, always visible:

```
🟢 MocDoc Automation ACTIVE   Last run 2 min ago · Next ~8 min · 14 reports today · 0 errors
🔴 MocDoc Automation NOT RUNNING   Last run 3 h 12 min ago — worker may be down  [Run Now]
```

Poll `/diagnostic/stats` every 30s. Colour driven solely by `health` above.
When `is_running_now`, show a pulsing "⟳ Running now…".

**(b) Delivery Log table** — the "did the patient actually get it" answer:

| Time | Patient | Phone | Report | Source | Delivery | Action |
|---|---|---|---|---|---|---|
| 14:32 | Ramesh K | +91XXXXXX3210 | CBC | 🤖 Auto | ✅ Delivered 14:32 | — |
| 14:31 | Sita D | +91XXXXXX7781 | LFT | 🤖 Auto | 🕓 Sent, no receipt | Resend |
| 14:28 | (unmatched) | +91XXXXXX2201 | TFT | 🤖 Auto | 🟠 Needs review | Resolve |
| 13:04 | Anil M | +91XXXXXX9930 | CBC | 👤 Manual | ❌ Failed — 131047 outside window | Resend |

Filter chips: All · Delivered · Failed · Awaiting receipt · Unmatched.
Default filter **Failed + Awaiting receipt** — the admin should land on what
needs action, not on a wall of successes.
Reuse the existing `resend` and `resolve-match` handlers; both are already wired.

---

## PHASE 4 — Honest accounting (P1)

### 4.1 Stop counting unmatched as failed

`connectors/runner.py` ~line 452 increments `reports_failed` for a
`needs_review` match result. That flips `run_status` to failed/partial, fires the
"⚠️ Connector Alert" WhatsApp, and paints the audit log red — for a run where
every download succeeded.

Add real counters to the summary dict and to `connector_audit_log`:

```sql
ALTER TABLE connector_audit_log
    ADD COLUMN IF NOT EXISTS reports_matched       INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reports_needs_review  INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reports_delivered     INTEGER DEFAULT 0;
```

`reports_failed` = downloads/uploads that actually errored, nothing else.
Only `reports_failed > 0` triggers the alert. Surface all six counts in the
admin audit-log table — this is the audit's requested
"Found / Processed / Matched vs Unmatched / Delivered vs Failed" line.

### 4.2 Make `poll_interval_minutes` real

`runner.py:732` hardcodes `IntervalTrigger(minutes=10)`; the admin-configurable
value is written and never read — a dead control on a live panel.

```python
    interval = int((config or {}).get("poll_interval_minutes") or 10)
    scheduler.add_job(..., IntervalTrigger(minutes=max(1, interval)), ...)
```

### 4.3 Explicit scheduler timezone

`app/services/scheduler.py` — ~13 `CronTrigger(hour=...)` with no `timezone=`,
relying entirely on container `TZ`. That is exactly how the 5.5h shift happened.

```python
IST = ZoneInfo("Asia/Kolkata")
scheduler = AsyncIOScheduler(timezone=IST)
```

Set it once on the scheduler; every trigger inherits it.

---

## PHASE 5 — Scale + runtime (P1/P2)

### 5.1 Pagination (`NEXT_PAGE_BUTTON` exists, referenced nowhere)

More than 100 pending-print rows are silently dropped. Two steps:

1. **Now (1 line):** log loud when at the cap —
   `logger.error(f"PAGINATION_TRUNCATED: {row_count} rows at 100-entry cap")`
   and surface it as `last_error` so the Phase 3 badge turns amber.
2. **Then:** loop `NEXT_PAGE_BUTTON` until it is disabled, accumulating rows,
   with a hard cap of 20 pages to bound a runaway.

Priority set by baseline 0.4.

### 5.2 Skip already-processed rows before downloading

`fetch_new_reports` docstring claims a cache check; there is none. MocDoc keeps
reports in Pending Print until staff print them, so every report is
re-downloaded through the full browser flow every 10 min — ~144 wasted cycles
per report per day — and only rejected at submit time.

Pre-load processed IDs once per run and skip at the row-parse stage:

```python
        processed = {
            r["external_report_id"] for r in supabase.table(
                "integration_processed_reports"
            ).select("external_report_id").eq("clinic_id", clinic_id)
             .eq("connector_type", "mocdoc").execute().data or []
        }
```

VAM ID is known pre-expansion; `report_no` is not. So skip on `vam_id` prefix
match, and keep the existing post-download dedup as the exact check.

### 5.3 Get connector runs off the web event loop

`admin.py:3046/3119` `asyncio.ensure_future(...)` runs the connector **inside the
web process serving Meta's webhooks**, and the run is full of synchronous
blocking calls on the loop: every `supabase...execute()`, the storage upload,
`extract_text_from_pdf`, and `groq_client.chat.completions.create` (sync SDK,
15s timeout, not awaited). Meta requires 200 OK within 20s — the exact deadline
`message_queue.py` was designed around, defeated by this.

- **Stopgap (this phase):** wrap the blocking calls in `asyncio.to_thread(...)`.
  Removes the stalls without restructuring.
- **Proper (next):** `/test` and `/run-now` enqueue a job row; the existing
  `mediassist-connector-worker` service claims it. The lock table already gives
  the claim primitive.

### 5.4 Atomic lock acquisition

`runner.py:100–140` is read-then-write TOCTOU. Make the UPDATE conditional and
check the returned rows:

```python
    res = supabase.table("integration_connectors").update(
        {"locked_at": now, "locked_by": worker_id}
    ).eq("id", connector_id).is_("locked_at", "null").execute()
    acquired = bool(res.data)
```

Keep the existing expired-lease path as a second conditional update on
`locked_at < now - lease`.

---

## PHASE 6 — Booking flow completion (P1, product decision needed)

Audit found these **absent, not broken**. Confirm scope before building —
migration 039 documents some as deliberate.

| Gap | Fix |
|---|---|
| No home-collection vs walk-in choice | New state after test select; `collection_mode` on `appointments` |
| No address/landmark capture | Required when `collection_mode='home'` — a phlebotomist cannot be dispatched without it |
| No age/gender intake | Add to the intake state; many panels need age/sex for reference ranges |
| No time slot | `appointment_time` is nullable per migration 039; add slot selection bounded by the branch collection window |
| Collection window shown but never enforced | Validate the chosen slot against `format_collection_window`'s source |
| Test list capped at `tests[:10]`, silent | Paginate with a "More tests" button; reuse the existing pagination helper from docs/05 |
| `_next_collection_dates` uses UTC | `datetime.now(IST).date()` — between 00:00–05:30 IST "tomorrow" resolves to today |
| "15 minutes" vs `booking_hold_minutes = 10` | `conversation.py:3639` → `{settings.booking_hold_minutes}` — patients lose the slot 5 min before they are told |

The last two are one-liners; ship them with Phase 1 regardless of the product
decision on the rest.

---

## Test plan

Per-phase, added to the existing 33-test suite (all currently green):

- **P1:** non-PDF → 400 + no `lab_reports` row; scoped parse returns own row's
  `report_no`; 10-digit → `+91...`.
- **P2:** no conversation row → `_can_send_freeform` False; outside window →
  `send_template` called, `send_text` NOT called; 429 with `Retry-After: 5`
  sleeps then retries; 400 does not retry.
- **P3:** `statuses` payload with `failed` updates the matching row by wamid;
  unknown wamid is a no-op, not a 500; `deliveries` masks phone.
- **P4:** run with 1 download error + 2 unmatched → `reports_failed == 1`,
  `reports_needs_review == 2`, one alert.
- **P5:** conditional-update lock — two concurrent acquires, exactly one wins.

Manual pre-launch: one real MocDoc report end-to-end to a test handset, verified
✅ Delivered in the panel; then disable the worker and confirm the badge flips to
🔴 NOT RUNNING within 3 poll intervals.

---

## Ship order

```
Phase 0  ──(0.1 in parallel, external)──────────────┐
Phase 1  ── P0 patient safety ── deploy ── verify   │
Phase 2  ── delivery guarantee ─ deploy ── verify ←─┘ (needs 0.1)
Phase 3  ── receipts + admin panel ── deploy ── verify
Phase 4  ── accounting ─────────────── deploy
Phase 5  ── scale + runtime ────────── deploy
Phase 6  ── booking UX ── after product sign-off
```

Phases 1–3 take the pipeline from "delivers wrong/no reports invisibly" to
"delivers correctly, and the admin can prove it per patient". That is the
minimum for unattended production. 4–6 are correctness and scale.
