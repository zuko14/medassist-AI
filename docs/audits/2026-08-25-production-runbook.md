# KRIYA AI / MEDIASSIST AI — PRODUCTION OPERATIONS RUNBOOK

**Target System:** Kriya AI Multi-Tenant Hospital Bot & Admin Platform  
**Operational Status:** Production-Ready  
**Revision:** 1.0 (Post-Remediation)  

---

## 1. Startup & Deployment

### Database Migration
Before starting application instances, apply pending database migrations:
```bash
python scripts/migrate.py
```
To run in dry-run mode:
```bash
python scripts/migrate.py --dry-run
```

### Application Startup
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## 2. Dead-Letter Queue (DLQ) & Message Recovery

### Replaying Dead-Lettered Inbound Messages
When messages fail due to external outages (e.g. Meta 500s or temporary DB locks), `MessageQueueManager.release(message_id)` allows redelivery.
To query unprocessed or failed messages:
```sql
SELECT id, clinic_id, phone, attempts, last_error, created_at 
FROM incoming_messages 
WHERE status = 'dead_letter' 
ORDER BY created_at DESC;
```
To trigger replay of a failed message:
```sql
UPDATE incoming_messages 
SET status = 'pending', attempts = 0 
WHERE id = '<MESSAGE_UUID>';
```

---

## 3. Financial Reconciliation & Stuck Booking Recovery

### Expired Booking Sweep
The background scheduler runs `expire_stale_bookings()` every 60 seconds.
If network errors occur during Razorpay status checks, bookings remain in `pending_payment` until Razorpay connectivity is re-established.

### Manual Refund Invocation
To issue an emergency refund via Admin API:
```bash
curl -X POST https://api.kriya.ai/admin/bookings/<BOOKING_ID>/refund \
  -H "Authorization: Bearer <ADMIN_JWT>" \
  -H "Content-Type: application/json" \
  -d '{"reason": "patient_cancelled"}'
```

---

## 4. Lab Report Delivery & Needs-Review Resolution

### Reviewing Unmatched Reports
When patient matching encounters name/phone conflicts or database errors, reports are held in `needs_review`:
```sql
SELECT id, clinic_id, patient_phone, patient_name, review_reason, created_at 
FROM lab_reports 
WHERE status = 'needs_review';
```

### Resending an Approved Report
```bash
curl -X POST https://api.kriya.ai/admin/lab-reports/<REPORT_ID>/resend \
  -H "Authorization: Bearer <ADMIN_JWT>"
```

---

## 5. Incident Response & Emergency Rollback

### Tenant Boundary Leak Detection
If unauthorized cross-tenant activity is suspected:
1. Check structured audit logs for `[SECURITY] Cross-tenant access attempt rejected`.
2. Invalidate tenant cache:
   ```python
   from app.services.tenant import invalidate_tenant_cache
   await invalidate_tenant_cache(clinic_id, whatsapp_number, phone_number_id)
   ```

### Emergency Database Rollback
If a newly applied migration needs reversion:
```sql
-- Migration 046 Rollback
DROP INDEX IF EXISTS idx_appointments_refund_id;
ALTER TABLE appointments 
DROP COLUMN IF EXISTS refund_id,
DROP COLUMN IF EXISTS refund_reason,
DROP COLUMN IF EXISTS refunded_at;
DELETE FROM schema_migrations WHERE filename = '046_add_refund_columns.sql';
```
