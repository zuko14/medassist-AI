# 09 - BACKGROUND JOBS & ASYNCHRONOUS PROCESSING

This document inventories all scheduled jobs, background tasks, queues, distributed locking, and dead-letter recovery mechanisms in KriyaAI.

---

## 1. ASYNC ARCHITECTURE & TOPOLOGY

KriyaAI utilizes three distinct asynchronous patterns:
1. **FastAPI `BackgroundTasks` / `spawn_background_task`**: Short-lived, out-of-band coroutines running on the main event loop immediately after HTTP response delivery (inbound message parsing, ledger logging, notification dispatch).
2. **APScheduler (`AsyncIOScheduler`)**: In-process cron and interval scheduler running in the web service (Asia/Kolkata timezone), executing 24 registered maintenance and reminder jobs.
3. **Distributed PostgreSQL Job Locks (`scheduler_locks`)**: Multi-instance concurrency barrier preventing duplicate execution across redundant Render instances or cluster workers.

---

## 2. INVENTORY OF ALL 24 SCHEDULED JOBS

All jobs are configured in [`app/services/scheduler.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/scheduler.py):

| Job ID | Frequency / Schedule | Target Function | Purpose & Impact |
| :--- | :--- | :--- | :--- |
| `24h_reminders` | Daily at 09:00 IST | `send_24h_reminders` | Sends WhatsApp appointment reminder templates to patients scheduled for tomorrow. |
| `2h_reminders` | Every hour (`*`) | `send_2h_reminders` | Sends urgent 2-hour reminder to patients with appointments starting in the next 120 minutes. |
| `auto_complete_appointments` | Daily at 00:30 IST | `auto_complete_appointments` | Closes yesterday's uncancelled visits by setting `status='completed'`. Enables follow-up tracking. |
| `followups` | Daily at 10:00 IST | `send_followups` | Sends post-consultation follow-up inquiry to patients completed 1–3 days prior. |
| `health_checkins` | Daily at 10:30 IST | `send_health_checkins` | Sends structured post-discharge recovery check-ins on Day+3 and Day+7. |
| `doctor_leaves` | Daily at 08:00 IST | `check_doctor_leaves` | Scans upcoming doctor leaves and alerts administration to reschedule conflicts. |
| `prescription_reminders` | Every 5 minutes | `send_due_reminders_job` | Queries active prescriptions and delivers scheduled medication dosage reminders. |
| `failed_messages_alert` | Mondays at 09:00 IST | `alert_failed_messages` | Aggregates unrecovered messages from the Dead Letter Queue (`failed_messages`) and alerts admin. |
| `dlq_pending_retry_drain` | Every 5 minutes | `drain_pending_retry_messages` | Replays lock-timeout or transiently dropped messages from `failed_messages`. |
| `failed_lab_reports_alert` | Hourly | `alert_failed_lab_reports` | Scans for reports stuck in `pending_retry` or failed delivery states. |
| `message_queue_fail_open_alert` | Every 10 minutes | `alert_message_queue_fail_open`| Monitors if Redis/DB message queue lock is experiencing high fail-open rates. |
| `rate_limits_cleanup` | Daily at 00:00 IST | `cleanup_rate_limits` | Prunes expired client IP entries from rate limiter memory. |
| `conversation_purge` | Daily at 02:00 IST | `purge_expired_conversations` | DPDP Data Minimization: Permanently purges WhatsApp conversation sessions older than 30 days. |
| `analytics_purge` | Daily at 03:00 IST | `purge_expired_session_data`| Purges raw user clickstream and analytics events older than 90 days. |
| `inbound_messages_purge` | Daily at 03:30 IST | `purge_inbound_messages` | Purges processed entries from `inbound_messages` older than retention window. |
| `catalogue_import_previews_purge` | Daily at 04:00 IST | `purge_expired_catalogue_import_previews` | Deletes uncommitted CSV price-list import staging records. |
| `expire_stale_bookings` | Every 1 minute | `expire_stale_bookings` | Cancels bookings stuck in `pending_payment` for >30 mins; checks Razorpay for missed webhooks. |
| `poll_recent_pending_payments` | Every 30 seconds | `poll_recent_pending_payments`| Fast-polls Razorpay API for payments created in the last 5 minutes to bypass webhook delays. |
| `payment_reconciliation` | Daily at 23:00 IST | `daily_payment_reconciliation`| Reconciles all day's confirmed appointments against Razorpay capture logs; flags mismatches. |
| `lab_report_retry` | Every 5 minutes | `_retry_pending_lab_reports` | Retries failed WhatsApp PDF document dispatches for diagnostic reports. |
| `recover_pending_inbound_messages`| Every 1 minute | `recover_pending_inbound_messages`| Sweeps durable queue for messages left in `received` or `processing` due to container crashes. |
| `reap_abandoned_message_claims` | Every 60 seconds | `reap_abandoned_message_claims`| Reaps stuck worker claim leases on `inbound_messages` where worker heartbeat timed out. |
| `connector_polling` | Every 1 minute | `run_all_connectors` | Evaluates configured external LIS/MocDoc connectors and triggers polling runs. |
| `connector_storage_cleanup` | Daily at 02:00 IST | `cleanup_expired_storage` | Deletes temporary report PDF files stored on disk older than 90 days. |

---

## 3. DISTRIBUTED LOCKING ENGINE (`DistributedJobLock`)

- **File**: [`app/services/distributed_lock.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/distributed_lock.py)
- **Lock Table**: `scheduler_locks(job_name, locked_by, locked_at, expires_at)`

### Mechanics & Invariants
1. **Atomic CAS Acquisition**:
   - Calls Supabase stored procedure `acquire_scheduler_lock(p_job_name, p_locked_by, p_lease_seconds)`.
   - Atomically grants lock if row is unassigned or expired (`expires_at < now()`).
2. **Fail-Closed Principle**:
   - If database connectivity fails during acquisition, the lock is **not granted**. The scheduled job skips execution for that tick, guaranteeing no duplicate sends occur.
3. **Heartbeat & Lease Renewal**:
   - While the job body executes, a background asyncio task renews the lease (`update scheduler_locks set expires_at = ... where locked_by = self.worker_id`).
   - If another instance steals or forcibly resets the lock, `renew()` returns `False` and cancels the running job via `LockStolenError`.

---

## 4. DURABLE MESSAGE QUEUE & RECOVERY (KRIYA-004)

- **File**: [`app/services/message_queue.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/message_queue.py)
- **Table**: `inbound_messages(id, clinic_id, raw_payload, status, worker_id, claim_expires_at)`

### Lifecycle & Failure Recovery
1. **Ingress**: Meta webhook immediately inserts payload with `status = 'received'`.
2. **Claiming**: Worker claims row using `claim_inbound_message()` via CAS update setting `status = 'processing'`, `worker_id`, and `claim_expires_at = now() + 60s`.
3. **Completion**: On successful handling, status becomes `'completed'`.
4. **Crash Recovery**: If a worker crashes or deploys while processing:
   - `reap_abandoned_message_claims` re-opens expired claims (`claim_expires_at < now()`).
   - `recover_pending_inbound_messages` reconstructs the original `WhatsAppMessage` from `raw_payload` and re-dispatches it to `conversation_service`.
5. **Dead-Letter Handling**: After 3 failed processing attempts, the message transitions to `'failed'`, is recorded in `failed_messages`, and alerts clinic operations.
