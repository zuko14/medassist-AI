# Kriya AI — Admin UI ↔ Backend API Verification Matrix (W9.1, W9.2)

**UI Entrypoint:** `admin/index.html`  
**Backend Router:** `app/routers/admin.py` & `app/routers/clinics.py`

---

## 1. Complete UI Action to Endpoint Mapping

| Section / Tab | UI Action / Trigger | Backend Endpoint | HTTP Method | Auth Role Gating | Error Surface & Empty State Handling |
|---|---|---|---|---|---|
| **Auth & Profile** | Login Form Submit | `GET /admin/profile` | `GET` | All Roles | Red banner error toast; input shakes on 401; 429 countdown timer |
| **Auth & Profile** | Switch Branch | `GET /admin/profile` | `GET` | Staff / Admin | Refetches scoped clinic branch state |
| **Appointments** | Load Appointments List | `GET /admin/appointments` | `GET` | Staff, Admin, Super | Filter tabs: Upcoming/Past/Cancelled; Empty state illustration |
| **Appointments** | Manual Book Appointment | `POST /admin/appointments` | `POST` | Staff, Admin, Super | Form modal; 409 conflict alert if slot taken; 422 validation |
| **Appointments** | Cancel & Refund | `POST /admin/appointments/{id}/cancel` | `POST` | Admin, Super | Confirmation modal; warns non-refundable if outside window |
| **Token Queue** | Check-in Patient | `POST /admin/queue/{id}/check-in` | `POST` | Staff, Admin, Super | Immediate token badge refresh; error toast if already checked in |
| **Token Queue** | Call Next Token | `POST /admin/queue/{id}/call` | `POST` | Staff, Admin, Super | Broadcasts token update; audio notification toggle |
| **Token Queue** | Complete Visit | `POST /admin/queue/{id}/complete` | `POST` | Staff, Admin, Super | Moves to completed section; clears active token display |
| **Diagnostics / Labs** | Upload Lab Report PDF | `POST /admin/lab-reports/upload` | `POST` | Staff, Admin, Super | PDF magic byte check; patient match confirmation preview |
| **Diagnostics / Labs** | Approve Needs Review | `POST /admin/lab-reports/{id}/approve` | `POST` | Admin, Super | Staff override with audit log entry; triggers delivery |
| **Diagnostics / Labs** | Replay Delivery | `POST /admin/lab-reports/{id}/resend` | `POST` | Admin, Super | Resends WhatsApp document; updates delivery status |
| **Doctors & Roster** | Add Doctor / Edit Slots | `POST /admin/doctors`, `PUT /admin/doctors/{id}` | `POST`/`PUT` | Admin, Super | Slot generation visual calendar; conflict validation |
| **Doctors & Roster** | Record Doctor Leave | `POST /admin/doctors/{id}/leaves` | `POST` | Admin, Super | Automatically cancels impacted slots and alerts patients |
| **Financials** | Revenue Dashboard | `GET /admin/financials/stats` | `GET` | Admin, Super (Gated) | Staff hidden; revenue charts; refund logs table |
| **Financials** | Trigger Manual Refund | `POST /admin/payments/{id}/refund` | `POST` | Admin, Super (MFA Gated) | Requires password confirmation; idempotent refund ID |
| **Settings & Clinic** | Update Clinic Profile | `PUT /admin/profile` | `PUT` | Admin, Super | Tenant cache invalidation; instant settings sync |
| **Settings & Clinic** | Payment Gateways | `PUT /admin/settings/payment` | `PUT` | Admin, Super | Razorpay key secret validation; partial mode bounds check |
| **Notifications** | Mark Read / Mark All | `POST /admin/notifications/mark-all-read` | `POST` | All Roles | Scoped to current clinic; badge counter resets to 0 |
| **Audit Logs** | View Staff History | `GET /admin/audit-logs` | `GET` | Super Admin | Paginated tamper-resistant NABH/DPDP staff action logs |

---

## 2. Verified Safety & Error Surfacing Rules (W9.2)
1. **No Silent Swallowed Failures:** Every `fetch()` invocation in `admin/index.html` inspects `res.ok`. On HTTP 4xx/5xx responses, the JSON error detail or status text is explicitly displayed in a floating toast or modal error box.
2. **Permission Guarding:** Front-desk `staff` accounts have destructive tabs (Financials, Plan Settings, Secret Keys, Manual Refunds) completely hidden from navigation and disabled in DOM.
3. **Empty States:** Every table and list container provides a dedicated `<div class="empty-state">` with actionable guidance rather than a blank or broken container.
