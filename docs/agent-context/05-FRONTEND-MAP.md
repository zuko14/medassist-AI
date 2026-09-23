# 05 - FRONTEND MAP & UI ARCHITECTURE

This document maps the user interfaces served by KriyaAI: the Clinic Administration Panel ([`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html)) and the Platform Owner Console ([`admin/platform.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/platform.html)).

---

## 1. FRONTEND ARCHITECTURE & DESIGN SYSTEM

### Architecture Philosophy
- **Zero Build Step**: The frontend contains no React, Vue, or Webpack/Vite toolchain. Both panels are single-file, self-contained HTML/CSS/JavaScript applications served directly by FastAPI via Starlette's `FileResponse`.
- **Content Security Policy (CSP) Adherence**: Configured in [`app/utils/security.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/utils/security.py) with `script-src 'self' 'unsafe-inline'`. External CDNs (such as cdnjs, unpkg, or jsdelivr) are blocked. Third-party libraries like Chart.js are vendored locally in [`admin/vendor/chart.umd.min.js`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/vendor/chart.umd.min.js) and served via `/panel-assets/chart.umd.min.js` and `/platform-panel/vendor/chart.umd.min.js`.
- **CSS Design Tokens ("Clinical Depth")**:
  - Dark Theme (`:root`): Deep obsidian-navy (`--bg: #040A11`), frosted translucent glass surfaces (`--surface: rgba(28,45,63,0.72)`), subtle emerald mint rims (`--border: rgba(199,234,225,0.13)`), brand gradient (`linear-gradient(135deg, #10B981 0%, #2FA8D8 55%, #3D8BFD 100%)`).
  - Light Theme (`:root[data-theme="light"]`): Crisp daylight clinical white-mint (`--bg: #F2F7F7`, `--surface: rgba(255,255,255,0.78)`).
- **Iconography**: Embedded SVG symbol sprite sheet (`#kriya-mark`, `#i-calendar`, `#i-activity`, `#i-users`, etc.) eliminating external font or icon downloads.

---

## 2. CLINIC ADMIN PANEL (`admin/index.html`)

Served at routes `/admin-panel`, `/hospital-panel`, `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel`, and `/women-child-panel`.

### Authentication & Session State
- State Variables: `authToken` (stored in `localStorage.getItem("admin_token")`), `currentUser` (cached profile from `GET /admin/me`).
- API Request Interceptor (`api(path, options)`):
  - Injects `Authorization: Bearer <token>`.
  - On HTTP 401: Clears `localStorage`, displays login modal, and aborts pending queries.
  - Automatically handles JSON parsing and standard error toast notifications.

### Navigation Pages & Permission Matrix

The sidebar dynamically renders 18 tabs based on user role (`admin`, `staff`, `doctor`, `receptionist`) and clinic plan features (`clinic.plan` / `clinic.config.features`):

| Page ID | Label | Required Role | Required Feature / Plan Flag | Key API Endpoints | Primary Components & Functionality |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `dashboard` | Overview | All | None | `GET /admin/stats`, `GET /admin/appointments/upcoming` | Live KPI cards (Today's Bookings, Completed, Pending, Revenue), quick action buttons, upcoming patient stream. |
| `insights` | Analytics | `admin` | None | `GET /admin/insights`, `GET /admin/insights/summary`, `POST /admin/insights/summary/generate` | Chart.js visual trends (7d/30d/90d), peak booking hours histogram, cancellation analysis, AI-generated executive summary. |
| `profile` | Clinic Profile | `admin` | None | `GET /admin/profile`, `PUT /admin/profile` | Edit hospital name, phone, address, operating hours, consultation fees, and UPI payment identifier. |
| `appointments` | Appointments | All | `booking` or `lab_test_booking` | `GET /admin/appointments`, `POST /admin/appointments/{id}/check-in`, `DELETE /admin/appointments/{id}` | Date-picker filterable appointments data grid, status pills (`confirmed`, `checked_in`, `completed`, `cancelled`), cancel with reason dialog. |
| `doctors` | Doctors & Slots | All | `booking` | `GET /admin/doctors`, `POST /admin/doctors`, `PUT /admin/doctors/{id}`, `DELETE /admin/doctors/{id}` | Doctor roster cards, consultation fee editor, schedule slot manager (morning/evening blocks), branch association picker. |
| `leaves` | Doctor Leaves | `admin` | `roster_management` | `GET /admin/leaves`, `POST /admin/leaves`, `DELETE /admin/leaves/{id}` | Calendar view of planned doctor leaves; blocks slot booking on WhatsApp during leave days. |
| `holidays` | Clinic Holidays | `admin` | `holiday_calendar` | `GET /admin/holidays`, `POST /admin/holidays`, `DELETE /admin/holidays/{date}` | Hospital-wide full-day closure dates; disables bot booking across all departments. |
| `patients` | Patient Directory | All | `booking` or `lab_test_booking` | `GET /admin/patients` | Searchable patient registry, appointment history drawer, WhatsApp re-engagement trigger. |
| `diagreports` | Diagnostic Queue | All | `diagnostic_reports` | `GET /admin/reports/queue`, `POST /admin/reports/{id}/resolve-match`, `POST /admin/reports/{id}/dismiss` | Unmatched/low-confidence connector report resolution workbench. Allows staff to match phone numbers to walk-in patients. |
| `labreports` | Lab Reports | All | `lab_reports` | `GET /admin/lab-reports`, `POST /admin/lab-reports/upload`, `POST /admin/lab-reports/{id}/resend` | Direct staff PDF upload modal, AI clinical summary viewer, delivery status pills (`sent`, `delivered`, `read`, `failed`). |
| `labtests` | Test Catalogue | All | `lab_test_booking` | `GET /admin/lab-tests`, `POST /admin/lab-tests`, `POST /admin/lab-tests/import-csv`, `GET /admin/lab-collection-window` | Test catalogue pricing, home sample collection operating window configuration, CSV import wizard with diff preview. |
| `treatments` | Procedures | All | Specialty plans (`derma`, `dental`, `eye`, `ivf`, `multispecialty`) | `GET /admin/treatments`, `POST /admin/treatments`, `POST /admin/treatments/ai-description` | Aesthetic and clinical procedure menu (e.g., HydraFacial, Root Canal, LASIK), doctor assignment, AI description generator. |
| `prescriptions` | Prescriptions | `admin` | `booking` | `GET /admin/prescriptions`, `POST /admin/prescriptions`, `POST /admin/prescriptions/{id}/deactivate` | Digital prescription repository and active medication tracking. |
| `payments` | Payments | `admin` | `booking` or `lab_test_booking` | `GET /admin/bookings`, `GET /admin/payments/reconciliation`, `POST /admin/bookings/{id}/refund` | Transaction log, Razorpay payment ID cross-check, partial-deposit tracking, instant refund initiator. |
| `paysettings` | Gateway Config | `admin` | `payments_razorpay` | `GET /admin/settings/payment`, `PUT /admin/settings/payment` | Razorpay Key ID and Key Secret configuration, payment mode toggle (`full`, `partial`, `pay_at_clinic`). |
| `branches` | Locations | `admin` | `multi_branch` | `GET /admin/branches`, `POST /admin/branches`, `PUT /admin/branches/{id}`, `GET /admin/branches/{id}/doctors` | Multi-facility setup: manage clinic branches, addresses, geo-coordinates, and branch-specific doctor assignments. |
| `staff` | Staff & Roles | `admin` | None | `GET /admin/staff`, `POST /admin/staff`, `PUT /admin/staff/{id}/toggle`, `DELETE /admin/staff/{id}` | Administrative user provisioning, role selection (`admin`, `staff`, `doctor`, `receptionist`), branch scoping. |
| `connectors` | Integrations | All | `diagnostic_reports` | `GET /admin/connectors`, `PUT /admin/connectors`, `POST /admin/connectors/{id}/test`, `GET /admin/connectors/failed-reports` | Laboratory Information System (LIS) / MocDoc connector setup, sync schedule controls, failure dead-letter monitor. |

---

## 3. PLATFORM OWNER CONSOLE (`admin/platform.html`)

Served at route `/platform-panel`.

### Authentication
- Protected by `verify_owner_credentials` via HTTP Basic Auth (`OWNER_USERNAME` / `OWNER_PASSWORD`).
- Credentials are held in memory during the browser session.

### Core Management Views
1. **Headline Metric Tiles**:
   - Total Hospitals, Active vs. Inactive count, Month-to-date Onboarded Clinics.
   - Clinic GMV (30-day cumulative patient transactions collected via Razorpay).
2. **Platform Financial Control Centre**:
   - Real-time gross margin and net platform revenue computation.
   - External provider cost breakdown: Meta WhatsApp Cloud API fees, OpenRouter / Groq AI token costs, Supabase database and storage hosting.
   - Manual infrastructure expense logger (`POST /platform/finance/expenses`).
   - Automated monthly PDF invoice generator (`POST /platform/finance/invoices/generate`).
3. **Multi-Tenant Leaderboard & Directory**:
   - Comprehensive table of all clinics showing WhatsApp phone number, plan tier, monthly appointment count, message usage, and active status.
   - Filterable by tier (`soloclinic`, `diagstream`, `diagbooking`, `polyclinic`, `enterprise`, `derma`, `eye`, `dental`, `ivf`, `multispecialty`, `womenchild`).
4. **Tenant Feature & AI Budget Manager**:
   - Live toggles for granular feature flags (`booking`, `diagnostic_reports`, `payments_razorpay`, `multi_branch`).
   - Soft/hard monthly AI dollar spending cap configuration (`PATCH /platform/clinics/{id}/ai-budget`).
5. **Subscription & Lifecycle Management**:
   - Subscription expiration monitor with manual renewal extension (`POST /platform/clinics/{id}/renew`).
   - Tenant Deletion Wizard with 2-step verification and pre-flight impact count (`GET /platform/clinics/{id}/deletion-preview`, `DELETE /platform/clinics/{id}`).
6. **Cross-Tenant System Broadcast**:
   - Tool to dispatch administrative alerts or system maintenance notices to clinic administrative contacts via WhatsApp.

---

## 4. FRONTEND-BACKEND MISMATCHES & KNOWN BEHAVIORS

1. **Dead `admin/admin.js` File**:
   - An orphaned file [`admin/admin.js`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/admin.js) exists in the repository. As documented in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py#L446-L452), this file is **not loaded** by `admin/index.html` (all JS logic is inlined in `index.html`). Modifications to `admin/admin.js` have zero effect on the application.
2. **Chart.js Fallback Handling**:
   - If `/panel-assets/chart.umd.min.js` fails to load, `renderInsights()` gracefully displays a fallback message without crashing dashboard navigation.
3. **Branch Scoping Realities**:
   - If an authenticated user is assigned a `branch_id`, the UI automatically locks the branch selector dropdown to that specific branch.
