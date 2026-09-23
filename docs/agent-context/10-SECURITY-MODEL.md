# 10 - SECURITY MODEL & COMPLIANCE FORENSICS

This document details the threat model, authentication gates, cryptographic boundaries, injection mitigations, and Indian healthcare data regulatory compliance (DPDP Act 2023 & NMC Regulations) in KriyaAI.

---

## 1. THREAT MODEL & DEFENSE PERIMETERS

| Boundary | Primary Threat | Mitigation & Enforcement Mechanism |
| :--- | :--- | :--- |
| **Inbound Webhook (`/webhook`)** | Spoofed WhatsApp messages / Replay attacks | HMAC-SHA256 signature verification (`X-Hub-Signature-256`) via Meta App Secret; atomic deduplication on `processed_messages(message_id, clinic_id)`. |
| **Payment Webhook (`/webhooks/razorpay`)** | Fraudulent payment confirmations | HMAC-SHA256 verification (`X-Razorpay-Signature`) against per-clinic or global secret; state transition idempotency. |
| **Internal Connectors (`/internal/integrations/*`)** | Unauthorized report submission / Cross-tenant injection | `X-Integration-Secret` validation; pinned per-clinic connector secrets; CallMedex 5-minute timestamp replay check. |
| **Admin API (`/admin/*`)** | Broken Object Level Auth (BOLA) / Cross-tenant data leak | Application-level tenant scoping via `TENANT_OWNED_TABLES`; `enforce_clinic_access()`; session token revocation in `admin_sessions`. |
| **Platform Owner API (`/platform/*`)** | Unauthorized multi-tenant administration | HTTP Basic Auth with `OWNER_USERNAME` and `OWNER_PASSWORD`; disabled (HTTP 503) if credentials are unset. |
| **Patient Chat (LLM Input)** | Prompt injection / Jailbreak / Clinical liability | Input sanitization regex (`strip_injection_markers`); zero-LLM deterministic `ClinicalFirewall` screening. |
| **Browser Frontend** | Cross-Site Scripting (XSS) / Clickjacking | Strict CSP blocking external scripts; `X-Frame-Options: DENY`; `X-Content-Type-Options: nosniff`. |
| **Database Connection** | Accidental cross-tenant query exposure | `scoped_query()` helper; `is_valid_clinic_scope()` refusing sentinel wildcards (`default`, `none`, `all`). |

---

## 2. CRYPTOGRAPHIC PRIMITIVES & SECRET MANAGEMENT

### Webhook Signatures
- **Meta WhatsApp**: Verifies payload bytes against `X-Hub-Signature-256` using `hmac.new(secret, body, hashlib.sha256).hexdigest()`. Comparison uses constant-time `hmac.compare_digest()` to eliminate timing attack vectors.
- **Razorpay**: Verifies payment signature using `razorpay.utility.verify_payment_signature()`.
- **CallMedex**: Verifies request payload digest against `X-Signature` header; checks `X-Timestamp` against a 300-second window to prevent replay attacks.

### Credential Encryption at Rest (MocDoc Passwords)
- **Module**: [`app/utils/connector_crypto.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/utils/connector_crypto.py)
- **Algorithm**: Fernet symmetric encryption (AES-128 in CBC mode with HMAC-SHA256 authentication).
- **Key**: Stored in `CONNECTOR_ENCRYPTION_KEY`. A startup pre-flight check validates key integrity before worker boot.

### Password Hashing
- Administrative passwords (`admin_users` table) are hashed using `bcrypt` with salt work factor of 12.

---

## 3. PROMPT INJECTION & CLINICAL LIABILITY MITIGATION

### Prompt Injection Shield (`app/utils/security.py`)
- Strips known injection markers before constructing LLM context:
  - System role hijackers (`system:`, `<|im_start|>`, `[INST]`, `<<SYS>>`)
  - Instruction override phrases: `ignore all instructions`, `disregard prior rules`, `you are now root`
  - Input length truncation: Caps user input strings to 500 characters to prevent buffer overflow or context poisoning attacks.

### NMC Regulatory Medical Guardrails (`app/services/clinical_firewall.py`)
- The National Medical Commission (NMC) Telemedicine Practice Guidelines prohibit autonomous AI diagnosis or prescription.
- `ClinicalFirewall` screens queries for prescription drug names, dosage requests, and treatment advice deterministically.
- Matching messages are **never passed to the LLM**; the system returns an immediate static refusal redirecting the patient to book an OPD consultation with a certified doctor.

---

## 4. REGULATORY COMPLIANCE: INDIA DPDP ACT 2023 & NMC

### DPDP Act 2023 (Digital Personal Data Protection Act)
1. **Consent Tracking**:
   - `patients.consent_given` tracks explicit opt-in.
   - WhatsApp opt-out: Replying `STOP` or `UNSUBSCRIBE` toggles `patients.is_active = False` and suppresses automated outbound notifications.
2. **Data Minimization & Purge Schedulers**:
   - `conversation_purge` job: Daily at 02:00 IST, deletes all WhatsApp interaction records older than **30 days** (`conversations` and `inbound_messages`).
   - `analytics_purge` job: Daily at 03:00 IST, deletes user clickstream analytics older than **90 days**.
3. **Right to Erasure ("DELETE MY DATA")**:
   - Patient sending "DELETE MY DATA" triggers an anonymization pipeline that strips personal identifiers (phone, name, email replaced with `[REDACTED]`).

### NMC 7-Year Clinical Record Retention Mandate
- Per NMC guidelines, clinical records (appointments, medical prescriptions, diagnostic laboratory reports) must be retained for **7 years**.
- When an erasure request is executed, **clinical records are anonymized rather than deleted**, preserving medical history and audit trails for regulatory inquiries while purging personal PII.

---

## 5. HARDENED HTTP SECURITY HEADERS

Injected on every request via `SecurityHeadersMiddleware` in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py):

```http
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

---

## 6. PRODUCTION BOOT SECURITY PRE-FLIGHT

On non-development environments (`settings.app_env != "development"`), [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py#L117-L135) inspects all critical credentials and **refuses to boot (exits with fatal RuntimeError)** if placeholder or default secrets are detected:
- `ADMIN_USERNAME` in `("admin", "administrator", "root", "")`
- `META_APP_SECRET` in `("change_me_in_production", "dev_secret", None)`
- `ADMIN_PASSWORD` or `OWNER_PASSWORD` in `("admin", "password", "123456", "admin123")`
- `INTEGRATION_SECRET` containing `"change_in_prod"`
- `CALLMEDEX_BEARER_TOKEN` in `("dev_bearer_token", "change_in_prod")`
