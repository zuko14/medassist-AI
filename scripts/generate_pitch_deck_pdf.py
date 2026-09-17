"""
Kriya AI — Executive Pitch Deck v2.0 PDF Generator
Renders a 21-slide, pixel-perfect 16:9 executive presentation using Playwright.
Matches the exact deep-navy, medical-cyan, high-tech enterprise design of the original deck.
"""

import os
import sys
from playwright.sync_api import sync_playwright

OUTPUT_DIR = r"c:\Users\chait\OneDrive\Desktop\SYSTEMS_ALL\KriyaAI\docs\presentations"
HTML_PATH = os.path.join(OUTPUT_DIR, "pitch_deck_v2.html")
PDF_PATH = os.path.join(OUTPUT_DIR, "Kriya_AI_Client_Pitch_Deck_v2.pdf")

SLIDES_DATA = [
    # Slide 1
    {
        "page_num": "01 / 21",
        "tag": "ENTERPRISE MULTI-TENANT OS",
        "tag_type": "cyan",
        "title": "Healthcare Operations, Reimagined.",
        "subtitle": "The 24/7 Intelligent Operations Layer for Multi-Specialty Hospitals, Specialty Clinics (Derma, Dental, Eye, IVF) & Diagnostic Centers on WhatsApp.",
        "cards": [
            {"title": "Instant 24/7 Patient Access", "desc": "Zero app downloads, zero logins. Reaches 98% of Indian smartphone users on verified WhatsApp.", "icon": "💬"},
            {"title": "ACID-Compliant Safety", "desc": "PostgreSQL database slot locking prevents double-booking race conditions mathematically.", "icon": "🛡️"},
            {"title": "Specialty & Procedure Catalogs", "desc": "Concern-based triage, procedural packages, transparent pricing, and deposit-backed slot locking.", "icon": "✨"},
            {"title": "Turnkey EMR & LIMS Bridges", "desc": "Headless browser connectors for MocDoc, CallMedex, and web HMIS with zero legacy vendor code.", "icon": "🔗"},
        ],
        "what_to_say": "Kriya AI solves the single largest operational bottleneck in modern healthcare: front-desk friction, phone overload, and lost high-margin procedure inquiries. By turning your hospital's verified WhatsApp into an autonomous front door, Kriya handles appointments, specialty packages, UPI deposits, live queues, and lab delivery with zero staff overhead.",
        "badge_title": "OBJECTION DEFENSE ('IS THIS A CHATBOT?')",
        "badge_desc": "No. Kriya AI is a deterministic operational state machine backed by PostgreSQL ACID database locks and clinical safety firewalls that executes verified transactions, not unguided text.",
        "badge_type": "amber"
    },
    # Slide 2
    {
        "page_num": "02 / 21",
        "tag": "OPERATIONAL REALITY",
        "tag_type": "crimson",
        "title": "The Front-Desk Crisis in Outpatient & Specialty Care",
        "subtitle": "High-volume hospital clinics and specialty centers face systemic administrative congestion across four critical touchpoints.",
        "cards": [
            {"title": "Phone Line Overload", "desc": "Receptionists spend up to 70% of their workday answering repetitive scheduling calls. High call abandonment drives patients directly to competitors.", "icon": "📞", "alert": True},
            {"title": "The No-Show Epidemic", "desc": "20% to 35% of booked consultations and high-ticket elective procedure slots are lost due to forgotten visits. Unsecured bookings drain specialist productivity.", "icon": "❌", "alert": True},
            {"title": "Waiting Room Congestion", "desc": "Patients endure 45 to 90 minutes of blind waiting in overcrowded lobby areas with zero real-time visibility into consulting doctor delays or live queue tokens.", "icon": "👥", "alert": True},
            {"title": "Diagnostic & Package Friction", "desc": "Staff are overwhelmed by repetitive calls checking lab report readiness, fasting rules, or explaining procedure pricing, sessions, and downtime.", "icon": "🧪", "alert": True},
        ],
        "what_to_say": "Every hospital executive and clinic owner knows the front desk is under siege. Receptionists field hundreds of repetitive calls: 'Which doctor is in?', 'How much does laser hair reduction cost?', 'Is my report ready?'. This friction creates lost revenue and patient dissatisfaction.",
        "badge_title": "SUPPORTING PROOF",
        "badge_desc": "Field studies across Indian hospital networks demonstrate that up to 30% of OPD capacity and 25% of elective procedure slots are wasted due to unconfirmed bookings and patient no-shows.",
        "badge_type": "amber"
    },
    # Slide 3
    {
        "page_num": "03 / 21",
        "tag": "IMPACT ANALYSIS",
        "tag_type": "cyan",
        "title": "Where Healthcare Facilities Lose Revenue & Patient Trust",
        "subtitle": "Front-desk administrative friction directly erodes clinic EBITDA and creates serious regulatory and clinical exposure.",
        "split_columns": [
            {
                "title": "Administrative & Clinical Friction",
                "icon": "👥",
                "items": [
                    ("Staff Burnout & Turnover", "High front-desk turnover requires perpetual training cycles for low-efficiency manual call handlers."),
                    ("Doctor & Specialist Frustration", "Double-booked appointments, unmanaged walk-ins, and schedule overruns lead to clinical dissatisfaction."),
                    ("Data Re-entry Inefficiencies", "Receptionists manually type patient details across paper registers, billing software, and siloed HMIS consoles.")
                ]
            },
            {
                "title": "Financial & Regulatory Exposure",
                "icon": "⚖️",
                "items": [
                    ("High-Ticket Revenue Leakage", "Empty doctor chairs and unbooked procedure slots (₹5,000–₹75,000) represent irrecoverable operational revenue loss."),
                    ("Digital Patient Attrition", "Modern patients abandon phone-dependent clinics in favor of tech-forward healthcare providers offering instant booking."),
                    ("DPDP Act 2023 Compliance Risk", "Unstructured handling of patient health reports over informal staff WhatsApp channels exposes clinics to heavy regulatory penalties.")
                ]
            }
        ],
        "what_to_say": "The true cost of a broken front-desk workflow isn't just phone bills. It is empty doctor chairs during prime hours, lost aesthetic procedures, receptionist attrition, and severe legal liability under India's Digital Personal Data Protection Act 2023.",
        "badge_title": "COMPLIANCE FOCUS",
        "badge_desc": "Informal WhatsApp communication by staff lacks audit trails. Kriya AI provides an enterprise, logged, DPDP-compliant transaction infrastructure with automated consent records.",
        "badge_type": "amber"
    },
    # Slide 4
    {
        "page_num": "04 / 21",
        "tag": "ARCHITECTURE OVERVIEW",
        "tag_type": "cyan",
        "title": "An Intelligent Operations Layer on WhatsApp",
        "subtitle": "Connecting patients to hospital doctors, specialty procedures, cashless payments, live queues, and EMRs through a unified platform.",
        "cards": [
            {"title": "24/7 Digital Front Door", "desc": "Patients interact naturally via verified WhatsApp in English, Hindi, or Telugu. Zero app download, zero logins, 100% device compatibility.", "icon": "💬"},
            {"title": "Dual-Path Clinical Triage", "desc": "Groq Llama-3.3-70b AI maps described symptoms to medical departments, while the Concern Engine maps aesthetic/specialty requests directly to treatments.", "icon": "🧠"},
            {"title": "Transaction & Slot Engine", "desc": "Atomic database slot locking, Razorpay UPI pre-payments & procedure deposits, dynamic shift buffers, and real-time waiting room queue tokens.", "icon": "⚡"},
            {"title": "Diagnostream & Booking Bridge", "desc": "Autonomous EMR scraping that extracts lab results and delivers signed PDFs with AI summaries, plus direct WhatsApp lab test booking.", "icon": "🧬"},
        ],
        "what_to_say": "Kriya AI acts as connective tissue between your patients and your hospital operations. It meets patients where they already are—on WhatsApp—and turns casual messages into verified, paid hospital transactions.",
        "badge_title": "KEY DISTINCTION",
        "badge_desc": "Unlike customer support bots, Kriya AI is an operational state machine directly connected to doctor rosters, specialty catalogs, payment gateways, and laboratory systems.",
        "badge_type": "cyan"
    },
    # Slide 5
    {
        "page_num": "05 / 21",
        "tag": "PATIENT JOURNEY",
        "tag_type": "cyan",
        "title": "Effortless, 24/7 Patient Self-Service Under 60 Seconds",
        "subtitle": "Two conversion-optimized conversational flows completed in under 60 seconds: General Consultations & Specialty Treatments.",
        "dual_flow": {
            "flow1_title": "Pathway A: OPD Doctor Consultation Flow",
            "flow1_steps": [
                ("1. Greeting", "Patient texts verified WhatsApp in EN, HI, or TE. Greeted 24/7 instantly."),
                ("2. Symptom Triage", "Enters symptoms (e.g. 'Fever & throat pain') -> AI maps to General Medicine / ENT."),
                ("3. Slot Selection", "Interactive buttons display real-time slots across doctor consulting rosters."),
                ("4. UPI Pre-Payment", "10-minute temporary database slot hold with instant Razorpay UPI payment link."),
                ("5. Confirmed Token", "Confirmed appointment card with Live Queue Token (e.g. Q-014) & Google Maps pin.")
            ],
            "flow2_title": "Pathway B: Specialty Treatment & Concern Flow (Derma, Dental, Eye, IVF)",
            "flow2_steps": [
                ("1. Catalog Tap", "Patient selects '✨ Our Treatments' or '🔍 Find by Concern' on WhatsApp."),
                ("2. Concern Match", "Patient types 'Acne scars' -> Matches HydraFacial & Chemical Peel packages."),
                ("3. Transparency", "Displays duration, session counts, package pricing, and specialist doctor profile."),
                ("4. Token Deposit", "Collects customizable procedure deposit (e.g. ₹500 or 25%) via Razorpay UPI."),
                ("5. Pre-Care Delivery", "Confirmed booking plus automated pre-procedure preparation guidelines sent to WhatsApp.")
            ]
        },
        "what_to_say": "From the patient's perspective, booking a specialist consultation or discovering an aesthetic treatment is as natural as texting a family member. In under one minute, they describe their issue, choose a time, pay securely, and get their confirmed digital token.",
        "badge_title": "WHY THIS WINS",
        "badge_desc": "Patients do not need to download custom hospital apps, remember passwords, or wait on hold during busy reception hours. 60-second self-service triples booking completion.",
        "badge_type": "amber"
    },
    # Slide 6
    {
        "page_num": "06 / 21",
        "tag": "CLINICAL & ADMIN EXPERIENCE",
        "tag_type": "cyan",
        "title": "Operational Clarity for Staff, Doctors & Specialists",
        "subtitle": "Transforming workplace productivity across reception desks, doctor cabins, specialty treatment rooms, and executive suites.",
        "split_three": [
            {
                "title": "Front-Desk Staff",
                "icon": "🖥️",
                "items": [
                    "60%–80% Call Reduction: Eliminates repetitive calls for scheduling and lab report inquiries.",
                    "Live Reception Console (reception.html): Single-click patient check-ins and smooth walk-in queue management.",
                    "Focus on In-Person Care: Staff can attend warmly to patients standing at the desk."
                ]
            },
            {
                "title": "Consulting Doctors & Specialists",
                "icon": "👨‍⚕️",
                "items": [
                    "Schedule Sovereignty: Precise slot durations with zero accidental double-booking.",
                    "Instant Leave Automation: 1-click leave declaration automatically alerts and reschedules patients.",
                    "Specialty Procedure Console (specialty.html): Real-time visibility into booked treatments and chair-time."
                ]
            },
            {
                "title": "Hospital Leadership & Owners",
                "icon": "🏢",
                "items": [
                    "Multi-Branch Visibility: Centralized platform console monitoring doctor shifts and footfall.",
                    "Revenue Transparency: Real-time tracking of pre-collected consultation fees and procedure deposits.",
                    "Patient CSAT Ratings: Automated post-consultation sentiment tracking and Google review boost."
                ]
            }
        ],
        "what_to_say": "For hospital staff, Kriya AI removes chaotic ringing phones. For doctors and specialists, it protects their consulting schedule and chair time from double-bookings and runaway queues. For administrators, it provides real-time operational and financial transparency.",
        "badge_title": "DOCTOR BUY-IN",
        "badge_desc": "Senior specialists love the platform because it respects their customized consulting shift rosters, eliminates walk-in chaos, and protects their private time.",
        "badge_type": "amber"
    },
    # Slide 7
    {
        "page_num": "07 / 21",
        "tag": "CORE ARCHITECTURE",
        "tag_type": "cyan",
        "title": "AI Speed with Zero Clinical Compromise",
        "subtitle": "A strict architectural separation between Natural Language Understanding and Authoritative Execution.",
        "split_columns": [
            {
                "title": "AI Handles Interpretation & Language",
                "icon": "🧠",
                "items": [
                    ("Multilingual Comprehension", "Understands colloquial English, Hindi, and Telugu phrasing seamlessly."),
                    ("Department & Concern Triage", "Groq Llama-3.3-70b matches patient concerns to specialist departments and treatment packages under strict zero-hallucination guardrails."),
                    ("Patient-Friendly Lab Summaries", "Translates complex laboratory parameters into layman-friendly explanations with PII redaction.")
                ]
            },
            {
                "title": "Deterministic Code Enforces Healthcare Safety",
                "icon": "🛡️",
                "items": [
                    ("Zero-LLM Clinical Safety Firewall", "Hard regex interceptor immediately blocks medical advice, drug prescriptions, and unauthorized diagnosis."),
                    ("ACID Database Slot Locking", "PostgreSQL partial unique indexes eliminate double-booking race conditions mathematically."),
                    ("Cryptographic Financial Validation", "Razorpay HMAC-SHA256 signature verification protects ledger integrity.")
                ]
            }
        ],
        "what_to_say": "In healthcare, generative AI must never make medical diagnoses or directly modify database records. Kriya AI enforces strict architectural separation: AI interprets language; deterministic code executes verified hospital transactions.",
        "badge_title": "OBJECTION DEFENSE ('WHAT IF AI HALLUCINATES?')",
        "badge_desc": "The AI is sandboxed. It has zero authority to dispense medical advice, prescribe drugs, or confirm appointments without database lock validation.",
        "badge_type": "amber"
    },
    # Slide 8
    {
        "page_num": "08 / 21",
        "tag": "FINANCIAL & SLOT ENGINE",
        "tag_type": "cyan",
        "title": "Frictionless Scheduling & Revenue Protection",
        "subtitle": "Eliminating empty doctor chairs and idle procedure rooms through dynamic capacity calculation and upfront UPI settlement.",
        "cards": [
            {"title": "Dynamic Rostering", "desc": "Calculates live slots by factoring doctor shift rosters, room allocations, declared leaves, and 30-minute advance booking buffers.", "icon": "📅"},
            {"title": "Atomic Slot Locking", "desc": "When a patient selects a time, a 10-minute temporary database lock is applied. No competing patient can claim the slot during checkout.", "icon": "🔒"},
            {"title": "Razorpay UPI Engine", "desc": "Collects full consultation fees, elective procedure deposits, or nominal token fees via UPI, GPay, PhonePe, Cards, or NetBanking with zero friction.", "icon": "💳"},
            {"title": "Automated Expiry & Refund", "desc": "Unpaid holds automatically release back to the pool after 10 minutes. Failed payment webhooks trigger automatic patient refunds.", "icon": "🔄"},
        ],
        "what_to_say": "By requiring upfront UPI pre-payment or nominal token deposits, Kriya AI drastically cuts no-show rates. Patients commit to their appointment, securing hospital revenue and doctor time.",
        "badge_title": "FLEXIBLE POLICY",
        "badge_desc": "Hospitals can customize payment policies per department: full advance payment for high-demand specialists, nominal ₹100 confirmation token, or ₹500 procedure deposits.",
        "badge_type": "amber"
    },
    # Slide 9
    {
        "page_num": "09 / 21",
        "tag": "PATIENT FLOW MANAGEMENT",
        "tag_type": "cyan",
        "title": "Transforming the Physical Waiting Room",
        "subtitle": "Eliminating lobby crowding and patient anxiety with real-time digital queue tokens.",
        "split_columns": [
            {
                "title": "Traditional Blind OPD Waiting",
                "icon": "❌",
                "alert": True,
                "items": [
                    ("Anxious Lobby Crowding", "Patients crowd consultation doors, demanding updates from overworked nursing staff."),
                    ("Zero Delay Visibility", "Unforeseen doctor delays cause patient frustration and vocal complaints in public areas."),
                    ("Cross-Infection Exposure", "Packed waiting rooms increase infection risk for vulnerable patients.")
                ]
            },
            {
                "title": "Kriya AI Live Digital Queue Experience",
                "icon": "📱",
                "items": [
                    ("Sequential Token Allocation", "Every patient receives a unique digital token (e.g. Q-014) upon booking."),
                    ("Real-Time WhatsApp Updates", "\"Token Q-011 is now inside with Dr. Sharma. 3 patients ahead of you.\""),
                    ("Decongested Lobbies", "Patients wait comfortably in hospital cafeterias or gardens until summoned."),
                    ("Single-Click Advance", "Receptionists advance queue tokens from the live console (reception.html) with 1 click.")
                ]
            }
        ],
        "what_to_say": "Waiting room anxiety is the number one driver of poor patient satisfaction scores. With Kriya AI, patients track their position live on WhatsApp and only approach the consultation suite when their token is called.",
        "badge_title": "DISPLAY SYNC",
        "badge_desc": "The digital queue integrates directly with hospital hallway LED TV displays and receptionist call desks with single-click token advance.",
        "badge_type": "amber"
    },
    # Slide 10
    {
        "page_num": "10 / 21",
        "tag": "LABORATORY AUTOMATION",
        "tag_type": "cyan",
        "title": "Diagnostream: Lab Result to WhatsApp in Minutes",
        "subtitle": "Autonomous EMR scraping, patient identity matching, and AI clinical summaries with one-click follow-up CTAs.",
        "cards": [
            {"title": "WhatsApp Test Booking", "desc": "Patients browse lab tests and checkup packages on WhatsApp, selecting home sample collection or clinic walk-in with fasting guidelines.", "icon": "🧪"},
            {"title": "Autonomous Scraping", "desc": "Playwright connector daemon polls laboratory EMRs (e.g., MocDoc) every 10 minutes for authorized test reports with zero vendor API cost.", "icon": "🤖"},
            {"title": "Patient Match Safety", "desc": "Fuzzy similarity engine strips honorifics and matches patient names and phones to prevent confidential data misrouting.", "icon": "🔒"},
            {"title": "Instant Delivery & AI Summary", "desc": "Delivers authenticated PDF report on WhatsApp with an AI summary highlighting out-of-range values and a 1-click doctor follow-up button.", "icon": "📄"},
        ],
        "what_to_say": "Diagnostream eliminates physical report collection trips. The moment a pathologist signs off a report in your LIMS, Diagnostream validates identity, extracts parameters, and sends the PDF to WhatsApp with an AI summary.",
        "badge_title": "CONFIDENTIALITY PROTECTION",
        "badge_desc": "If patient name match confidence is below 85%, the report is routed to front-desk review rather than auto-delivered.",
        "badge_type": "amber"
    },
    # Slide 11
    {
        "page_num": "11 / 21",
        "tag": "CHANNEL STRATEGY",
        "tag_type": "cyan",
        "title": "Why WhatsApp Outperforms Custom Mobile Apps",
        "subtitle": "Leveraging the universal digital platform already active on 500M+ Indian smartphones.",
        "metrics_quad": [
            {"stat": "98%", "label": "Smartphone Reach", "sub": "Active across all age groups & demographics"},
            {"stat": "< 8%", "label": "Hospital App Installs", "sub": "Most deleted within 48 hours of download"},
            {"stat": "60s", "label": "Average Booking Time", "sub": "From first message to confirmed UPI token"},
            {"stat": "0", "label": "App Store Downloads", "sub": "Zero friction, zero storage footprint"}
        ],
        "sidebar_points": [
            ("Official Meta Green-Tick Account", "Protects hospital credibility and establishes verified institutional branding."),
            ("Multilingual Fluency", "Engages patients in their preferred regional language (English, Hindi, Telugu)."),
            ("90%+ Open Rates", "WhatsApp appointment reminders achieve 5x higher engagement than SMS or email.")
        ],
        "what_to_say": "Hospitals have spent millions developing custom mobile apps that fewer than 8% of patients ever download. WhatsApp is already active on every smartphone in India. Meeting patients on WhatsApp guarantees instant adoption.",
        "badge_title": "ZERO MARKETING EXPENSE",
        "badge_desc": "Hospitals do not need ad campaigns to drive downloads. Simply placing a WhatsApp QR code at reception activates patient self-service immediately.",
        "badge_type": "amber"
    },
    # Slide 12
    {
        "page_num": "12 / 21",
        "tag": "ECOSYSTEM INTEGRATION",
        "tag_type": "cyan",
        "title": "Coexists with Your Current Hospital Technology",
        "subtitle": "Non-invasive connectors that sit on top of your existing HMIS without requiring rip-and-replace disruption.",
        "cards": [
            {"title": "Non-Invasive EMR Connectors", "desc": "Headless Playwright daemons sync with MocDoc, CallMedex, and web HMIS with zero expensive custom API development required from legacy vendors.", "icon": "🔌"},
            {"title": "HL7 FHIR R4 Standards", "desc": "REST API endpoints structured around international healthcare data standards, ensuring seamless integration with modern enterprise platforms.", "icon": "🏥"},
            {"title": "ABDM / ABHA Ready", "desc": "Built-in schema foundations and gateway stubs for Ayushman Bharat Digital Mission patient identifiers and electronic health record compliance.", "icon": "🇮🇳"},
            {"title": "Parallel Zero-Disruption Engine", "desc": "Operates as an external digital front door while feeding verified bookings and payments into your core billing and inpatient management records.", "icon": "⚙️"},
        ],
        "what_to_say": "Kriya AI does not ask you to replace your core hospital management software. It acts as the modern, patient-facing digital front door that communicates bi-directionally with your existing systems.",
        "badge_title": "LEGACY HMIS COMPATIBILITY",
        "badge_desc": "Even if your HMIS vendor does not provide APIs, our automated browser connector securely synchronizes appointment rosters.",
        "badge_type": "amber"
    },
    # Slide 13
    {
        "page_num": "13 / 21",
        "tag": "DATA GOVERNANCE",
        "tag_type": "cyan",
        "title": "Bank-Grade Security & DPDP Act Compliance",
        "subtitle": "Multi-tenant isolation, cryptographic verification, and automated personal data protection guardrails.",
        "cards": [
            {"title": "Multi-Tenant RLS", "desc": "PostgreSQL Row-Level Security guarantees mathematical data isolation. One clinic or branch can never view or query another tenant's records.", "icon": "🛡️"},
            {"title": "Cryptographic Ingestion", "desc": "All inbound webhooks are verified via Meta HMAC-SHA256 signatures (X-Hub-Signature-256), blocking spoofing attacks.", "icon": "🔐"},
            {"title": "DPDP Act Governance", "desc": "Automated patient consent logging, right-to-erasure endpoints, and 30-day chat transcript purges while retaining 7-year medical audit logs.", "icon": "⚖️"},
            {"title": "PCI-DSS Scope Minimization", "desc": "Zero cardholder or banking data stored on Kriya servers. All financial flows processed via RBI-regulated Razorpay payment gateways.", "icon": "💳"},
        ],
        "what_to_say": "We treat healthcare security and compliance with uncompromising rigor. With database Row-Level Security and automated DPDP consent logging, your leadership is fully protected against breaches and regulatory fines.",
        "badge_title": "DATA RESIDENCY",
        "badge_desc": "All patient data and audit trails are hosted on secure Indian cloud infrastructure in full alignment with national data localization mandates.",
        "badge_type": "amber"
    },
    # Slide 14
    {
        "page_num": "14 / 21",
        "tag": "HIGH AVAILABILITY",
        "tag_type": "cyan",
        "title": "Engineered for Zero Collisions & Zero Failures",
        "subtitle": "Mission-critical concurrency controls that guarantee transactional integrity under high load.",
        "split_columns": [
            {
                "title": "Concurrency & Collision Prevention",
                "icon": "🔒",
                "items": [
                    ("PostgreSQL ACID Slot Protection", "Database partial unique indexes reject concurrent slot collision attempts at the engine level."),
                    ("Message Idempotency", "An atomic processed_messages ledger prevents duplicate executions during WhatsApp network retries.")
                ]
            },
            {
                "title": "Fail-Closed Safety & Durable Scheduling",
                "icon": "⚡",
                "items": [
                    ("Fail-Closed State Machine", "Ambiguous payment webhooks or conflicting patient names transition safely to pending_review rather than auto-confirming."),
                    ("Durable Background Daemons", "Distributed background schedulers guarantee 24-hour and 2-hour WhatsApp reminder dispatches.")
                ]
            }
        ],
        "what_to_say": "What happens when two patients try to book the last remaining doctor slot at the exact same second? Generic chatbots double-book. In Kriya AI, ACID database locks guarantee exactly one transaction succeeds while the other is cleanly routed.",
        "badge_title": "HIGH CONCURRENCY",
        "badge_desc": "The system is load-tested to handle concurrent patient surges during morning booking rushes without slowdown or transaction loss.",
        "badge_type": "amber"
    },
    # Slide 15 - UPDATED OPERATIONAL TIERS
    {
        "page_num": "15 / 21",
        "tag": "OPERATIONAL TIERS",
        "tag_type": "cyan",
        "title": "Modular Architecture for Every Healthcare Scale",
        "subtitle": "11 purpose-built operational plans structured across 4 distinct healthcare delivery models.",
        "cards": [
            {
                "title": "1. General OPD Tiers",
                "badge": "SOLO / ESSENTIAL / POLY / ENT",
                "desc": "SoloClinic (1 Dr fast booking), Essential (1-3 Drs, shift rosters, reminders), PolyClinic (up to 25 Drs, queue tokens, department triage), Enterprise (multi-branch chains, custom EMR bridges, RBAC).",
                "icon": "🏥"
            },
            {
                "title": "2. Specialty Verticals",
                "badge": "DERMA / DENTAL / EYE / IVF",
                "desc": "Dermatology (HydraFacial, peels, concern triage), Dental (aligners, chair-time slotting), Ophthalmology (cataract, dilation alerts), IVF & Fertility (discrete couple onboarding, semen analysis).",
                "icon": "✨"
            },
            {
                "title": "3. Diagnostic Infrastructure",
                "badge": "DIAGSTREAM / DIAGBOOKING",
                "desc": "Diagnostream (autonomous LIMS scraping, OCR summaries, PDF WhatsApp dispatch) and Diagbooking (WhatsApp test catalog, home sample collection, fasting prep alerts).",
                "icon": "🧪"
            },
            {
                "title": "4. Multi-Specialty Flagship",
                "badge": "HYBRID HOSPITAL OS",
                "desc": "Unified platform combining General OPD Doctor Triage + Specialty Treatment Catalog (✨ Our Treatments + 🔍 Find by Concern + Book Doctor + Lab Tests in an 8-row menu).",
                "icon": "⭐"
            }
        ],
        "what_to_say": "Kriya AI is not a one-size-fits-all tool. Whether you run a solo practice, a 25-doctor polyclinic, a specialized aesthetic or eye center, an independent pathology lab, or a multi-branch hospital chain, our 11 modular plans match your exact clinical workflows.",
        "badge_title": "MODULAR UPGRADES",
        "badge_desc": "Healthcare networks can begin with an Essential or Specialty tier and scale up to Multi-Specialty or Enterprise without system re-installation.",
        "badge_type": "amber"
    },
    # Slide 16 - NEW SLIDE: SPECIALTY CLINICAL DEPTH
    {
        "page_num": "16 / 21",
        "tag": "SPECIALTY VERTICALS",
        "tag_type": "cyan",
        "title": "Built for Specialized Healthcare: Derma, Dental, Eye & IVF",
        "subtitle": "Moving beyond consultations: procedural packages, pre/post care instructions, and concern-based triage.",
        "cards": [
            {
                "title": "Dermatology & Aesthetics",
                "badge": "PLAN: DERMA",
                "desc": "Pre-seeded starter catalogs (HydraFacial, Peels, Laser Hair Reduction). Concern matching ('Acne scars', 'Pigmentation'). Automated pre-care warnings (avoid retinol, sun protection).",
                "icon": "🌸"
            },
            {
                "title": "Dental Care & Polyclinics",
                "badge": "PLAN: DENTAL",
                "desc": "High-ticket procedure booking (Root Canal, Clear Aligners, Implants). Variable chair-time slotting for complex multi-step treatments. Post-procedure diet and hygiene WhatsApp guides.",
                "icon": "🦷"
            },
            {
                "title": "Ophthalmology & Eye Care",
                "badge": "PLAN: EYE",
                "desc": "Specialized packages: Cataract Evaluation, LASIK Screening, Diabetic Retinopathy, Dry Eye. Critical clinical preparation alerts: pupil dilation travel warnings (bring an escort).",
                "icon": "👁️"
            },
            {
                "title": "Fertility & IVF Centers",
                "badge": "PLAN: IVF",
                "desc": "High-privacy onboarding: confidential couple consultations, IUI/IVF packages, semen analysis. Empathy-first clinical guardrails and discrete communications.",
                "icon": "🌱"
            }
        ],
        "what_to_say": "Specialty clinics cannot survive on simple doctor time slots alone. They depend on high-margin procedural treatments. Kriya AI understands the unique clinical language of dermatology, dental care, ophthalmology, and fertility, delivering pre-care guidance and package pricing natively on WhatsApp.",
        "badge_title": "PRE-SEEDED SPEED",
        "badge_desc": "Every specialty plan comes pre-seeded with clinical starter treatments that clinics can review and activate in under 10 minutes.",
        "badge_type": "amber"
    },
    # Slide 17 - NEW SLIDE: 6 DEDICATED ADMIN CONSOLES
    {
        "page_num": "17 / 21",
        "tag": "ADMINISTRATIVE SUITE",
        "tag_type": "cyan",
        "title": "6 Dedicated Web Consoles for Every Team Role",
        "subtitle": "A cohesive suite of role-tailored web portals delivering operational control with zero desktop software installation.",
        "cards": [
            {
                "title": "Live Reception Desk",
                "badge": "reception.html",
                "desc": "1-click patient check-in, walk-in queue token issuance, no-show marking, and live hallway TV display synchronization.",
                "icon": "🖥️"
            },
            {
                "title": "Doctor Cabin Portal",
                "badge": "doctor.html",
                "desc": "Doctor schedule sovereignty: view daily appointment queue, advance tokens, and 1-click leave/delay declaration with auto-rescheduling.",
                "icon": "🩺"
            },
            {
                "title": "Specialty Treatment Console",
                "badge": "specialty.html",
                "desc": "For Derma, Dental, Eye, and IVF clinics: manage procedure packages, pricing, session counts, concern tags, and doctor qualifications.",
                "icon": "✨"
            },
            {
                "title": "Multi-Specialty Hospital Command",
                "badge": "multispecialty.html",
                "desc": "Unified administration for full hospitals: manage consulting doctor rosters, specialty treatments, and department triage from one interface.",
                "icon": "🏥"
            },
            {
                "title": "Diagnostic Lab Console",
                "badge": "diagnostics.html",
                "desc": "Pathology operations: monitor LIMS scraping queues, review OCR confidence scores, and dispatch home collection phlebotomists.",
                "icon": "🔬"
            },
            {
                "title": "Platform Superadmin",
                "badge": "platform.html",
                "desc": "Multi-tenant hospital chain headquarters: tenant provisioning, plan upgrades, WhatsApp credential verification, and cross-branch analytics.",
                "icon": "🌐"
            }
        ],
        "what_to_say": "A receptionist should not see the hospital billing database, and a doctor should not navigate a complex HMIS just to declare a delay. Kriya AI provides 6 lightweight, role-tailored web portals that give every staff member exactly what they need in one click.",
        "badge_title": "ZERO INSTALLATION",
        "badge_desc": "All consoles run as fast, responsive web applications accessible from reception desktops, doctor tablets, or smartphones with secure role-based session tokens.",
        "badge_type": "amber"
    },
    # Slide 18 - ROI
    {
        "page_num": "18 / 21",
        "tag": "ROI & EFFICIENCY",
        "tag_type": "cyan",
        "title": "Modelled Operational Returns for Leadership",
        "subtitle": "Quantifiable performance improvements observed across deployed clinical environments.",
        "metrics_row": [
            {"stat": "60%–80%", "label": "Call Volume Drop", "sub": "Repetitive front-desk scheduling calls eliminated"},
            {"stat": "50%–70%", "label": "No-Show Reduction", "sub": "Achieved via automated WhatsApp 24h/2h reminders"},
            {"stat": "30%–50%", "label": "Procedure Uplift", "sub": "Immediate 24/7 capture of high-margin inquiries"},
            {"stat": "90%", "label": "Lab Query Elimination", "sub": "Automated report delivery removes inquiry calls"},
            {"stat": "100%", "label": "After-Hours Capture", "sub": "Night & weekend booking inquiries converted to revenue"}
        ],
        "what_to_say": "Implementing Kriya AI yields immediate, measurable returns. Reception call volumes drop by up to 80%, appointment no-shows decline by over half, and high-margin elective procedure inquiries convert directly into confirmed revenue.",
        "badge_title": "DIRECT PAYBACK",
        "badge_desc": "Recovering just two missed specialist consultations or one elective procedure per doctor per week completely offsets administrative overhead.",
        "badge_type": "amber"
    },
    # Slide 19 - MARKET COMPARISON
    {
        "page_num": "19 / 21",
        "tag": "MARKET COMPARISON",
        "tag_type": "cyan",
        "title": "Kriya AI vs. Conventional Hospital Software",
        "subtitle": "Why traditional billing HMIS systems fail at modern patient-facing outpatient engagement.",
        "comparison_table": [
            ("Patient Access Channel", "Requires calling reception or downloading low-adoption mobile apps (< 8% install rate).", "Native WhatsApp interaction with 98% reach and zero app downloads."),
            ("Scheduling Engine", "Static calendar grids requiring manual receptionist booking and phone confirmation.", "24/7 autonomous booking with atomic slot locks and automated UPI pre-payment."),
            ("Specialty & Procedure Discovery", "Hidden behind phone calls; zero self-service package information or transparent pricing.", "Interactive '✨ Our Treatments' and '🔍 Find by Concern' catalogs with transparent package pricing."),
            ("Waiting Room Flow", "Blind waiting in overcrowded lobbies; zero real-time token tracking for patients.", "Live WhatsApp queue tokens showing real-time position and doctor status."),
            ("Lab Report Delivery", "Trapped in counter printouts or complex web portals requiring forgotten passwords.", "Autonomous PDF delivery to WhatsApp with plain-English AI summaries.")
        ],
        "what_to_say": "Traditional hospital software was built for back-office accountants, not modern patients. Kriya AI doesn't replace your billing software—it provides the modern patient-facing front door that legacy systems lack.",
        "badge_title": "SEAMLESS SYNERGY",
        "badge_desc": "Kriya AI enhances your current HMIS investment by automating the patient interaction layer without touching legacy vendor code.",
        "badge_type": "amber"
    },
    # Slide 20 - CHATBOT VS HEALTHCARE OS
    {
        "page_num": "20 / 21",
        "tag": "CLINICAL SAFETY DEFENSE",
        "tag_type": "cyan",
        "title": "Chatbot vs. Enterprise Healthcare OS",
        "subtitle": "Why generic conversational tools introduce severe clinical liability and operational failure.",
        "comparison_table": [
            ("Architecture & Logic", "Stateless text generation with high prompt drift and hallucination risk.", "22-State Finite State Machine with deterministic database validations."),
            ("Clinical Safety Firewall", "No medical guardrails; creates malpractice liability by attempting clinical advice.", "Zero-LLM regex interceptor strictly blocking medical advice & prescriptions."),
            ("Slot Concurrency Locking", "Lacks database-level locking; severe double-booking risk during concurrent traffic.", "PostgreSQL ACID unique indexes mathematically preventing collisions."),
            ("Specialty Treatment Engines", "Incapable of managing structured multi-session packages, chair-time, or pre-care.", "Pre-seeded specialty treatment engines with doctor qualification mapping."),
            ("Diagnostic Integration", "Incapable of scraping legacy EMRs, running OCR, or matching fuzzy patient records.", "Automated Playwright daemons with Tesseract OCR and PII redaction.")
        ],
        "what_to_say": "Generic chatbots are toys; in a hospital environment, they are a malpractice lawsuit waiting to happen. Kriya AI is an engineered healthcare operating system with database-level concurrency locks and strict safety firewalls.",
        "badge_title": "LIABILITY MITIGATION",
        "badge_desc": "Our clinical safety firewall ensures your hospital is never exposed to unauthorized AI medical advice or prescription errors.",
        "badge_type": "amber"
    },
    # Slide 21 - IMPLEMENTATION & NEXT STEPS
    {
        "page_num": "21 / 21",
        "tag": "DEPLOYMENT & ACTION",
        "tag_type": "cyan",
        "title": "Rapid 14-Day Implementation & Executive Next Steps",
        "subtitle": "A battle-tested 14-day implementation methodology followed by a zero-risk pilot.",
        "split_columns": [
            {
                "title": "Turnkey 14-Day Implementation Roadmap",
                "icon": "🚀",
                "items": [
                    ("Days 1–3: Provisioning", "Meta WhatsApp WABA verification, multi-tenant database provisioning, admin portal credentials."),
                    ("Days 4–6: Roster & Specialty Setup", "Doctor consulting profiles, pre-seeded specialty treatment catalog review and activation, Razorpay gateway link."),
                    ("Days 7–10: EMR & LIMS Bridge", "Diagnostream Playwright setup, MocDoc/CallMedex sync testing, OCR parameter parsing validation."),
                    ("Days 11–14: Go-Live", "Reception staff console training, desk QR collateral placement, production launch with active engineering support.")
                ]
            },
            {
                "title": "Executive Experience & Pilot Opportunity",
                "icon": "✨",
                "items": [
                    ("Interactive WhatsApp Simulation", "Experience the 60-second multilingual booking, specialty treatment catalog, and live queue token flow firsthand."),
                    ("Live Admin Console Walkthrough", "Review doctor roster controls, specialty treatment pricing, and real-time revenue analytics."),
                    ("Single Department Pilot", "Deploy Kriya AI in a single high-volume OPD department or specialty clinic prior to hospital-wide rollout with zero operational risk.")
                ]
            }
        ],
        "what_to_say": "Thank you for your time. We invite your leadership and clinical teams to experience a live, hands-on demonstration of Kriya AI on WhatsApp today. Let us show you how we eliminate front-desk chaos and modernize your patient journey.",
        "badge_title": "OPEN FOR Q&A",
        "badge_desc": "We welcome your questions regarding our technical architecture, EMR integration daemons, or clinical safety guardrails.",
        "badge_type": "amber"
    }
]

def generate_html():
    html_out = []
    html_out.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Kriya AI — Executive Client Pitch Deck v2.0</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Outfit:wght@400;500;600;700;800&display=swap');

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background-color: #030712;
    color: #F8FAFC;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    -webkit-font-smoothing: antialiased;
}

@page {
    size: 1920px 1080px;
    margin: 0;
}

.slide {
    width: 1920px;
    height: 1080px;
    page-break-after: always;
    position: relative;
    background: radial-gradient(circle at 85% 15%, rgba(0, 180, 216, 0.08) 0%, transparent 45%),
                radial-gradient(circle at 15% 85%, rgba(16, 185, 129, 0.04) 0%, transparent 45%),
                linear-gradient(135deg, #070D1D 0%, #0A1428 50%, #060B18 100%);
    padding: 50px 80px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    overflow: hidden;
}

/* Slide Header */
.header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
}

.logo-group {
    display: flex;
    align-items: center;
    gap: 14px;
}

.logo-icon {
    width: 44px;
    height: 44px;
    border-radius: 10px;
    background: linear-gradient(135deg, #00B4D8 0%, #0077B6 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 20px rgba(0, 180, 216, 0.4);
    color: #fff;
    font-family: 'Outfit', sans-serif;
    font-weight: 800;
    font-size: 20px;
}

.logo-text h2 {
    font-family: 'Outfit', sans-serif;
    font-size: 20px;
    font-weight: 800;
    letter-spacing: 1.5px;
    color: #FFFFFF;
}

.logo-text p {
    font-size: 11px;
    letter-spacing: 1px;
    color: #00B4D8;
    text-transform: uppercase;
    font-weight: 600;
}

.page-badge {
    display: flex;
    align-items: center;
    gap: 12px;
}

.badge-tag {
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    background: rgba(0, 180, 216, 0.12);
    color: #00B4D8;
    border: 1px solid rgba(0, 180, 216, 0.3);
}

.badge-tag.crimson {
    background: rgba(239, 68, 68, 0.12);
    color: #EF4444;
    border-color: rgba(239, 68, 68, 0.3);
}

.page-num {
    font-family: 'Outfit', sans-serif;
    font-size: 15px;
    font-weight: 700;
    color: #64748B;
    letter-spacing: 1px;
}

/* Slide Title Section */
.title-section {
    margin-bottom: 24px;
}

.title-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 8px;
    color: #00B4D8;
}

.title-tag.crimson {
    color: #EF4444;
}

.slide-title {
    font-family: 'Outfit', sans-serif;
    font-size: 42px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #FFFFFF;
    margin-bottom: 8px;
    line-height: 1.15;
}

.slide-subtitle {
    font-size: 18px;
    color: #94A3B8;
    font-weight: 400;
    max-width: 1400px;
    line-height: 1.4;
}

/* Main Content Area */
.main-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    margin-bottom: 24px;
}

/* Grid Cards (4 columns) */
.cards-grid-4 {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 20px;
    height: 100%;
    align-items: stretch;
}

/* Grid Cards (6 items: 3 columns x 2 rows) */
.cards-grid-6 {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 18px;
    height: 100%;
}

.feature-card {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    backdrop-filter: blur(10px);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
}

.feature-card.alert {
    border-color: rgba(239, 68, 68, 0.3);
    background: rgba(30, 16, 26, 0.6);
}

.card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
}

.card-icon {
    font-size: 26px;
}

.card-pill {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 4px;
    background: rgba(0, 180, 216, 0.15);
    color: #38BDF8;
    border: 1px solid rgba(0, 180, 216, 0.3);
}

.feature-card h3 {
    font-family: 'Outfit', sans-serif;
    font-size: 20px;
    font-weight: 700;
    color: #FFFFFF;
    margin-bottom: 10px;
    line-height: 1.25;
}

.feature-card.alert h3 {
    color: #FCA5A5;
}

.feature-card p {
    font-size: 14px;
    color: #94A3B8;
    line-height: 1.5;
    flex: 1;
}

/* Split Columns (2 columns) */
.split-grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    height: 100%;
}

.column-card {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    padding: 24px 28px;
    display: flex;
    flex-direction: column;
}

.column-card.alert {
    border-color: rgba(239, 68, 68, 0.3);
    background: rgba(30, 16, 26, 0.6);
}

.column-card h3 {
    font-family: 'Outfit', sans-serif;
    font-size: 20px;
    font-weight: 700;
    color: #FFFFFF;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding-bottom: 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.column-card.alert h3 {
    color: #FCA5A5;
    border-bottom-color: rgba(239, 68, 68, 0.2);
}

.column-items {
    display: flex;
    flex-direction: column;
    gap: 14px;
    justify-content: space-around;
    flex: 1;
}

.col-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.col-item-title {
    font-size: 15px;
    font-weight: 600;
    color: #E2E8F0;
    display: flex;
    align-items: center;
    gap: 8px;
}

.col-item-desc {
    font-size: 13.5px;
    color: #94A3B8;
    line-height: 1.45;
    padding-left: 20px;
}

/* Split 3 Columns */
.split-grid-3 {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 20px;
    height: 100%;
}

.three-col-card {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    padding: 24px;
    display: flex;
    flex-direction: column;
}

.three-col-card h3 {
    font-family: 'Outfit', sans-serif;
    font-size: 20px;
    font-weight: 700;
    color: #FFFFFF;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.three-col-card ul {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1;
    justify-content: space-around;
}

.three-col-card li {
    font-size: 13.5px;
    color: #94A3B8;
    line-height: 1.45;
    position: relative;
    padding-left: 22px;
}

.three-col-card li::before {
    content: "✓";
    position: absolute;
    left: 0;
    color: #10B981;
    font-weight: 700;
}

/* Dual Flow Horizontal */
.dual-flow-container {
    display: flex;
    flex-direction: column;
    gap: 18px;
    height: 100%;
    justify-content: center;
}

.flow-box {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    padding: 16px 20px;
}

.flow-title {
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #00B4D8;
    margin-bottom: 10px;
}

.steps-row {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
}

.step-card {
    background: rgba(10, 20, 38, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.12);
    border-radius: 8px;
    padding: 10px 12px;
}

.step-card h5 {
    font-size: 12px;
    font-weight: 700;
    color: #38BDF8;
    margin-bottom: 4px;
}

.step-card p {
    font-size: 11px;
    color: #94A3B8;
    line-height: 1.35;
}

/* Metrics Row (5 columns) */
.metrics-row {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 16px;
    height: 100%;
    align-items: center;
}

.metric-card {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.18);
    border-radius: 12px;
    padding: 30px 20px;
    text-align: center;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    gap: 10px;
    height: 80%;
}

.metric-stat {
    font-family: 'Outfit', sans-serif;
    font-size: 46px;
    font-weight: 800;
    background: linear-gradient(135deg, #38BDF8 0%, #00B4D8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    line-height: 1;
}

.metric-label {
    font-size: 16px;
    font-weight: 700;
    color: #FFFFFF;
}

.metric-sub {
    font-size: 12px;
    color: #94A3B8;
    line-height: 1.4;
}

/* Metrics Quad + Sidebar (Slide 11) */
.metrics-quad-layout {
    display: grid;
    grid-template-columns: 1.2fr 1fr;
    gap: 24px;
    height: 100%;
}

.quad-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
}

.sidebar-box {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    justify-content: space-around;
}

.sidebar-item h4 {
    font-size: 15px;
    font-weight: 700;
    color: #FFFFFF;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
}

.sidebar-item p {
    font-size: 13px;
    color: #94A3B8;
    line-height: 1.4;
    padding-left: 20px;
}

/* Comparison Table (Slides 19, 20) */
.table-container {
    background: rgba(17, 27, 49, 0.7);
    border: 1px solid rgba(0, 180, 216, 0.15);
    border-radius: 12px;
    overflow: hidden;
    height: 100%;
    display: flex;
    flex-direction: column;
}

.comp-table {
    width: 100%;
    height: 100%;
    border-collapse: collapse;
}

.comp-table th {
    padding: 14px 20px;
    font-family: 'Outfit', sans-serif;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    text-align: left;
    background: rgba(10, 18, 36, 0.9);
    border-bottom: 1px solid rgba(0, 180, 216, 0.2);
}

.comp-table th:first-child { color: #94A3B8; width: 25%; }
.comp-table th:nth-child(2) { color: #F87171; width: 37.5%; }
.comp-table th:nth-child(3) { color: #38BDF8; width: 37.5%; }

.comp-table td {
    padding: 14px 20px;
    font-size: 13.5px;
    line-height: 1.45;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    vertical-align: middle;
}

.comp-table tr:last-child td { border-bottom: none; }
.comp-table td:first-child { font-weight: 600; color: #E2E8F0; }
.comp-table td:nth-child(2) { color: #94A3B8; background: rgba(239, 68, 68, 0.02); }
.comp-table td:nth-child(3) { color: #F1F5F9; background: rgba(0, 180, 216, 0.03); }

/* Slide Footer Bar */
.footer-row {
    display: grid;
    grid-template-columns: 2fr 1.3fr;
    gap: 20px;
    margin-top: 10px;
}

.footer-box {
    background: rgba(10, 19, 36, 0.85);
    border: 1px solid rgba(0, 180, 216, 0.2);
    border-radius: 8px;
    padding: 12px 18px;
    display: flex;
    align-items: flex-start;
    gap: 12px;
}

.footer-icon {
    font-size: 18px;
    line-height: 1.3;
}

.footer-text {
    font-size: 12.5px;
    color: #CBD5E1;
    line-height: 1.45;
}

.footer-text strong {
    color: #38BDF8;
    letter-spacing: 0.5px;
}

.footer-box.amber {
    border-color: rgba(245, 158, 11, 0.3);
    background: rgba(30, 22, 12, 0.6);
}

.footer-box.amber .footer-text strong {
    color: #FBBF24;
}
</style>
</head>
<body>
""")

    for idx, slide in enumerate(SLIDES_DATA):
        html_out.append(f'<div class="slide" id="slide-{idx+1}">')
        
        # Header
        html_out.append(f'''
        <div class="header-row">
            <div class="logo-group">
                <div class="logo-icon">K</div>
                <div class="logo-text">
                    <h2>KRIYA AI</h2>
                    <p>HOSPITAL OPERATING SYSTEM • ENGINEERED BY XYLARC AI</p>
                </div>
            </div>
            <div class="page-badge">
                <div class="badge-tag {slide.get('tag_type', 'cyan')}">{slide['tag']}</div>
                <div class="page-num">{slide['page_num']}</div>
            </div>
        </div>
        ''')
        
        # Title Section
        html_out.append(f'''
        <div class="title-section">
            <div class="title-tag {slide.get('tag_type', 'cyan')}">◈ {slide['tag']}</div>
            <h1 class="slide-title">{slide['title']}</h1>
            <p class="slide-subtitle">{slide['subtitle']}</p>
        </div>
        ''')
        
        # Main Content
        html_out.append('<div class="main-content">')
        
        if "cards" in slide:
            grid_class = "cards-grid-6" if len(slide["cards"]) > 4 else "cards-grid-4"
            html_out.append(f'<div class="{grid_class}">')
            for c in slide["cards"]:
                alert_cls = "alert" if c.get("alert") else ""
                badge_html = f'<span class="card-pill">{c["badge"]}</span>' if "badge" in c else ''
                html_out.append(f'''
                <div class="feature-card {alert_cls}">
                    <div class="card-top">
                        <span class="card-icon">{c.get('icon', '✦')}</span>
                        {badge_html}
                    </div>
                    <h3>{c['title']}</h3>
                    <p>{c['desc']}</p>
                </div>
                ''')
            html_out.append('</div>')
            
        elif "split_columns" in slide:
            html_out.append('<div class="split-grid-2">')
            for col in slide["split_columns"]:
                alert_cls = "alert" if col.get("alert") else ""
                html_out.append(f'''
                <div class="column-card {alert_cls}">
                    <h3><span>{col.get('icon', '✦')}</span> {col['title']}</h3>
                    <div class="column-items">
                ''')
                for item_title, item_desc in col["items"]:
                    html_out.append(f'''
                        <div class="col-item">
                            <div class="col-item-title"><span>◈</span> {item_title}</div>
                            <div class="col-item-desc">{item_desc}</div>
                        </div>
                    ''')
                html_out.append('</div></div>')
            html_out.append('</div>')
            
        elif "split_three" in slide:
            html_out.append('<div class="split-grid-3">')
            for col in slide["split_three"]:
                html_out.append(f'''
                <div class="three-col-card">
                    <h3><span>{col.get('icon', '✦')}</span> {col['title']}</h3>
                    <ul>
                ''')
                for item in col["items"]:
                    html_out.append(f'<li>{item}</li>')
                html_out.append('</ul></div>')
            html_out.append('</div>')
            
        elif "dual_flow" in slide:
            df = slide["dual_flow"]
            html_out.append('<div class="dual-flow-container">')
            # Flow 1
            html_out.append(f'''
            <div class="flow-box">
                <div class="flow-title">◈ {df['flow1_title']}</div>
                <div class="steps-row">
            ''')
            for step_title, step_desc in df["flow1_steps"]:
                html_out.append(f'''
                <div class="step-card">
                    <h5>{step_title}</h5>
                    <p>{step_desc}</p>
                </div>
                ''')
            html_out.append('</div></div>')
            # Flow 2
            html_out.append(f'''
            <div class="flow-box">
                <div class="flow-title">✨ {df['flow2_title']}</div>
                <div class="steps-row">
            ''')
            for step_title, step_desc in df["flow2_steps"]:
                html_out.append(f'''
                <div class="step-card">
                    <h5>{step_title}</h5>
                    <p>{step_desc}</p>
                </div>
                ''')
            html_out.append('</div></div></div>')
            
        elif "metrics_quad" in slide:
            html_out.append('<div class="metrics-quad-layout">')
            html_out.append('<div class="quad-grid">')
            for m in slide["metrics_quad"]:
                html_out.append(f'''
                <div class="metric-card">
                    <div class="metric-stat">{m['stat']}</div>
                    <div class="metric-label">{m['label']}</div>
                    <div class="metric-sub">{m['sub']}</div>
                </div>
                ''')
            html_out.append('</div>')
            html_out.append('<div class="sidebar-box">')
            for title, desc in slide["sidebar_points"]:
                html_out.append(f'''
                <div class="sidebar-item">
                    <h4><span>✦</span> {title}</h4>
                    <p>{desc}</p>
                </div>
                ''')
            html_out.append('</div></div>')
            
        elif "metrics_row" in slide:
            html_out.append('<div class="metrics-row">')
            for m in slide["metrics_row"]:
                html_out.append(f'''
                <div class="metric-card">
                    <div class="metric-stat">{m['stat']}</div>
                    <div class="metric-label">{m['label']}</div>
                    <div class="metric-sub">{m['sub']}</div>
                </div>
                ''')
            html_out.append('</div>')
            
        elif "comparison_table" in slide:
            headers = ["OPERATIONAL DIMENSION", "GENERIC BOT / LEGACY HMIS", "KRIYA AI HEALTHCARE OS"]
            if "vs. Conventional" in slide["title"]:
                headers[1] = "LEGACY HOSPITAL SOFTWARE (HMIS)"
                headers[2] = "KRIYA AI OPERATIONS LAYER"
            html_out.append(f'''
            <div class="table-container">
                <table class="comp-table">
                    <thead>
                        <tr>
                            <th>{headers[0]}</th>
                            <th>{headers[1]}</th>
                            <th>{headers[2]}</th>
                        </tr>
                    </thead>
                    <tbody>
            ''')
            for dim, legacy, kriya in slide["comparison_table"]:
                html_out.append(f'''
                <tr>
                    <td>{dim}</td>
                    <td>{legacy}</td>
                    <td>{kriya}</td>
                </tr>
                ''')
            html_out.append('</tbody></table></div>')
            
        html_out.append('</div>') # end main-content
        
        # Footer
        html_out.append(f'''
        <div class="footer-row">
            <div class="footer-box">
                <span class="footer-icon">💬</span>
                <div class="footer-text"><strong>WHAT TO SAY:</strong> "{slide['what_to_say']}"</div>
            </div>
            <div class="footer-box {slide.get('badge_type', 'cyan')}">
                <span class="footer-icon">🛡️</span>
                <div class="footer-text"><strong>{slide['badge_title']}:</strong> {slide['badge_desc']}</div>
            </div>
        </div>
        ''')
        
        html_out.append('</div>') # end slide
        
    html_out.append("""</body>
</html>""")
    
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(html_out))
    print(f"Generated HTML slide deck at: {HTML_PATH}")

def render_pdf():
    print("Launching Chromium via Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"file:///{HTML_PATH.replace(os.sep, '/')}")
        page.wait_for_load_state("networkidle")
        
        print(f"Printing 21 slides to PDF: {PDF_PATH}...")
        page.pdf(
            path=PDF_PATH,
            width="1920px",
            height="1080px",
            print_background=True,
            margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"}
        )
        browser.close()
    print(f"Successfully generated PDF at: {PDF_PATH}")

if __name__ == "__main__":
    generate_html()
    render_pdf()
