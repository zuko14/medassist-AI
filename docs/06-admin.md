# Phase 17: Admin Panel Full UI Spec


## 🖥️ Phase 17 — Admin Panel (Full UI Spec)

> Built as a password-protected web app at `/admin`. Pure HTML + Tailwind CSS + your existing FastAPI routes.
> Hospital staff uses this daily — no Supabase dashboard access needed.
> Mobile-friendly. Works on any browser.

---

### 17.1 Tech Stack for Admin Panel

```
Frontend : Single HTML file — Tailwind CSS + vanilla JS (no React, no build step)
Backend  : Existing FastAPI admin routes (already specced in Phase 8)
Auth     : HTTP Basic Auth via FastAPI dependency — username + password per hospital
Hosting  : Same Render instance as the bot — zero extra cost
URL      : https://your-render-app.onrender.com/admin
```

No separate deployment. No extra server. One file.

---

### 17.2 Authentication

**`app/routers/admin.py`** — protect every route:

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, settings.admin_username)
    correct_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (correct_user and correct_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username
```

Add to `.env`:
```
ADMIN_USERNAME=citycareAdmin
ADMIN_PASSWORD=choose-a-strong-password-here
```

Each hospital client gets their own username + password. You change it in `.env` and redeploy.

---

### 17.3 Admin Panel Pages — Full Spec

#### Page 1: `/admin` → Dashboard

**Purpose**: Front desk and manager see today at a glance the moment they open the panel.

**Stat cards (top row)**:
- Today's Bookings (count, delta from yesterday)
- Pending Today (appointments not yet arrived)
- No-Shows (status = no_show today)
- Conversion Rate (bookings / total WhatsApp conversations today × 100)
- Total Patients (all-time registered count)
- Avg Rating (running average from feedback table)

**Weekly bar chart**: 7 bars Mon–Sun, today's bar highlighted in teal. Show booking count per day.

**Top departments**: Horizontal progress bars showing booking count per dept (current month).

**Today's appointment list**: Last 10 today, columns: Ref, Patient, Doctor, Time, Status. Clickable rows open appointment detail.

**API calls on load**:
```
GET /admin/stats/daily
GET /admin/stats/departments
GET /admin/appointments?date=today&limit=10
```

---

#### Page 2: `/admin/doctors` → Doctors

**Purpose**: Manager adds, edits, activates/deactivates doctors.

**Header**: Total active count, total inactive count. "Add Doctor" button top right.

**Search bar**: Filters by name or department in real-time (client-side, no API call).

**Doctor cards** (one per doctor):
```
[Avatar] Dr. Arjun Reddy                              [Active ✅]
         MBBS, DM Cardiology · 14 yrs · ⭐ 4.8
         🏥 Cardiology · ₹800 · 🌐 EN, HI, TE
         Next slot: Today 5:30 PM              [Edit] [Deactivate]
```

**Add/Edit Doctor modal** — form fields:
- Full Name (text, required)
- Department (dropdown: all dept options)
- Qualifications (text, e.g. "MBBS, MD, DM Cardiology")
- Experience Years (number)
- Consultation Fee ₹ (number)
- Working Days (checkboxes: Mon Tue Wed Thu Fri Sat)
- Morning Slots (comma-separated times, e.g. "09:00,09:30,10:00")
- Evening Slots (comma-separated times)
- Languages Spoken (multi-checkbox: English, Hindi, Telugu)
- Fun Fact (text, optional, shown in bot profile)

**Deactivate doctor**: Confirmation dialog — "Dr. Arjun will be hidden from patients. Existing bookings are not affected." Sets `is_active = false`.

**API calls**:
```
GET    /admin/doctors               → list all
POST   /admin/doctors               → add new
PUT    /admin/doctors/{id}          → edit
PATCH  /admin/doctors/{id}/toggle   → activate/deactivate
```

---

#### Page 3: `/admin/appointments` → Appointments

**Purpose**: Front desk views, searches, cancels, reschedules bookings.

**Filters bar**:
- Search: by patient name, booking ref (MC-XXXX), phone number
- Date picker: defaults to today
- Status filter: All / Confirmed / Completed / Cancelled / No-Show
- Department filter: dropdown

**Appointment card** (each booking):
```
MC-2026-4821  [Confirmed]  [Family Member]
Ravi Kumar
📞 +91 98765 43210  ·  👨‍⚕️ Dr. Arjun Reddy  ·  🏥 Cardiology
📅 17 Mar 2026  ·  ⏰ 10:00 AM
                                [Reschedule]  [Cancel]  [Mark Complete]
```

**Cancel action**: Confirm dialog → calls `DELETE /admin/appointments/{id}` → status = cancelled → WhatsApp notification sent automatically.

**Mark Complete**: Button visible only for past confirmed appointments → sets status = completed.

**Export**: "Export CSV" button downloads filtered results as CSV (use FastAPI `StreamingResponse`).

**API calls**:
```
GET    /admin/appointments?date=&status=&dept=&search=&page=
PUT    /admin/appointments/{id}         → update status
GET    /admin/appointments/export?...   → CSV download
```

---

#### Page 4: `/admin/leaves` → Doctor Leaves

**Purpose**: Front desk marks leaves. Bot auto-blocks slots and cancels affected bookings.

**Warning banner** (always visible):
> ⚠️ Adding a leave here will auto-cancel existing bookings on that date and notify patients via WhatsApp.

**Leave list**: Cards showing doctor, date, type, reason. "Remove" button restores availability.

**Add Leave modal**:
- Doctor (dropdown of active doctors)
- Date (date picker — cannot select past dates)
- Leave Type:
  - Full Day (entire day blocked)
  - Half Day — Morning Off (only evening slots available)
  - Half Day — Evening Off (only morning slots available)
- Reason (optional text)
- Affected bookings preview: "This will cancel 3 existing appointments. Patients will be notified."

**Affected bookings preview logic**: On date selection, call `GET /admin/appointments?doctor=&date=&status=confirmed` and show count in the modal before confirming.

**API calls**:
```
GET    /admin/leaves?doctor=
POST   /admin/leaves                → add leave, triggers auto-cancel job
DELETE /admin/leaves/{id}           → remove leave
```

---

#### Page 5: `/admin/analytics` → Analytics

**Purpose**: Hospital manager reviews performance weekly.

**KPI row**:
- Total Bookings (this month)
- Conversion Rate % (with trend arrow)
- No-Show Rate % (with benchmark: "Industry avg: 18%")
- WhatsApp Sessions Started

**Bookings by department**: Horizontal bar chart, sorted by volume.

**Bookings over time**: Line chart (last 30 days), one data point per day.

**Peak booking hours**: Hour-of-day bar chart (0–23h), shows when patients WhatsApp most.

**Patient feedback**: Star rating breakdown — Excellent / Good / Average / Poor with bar proportions.

**Doctor leaderboard**: Table — Doctor Name, Bookings, Rating, No-Show %, sorted by bookings.

**Callbacks pending** (after-hours requests): Count of patients who requested human help but haven't been called back. Link to `/admin/callbacks`.

**API calls**:
```
GET /admin/stats/daily
GET /admin/stats/departments
GET /admin/stats/peak-times
GET /admin/feedback/summary
GET /admin/doctors/leaderboard
GET /admin/callbacks?status=pending
```

---

#### Page 6: `/admin/holidays` → Hospital Holidays

**Purpose**: Manager adds public holidays so bot blocks slots and names the holiday to patients.

**Holiday list**: Simple cards — date, holiday name, "Remove" button.

**Add Holiday modal**: Date picker + holiday name text field.

**Seeded holidays**: Republic Day, Independence Day, Gandhi Jayanti (from migration SQL).

**Hospital-specific**: Hospital can add Diwali, Eid, regional holidays as needed.

**API calls**:
```
GET    /admin/holidays
POST   /admin/holidays
DELETE /admin/holidays/{date}
```

---

#### Page 7: `/admin/callbacks` → After-Hours Callbacks

**Purpose**: Patients who messaged after hours asking for human help. Staff calls them back.

**Card per pending callback**:
```
📞 Ravi Kumar  ·  +91 98765 43210
Message: "I need to speak to someone about my father's surgery"
Received: Yesterday 11:34 PM
                              [Mark Called]  [Book Appointment for them]
```

**"Book Appointment for them"** → opens a booking form pre-filled with their phone, routes to `/admin/appointments/new`.

**API calls**:
```
GET   /admin/callbacks?status=pending
PATCH /admin/callbacks/{id}    → mark as called
POST  /admin/appointments      → direct booking by staff
```

---

### 17.4 Admin Panel File Structure

```
admin/
├── index.html          # Single file — entire admin panel
├── css/
│   └── (Tailwind CDN — no local install)
└── js/
    └── (inline in index.html — no bundler)
```

Single `index.html` — Claude Code builds the entire admin panel in one file using:
- Tailwind CSS via CDN
- Vanilla JS `fetch()` calls to FastAPI endpoints
- Client-side routing (hash-based: `#/dashboard`, `#/doctors`, etc.)
- No React, no build step, no npm

Serve it from FastAPI:
```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app.mount("/admin/static", StaticFiles(directory="admin"), name="admin-static")

@app.get("/admin/{full_path:path}")
async def admin_panel(full_path: str, user=Depends(verify_admin)):
    return FileResponse("admin/index.html")
```

---

### 17.5 Mobile Responsiveness Rules

Hospital front desk staff often use phones. The admin panel must work on mobile:

- Sidebar collapses to a hamburger menu on screens < 768px
- Stat cards go from 3-column to 2-column to 1-column grid
- Tables scroll horizontally on small screens (overflow-x: auto)
- All modals are full-screen on mobile
- Touch targets minimum 44×44px (buttons, toggles)
- Date pickers use native HTML `<input type="date">` for mobile keyboard support

---

### 17.6 Performance Rules

The admin panel must load fast even on slow hospital WiFi:

- All API calls are parallel where possible (`Promise.all`)
- Dashboard stats load first, charts load after (non-blocking)
- Appointment list is paginated — 20 per page, load more on scroll
- Search is debounced 300ms — no API call on every keystroke
- Doctor/holiday/leave lists are cached in memory for the session
- No heavy charting library — build bar charts in pure CSS/HTML divs

---