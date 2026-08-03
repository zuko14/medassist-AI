# Security Model — MediAssist AI & CallMeDex Subsystem

## Overview
This document outlines the security architecture, authentication mechanisms, credential management posture, and data protection controls implemented across MediAssist AI and the CallMeDex integration subsystem.

---

## Authentication & Authorization Architecture

### 1. Machine-to-Machine API Security
Internal API endpoints under `/internal/integrations/callmedex/*` require dual-layer authentication:
- **Bearer Token Header**: `Authorization: Bearer <CALLMEDEX_BEARER_TOKEN>`
- **Shared Integration Secret Header**: `X-Integration-Secret: <CALLMEDEX_INTEGRATION_SECRET>`

Requests failing both header checks are rejected immediately with `HTTP 401 Unauthorized`.

### 2. HMAC-SHA256 Webhook & Request Signing
To guarantee payload integrity and prevent tampering, incoming requests and outgoing status callback webhooks are signed using HMAC-SHA256:
- **Header**: `X-Signature-256: <hex_digest>`
- **Algorithm**: `hmac.new(secret.encode("utf-8"), raw_body_bytes, hashlib.sha256).hexdigest()`
- **Verification**: Evaluated using constant-time comparison (`hmac.compare_digest`) to prevent timing side-channel attacks.

### 3. Replay Protection Window
To prevent replay attacks on machine-to-machine webhooks and process-report requests:
- **Header**: `X-Timestamp: <ISO-8601 string or Unix epoch>`
- **Enforcement**: Requests with a timestamp difference greater than **300 seconds (5 minutes)** relative to host server UTC time are rejected with `HTTP 401 Unauthorized`.

---

## EMR Credentials & Secrets Management

- **Zero Hardcoded Secrets**: No EMR portal credentials, Razorpay keys, or HMAC secrets exist in source code.
- **Pydantic SecretStr Environment Masking**:
  - `CALLMEDEX_MOCDOC_USERNAME`: Portal login username (`SecretStr`)
  - `CALLMEDEX_MOCDOC_PASSWORD`: Portal login password (`SecretStr`)
  - `CALLMEDEX_INTEGRATION_SECRET`: Machine-to-machine API secret (`SecretStr`)
  - `CALLMEDEX_HMAC_SIGNATURE_SECRET`: Webhook signing secret (`SecretStr`)
  - `CALLMEDEX_BEARER_TOKEN`: Bearer token secret (`SecretStr`)
- **Secret Access Control**: Credentials are accessed exclusively via `.get_secret_value()` at point of use and masked in `repr()`, `str()`, and log outputs.

---

## Data Protection, PHI Masking & Storage Security

### 1. Patient Identifiable Information (PHI) Masking
All log output touching patient data enforces strict phone number masking:
- Pattern: `patient=***{patient_phone[-4:]}`
- Full names, MRNs, and unmasked phone numbers are excluded from application logs.

### 2. Temporary PDF Buffer Sanitization
Temporary report downloads buffered by `LocalStorageProvider` undergo path sanitization to prevent directory traversal vectors:
- `_sanitize_part` strips directory separators (`/`, `\`) and `..` sequences via `os.path.basename` and a strict regex allow-list (`[A-Za-z0-9_\-\.]`).
- Downloaded temp files are purged unconditionally in job execution `finally` blocks.

---

## Integration Isolation & Sandboxing

- **Environment-based Transport Bypass**: Real HTTP webhook dispatch is strictly enforced when `CALLMEDEX_APP_ENV=production`. In `development`, `staging`, and `test` environments, transport bypass runs safely in isolated sandbox mode.
- **Headless Browser Containment**: Playwright browser contexts are launched in isolated headless sessions (`headless=True`) with explicit navigation timeouts (`browser_navigation_timeout_ms=60000`). All browser resources (`page`, `context`, `browser`, `playwright`) are closed unconditionally in `close_context` via `try/finally` blocks to guarantee zero process or memory leaks.
