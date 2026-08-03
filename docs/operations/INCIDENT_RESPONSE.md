# CallMedex Incident Response & Escalation Plan

**Contract Version:** `v1.0.0`

---

## 1. Incident Severity Definitions

- **P1 - CRITICAL**: Total failure of EMR connector or WhatsApp delivery pipeline affecting 100% of jobs.
- **P2 - HIGH**: Intermittent selector failure or EMR portal timeout affecting > 10% of jobs.
- **P3 - MEDIUM**: Report OCR extraction confidence < 0.80 resulting in job escalation.
- **P4 - LOW**: Minor latency spike or isolated retry event.

---

## 2. Step-by-Step Incident Resolution Workflow

1. **Acknowledge & Triage**: Inspect structured logs filtering by `correlation_id` or `report_job_id`.
2. **Isolate Subsystem**:
   - EMR Login/Navigation issue ➔ Inspect `MocDocSelectorProvider` (`v1.py`).
   - OCR Extraction issue ➔ Inspect `CanonicalOCRPipeline` (`engine.py`).
   - AI Summary issue ➔ Inspect `MultiAudienceSummaryGenerator` (`generator.py`).
   - Callback issue ➔ Inspect `CallMedexCallbackHandler` (`handler.py`).
3. **Apply Hotfix / Selector Rollforward**: If EMR UI updated, define `MocDocSelectorProviderV2` in `v2.py` and update `current.py` alias. Zero changes required in business logic!
4. **Post-Mortem & RCA**: Document root cause and add regression test case to `tests/`.
