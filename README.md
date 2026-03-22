# MediAssist AI - Hospital WhatsApp Assistant

A HIPAA-compliant, AI-powered WhatsApp assistant for hospital appointment scheduling built with FastAPI, Meta WhatsApp Cloud API, Supabase PostgreSQL, and Groq AI.

## Features

- 🤖 **AI-Powered Conversations** - Natural language understanding with Groq (Llama 3.3-70b)
- 📅 **Smart Appointment Booking** - Symptom-based department suggestions, doctor selection, slot availability
- 🌐 **Multilingual Support** - English, Hindi, and Telugu
- 🚨 **Emergency Detection** - Automatic emergency keyword detection with immediate escalation
- 🔔 **Automated Reminders** - 24-hour and 2-hour appointment reminders
- 📊 **Analytics Dashboard** - Real-time insights and reporting
- 🔒 **DPDP Compliant** - Data consent management and right to erasure
- 🏥 **Doctor Leave Management** - Automatic appointment cancellation for doctor leaves

## Tech Stack

- **Backend**: Python FastAPI
- **AI**: Groq API (Llama 3.3-70b)
- **Database**: Supabase PostgreSQL
- **Messaging**: Meta WhatsApp Cloud API
- **Scheduler**: APScheduler
- **Deployment**: Docker, Railway/Render

## Quick Start

### Prerequisites

- Python 3.11+
- Supabase account
- Meta Business account with WhatsApp API access
- Groq API key

### Installation

1. Clone the repository:
```bash
git clone https://github.com/your-org/mediassist-ai.git
cd mediassist-ai
```

2. Create virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Copy environment variables:
```bash
cp .env.example .env
# Edit .env with your credentials
```

5. Run database migrations in Supabase SQL Editor (see `migrations/001_initial_schema.sql`)

6. Start the server:
```bash
uvicorn app.main:app --reload
```

### Environment Variables

```env
# Meta WhatsApp Cloud API
WHATSAPP_TOKEN=your_permanent_access_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_VERIFY_TOKEN=your_custom_verify_string

# Groq AI
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile

# Supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key

# Hospital Config
HOSPITAL_NAME=City Care Hospital
HOSPITAL_EMERGENCY_NUMBER=108
HOSPITAL_PHONE=+919876543210
HOSPITAL_MAPS_LINK=https://maps.google.com/?q=YourHospitalLocation
HOSPITAL_WEBSITE=https://yourhospital.com
HOSPITAL_PRIVACY_POLICY_URL=https://yourhospital.com/privacy
HOSPITAL_ADDRESS=Plot 12, Hitech City, Hyderabad, Telangana 500081
HOSPITAL_LANDMARK=Near Cyber Towers
BOOKING_REF_PREFIX=MC

# App
APP_ENV=production
APP_PORT=8000

# Admin
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your_secure_password
```

## WhatsApp Webhook Setup

1. Expose your local server using ngrok:
```bash
ngrok http 8000
```

2. In Meta Business Manager:
   - Go to WhatsApp > Configuration
   - Set webhook URL: `https://your-ngrok-url/webhook`
   - Set verify token (match WHATSAPP_VERIFY_TOKEN)
   - Subscribe to `messages` webhook field

## API Endpoints

### Public Endpoints

- `GET /` - Service info
- `GET /health` - Health check
- `GET /health/ready` - Readiness check
- `GET /health/live` - Liveness check

### Webhook Endpoints

- `GET /webhook` - Webhook verification (Meta)
- `POST /webhook` - Receive WhatsApp messages
- `POST /webhook/test` - Test webhook (development)

### Admin Endpoints (Basic Auth)

- `GET /admin/stats` - Dashboard statistics
- `GET /admin/appointments/recent` - Recent appointments
- `GET /admin/appointments/upcoming` - Upcoming appointments
- `GET /admin/doctors` - List doctors
- `POST /admin/doctors` - Add doctor
- `GET /admin/leaves` - List doctor leaves
- `POST /admin/leaves` - Add doctor leave
- `DELETE /admin/leaves/{id}` - Remove doctor leave
- `GET /admin/holidays` - List holidays
- `POST /admin/holidays` - Add holiday

## Project Structure

```
mediassist-ai/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings & env vars
│   ├── database.py          # Supabase client
│   ├── routers/
│   │   ├── webhook.py       # WhatsApp webhook handler
│   │   ├── health.py        # Health checks
│   │   └── admin.py         # Admin API
│   ├── services/
│   │   ├── ai_engine.py     # Groq AI integration
│   │   ├── conversation.py  # State machine
│   │   ├── appointment.py   # Booking logic
│   │   ├── scheduler.py     # Reminders & follow-ups
│   │   ├── consent.py       # DPDP compliance
│   │   ├── analytics.py     # Event tracking
│   │   └── whatsapp.py      # WhatsApp API
│   ├── models/
│   │   ├── patient.py       # Patient models
│   │   ├── appointment.py   # Appointment models
│   │   ├── conversation.py  # Session models
│   │   └── message.py       # Webhook payload models
│   ├── templates/
│   │   └── whatsapp_templates.py  # Message templates
│   └── utils/
│       ├── logger.py        # Logging config
│       ├── validators.py    # Input validation
│       └── helpers.py       # Utilities
├── migrations/
│   └── 001_initial_schema.sql  # Database schema
├── tests/
│   ├── test_webhook.py
│   ├── test_ai_engine.py
│   └── test_appointment.py
├── Dockerfile
├── railway.toml
├── requirements.txt
└── README.md
```

## Testing

Run tests with pytest:

```bash
pytest tests/ -v
```

## Deployment

### Railway

1. Connect your GitHub repo to Railway
2. Add environment variables in Railway dashboard
3. Deploy automatically on push

### Render

1. Create a new Web Service
2. Connect your GitHub repo
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables

### Docker

```bash
docker build -t mediassist-ai .
docker run -p 8000:8000 --env-file .env mediassist-ai
```

## Compliance

- **Meta Business Policy**: 24-hour session window, opt-in/opt-out handling
- **India DPDP Act 2023**: Data consent collection, right to erasure
- **Healthcare**: No diagnostic claims, emergency escalation, medical disclaimer

## License

MIT License - See LICENSE file

## Support

For support, email support@yourhospital.com or call +91-XXX-XXXXXXX.
