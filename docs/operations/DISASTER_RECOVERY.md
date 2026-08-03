# CallMedex Subsystem Disaster Recovery Runbook

**Contract Version:** `v1.0.0`  
**RTO (Recovery Time Objective):** < 15 minutes  
**RPO (Recovery Point Objective):** 0 lost transactions (Resumable Job Checkpoints)

---

## 1. Disaster Recovery & Checkpoint Resumption

The CallMedex worker tracks execution using 7 atomic `JobCheckpoint` states:
1. `CHECKPOINT_1_CREATED`
2. `CHECKPOINT_2_AUTHENTICATED`
3. `CHECKPOINT_3_BARCODE_LOCATED`
4. `CHECKPOINT_4_REPORT_LOCATED`
5. `CHECKPOINT_5_PDF_DOWNLOADED`
6. `CHECKPOINT_6_VALIDATED`
7. `CHECKPOINT_7_CALLBACK_SENT`

If a node crash, network drop, or container failover occurs:
- The worker automatically inspects the last persisted `JobCheckpoint`.
- It resumes execution directly from that checkpoint without repeating prior completed stages (e.g. avoiding redundant EMR re-logins if already authenticated).

---

## 2. Emergency Isolation & Shutdown

If an EMR portal undergoes unplanned maintenance or structural DOM changes:
1. Disable CallMedex integration feature flag:
```env
CALLMEDEX_ENABLED=false
```
2. CallMedex incoming requests will return `503 Service Unavailable` with typed error message while queue items remain safely buffered.
