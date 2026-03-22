-- Patients table
CREATE TABLE patients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100),
    language VARCHAR(10) DEFAULT NULL,
    opted_in BOOLEAN DEFAULT false,
    opted_in_at TIMESTAMPTZ,
    opted_out_at TIMESTAMPTZ,
    data_consent BOOLEAN DEFAULT NULL,
    data_consent_at TIMESTAMPTZ,
    visit_count INTEGER DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Appointments table
CREATE TABLE appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID REFERENCES patients(id) ON DELETE CASCADE,
    patient_phone VARCHAR(20) NOT NULL,
    patient_name VARCHAR(100),
    department VARCHAR(50) NOT NULL,
    doctor_name VARCHAR(100),
    appointment_date DATE NOT NULL,
    appointment_time TIME NOT NULL,
    symptoms TEXT,
    status VARCHAR(20) DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show')),
    reminder_24h_sent BOOLEAN DEFAULT false,
    reminder_2h_sent BOOLEAN DEFAULT false,
    followup_sent BOOLEAN DEFAULT false,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Conversations (session state)
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20) UNIQUE NOT NULL,
    state VARCHAR(50) DEFAULT 'idle',
    context JSONB DEFAULT '{}',
    session_expires_at TIMESTAMPTZ,
    booking_context_expires_at TIMESTAMPTZ,
    last_message_at TIMESTAMPTZ DEFAULT NOW(),
    last_processed_message_id VARCHAR(100),
    unknown_intent_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Analytics events
CREATE TABLE analytics_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(20),
    event_type VARCHAR(50) NOT NULL,
    department VARCHAR(50),
    intent VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Doctors (configurable per hospital)
CREATE TABLE doctors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    specialization VARCHAR(50) NOT NULL,
    department VARCHAR(50) NOT NULL,
    available_days VARCHAR(100) DEFAULT 'Mon,Tue,Wed,Thu,Fri',
    morning_slots JSONB DEFAULT '["09:00","09:30","10:00","10:30","11:00","11:30"]',
    evening_slots JSONB DEFAULT '["17:00","17:30","18:00","18:30"]',
    is_active BOOLEAN DEFAULT true,
    consultation_fee INTEGER DEFAULT 500,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Doctor leaves
CREATE TABLE doctor_leaves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doctor_name VARCHAR(100) NOT NULL,
    leave_date DATE NOT NULL,
    leave_type VARCHAR(20) DEFAULT 'full'
        CHECK (leave_type IN ('full', 'half_morning', 'half_evening')),
    reason VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(doctor_name, leave_date)
);

CREATE INDEX idx_leaves_doctor_date ON doctor_leaves(doctor_name, leave_date);

-- Hospital public holidays
CREATE TABLE hospital_holidays (
    holiday_date DATE PRIMARY KEY,
    name VARCHAR(100)
);

-- Pre-seed common Indian national holidays
INSERT INTO hospital_holidays (holiday_date, name) VALUES
('2026-01-26', 'Republic Day'),
('2026-08-15', 'Independence Day'),
('2026-10-02', 'Gandhi Jayanti');

-- Seed doctors
-- Add superior feature columns to doctors
ALTER TABLE doctors ADD COLUMN experience_years INTEGER DEFAULT 0;
ALTER TABLE doctors ADD COLUMN qualifications VARCHAR(200);
ALTER TABLE doctors ADD COLUMN languages_spoken VARCHAR(100) DEFAULT 'English,Hindi,Telugu';
ALTER TABLE doctors ADD COLUMN rating DECIMAL(2,1) DEFAULT 4.5;
ALTER TABLE doctors ADD COLUMN fun_fact VARCHAR(200);

-- Add booking reference to appointments
ALTER TABLE appointments ADD COLUMN booking_ref VARCHAR(20) UNIQUE;
CREATE INDEX idx_appointments_booking_ref ON appointments(booking_ref);

INSERT INTO doctors (name, specialization, department, experience_years, qualifications, rating, fun_fact) VALUES
('Dr. Priya Sharma',  'General Physician',   'General Medicine', 8,  'MBBS, MD General Medicine',         4.7, 'Treated 10,000+ outpatients'),
('Dr. Arjun Reddy',   'Cardiologist',        'Cardiology',       14, 'MBBS, MD, DM Cardiology',           4.8, 'Performed 500+ cardiac procedures'),
('Dr. Meena Patel',   'Dentist',             'Dental',           6,  'BDS, MDS Oral Surgery',             4.6, 'Specialist in painless dentistry'),
('Dr. Suresh Kumar',  'Orthopedic Surgeon',  'Orthopedics',      12, 'MBBS, MS Orthopedics',              4.7, 'Expert in joint replacement'),
('Dr. Anita Singh',   'Gynecologist',        'Gynecology',       10, 'MBBS, MD Obstetrics & Gynecology',  4.9, '2000+ safe deliveries'),
('Dr. Ravi Nair',     'Pediatrician',        'Pediatrics',       9,  'MBBS, MD Pediatrics, DCH',          4.8, 'Child-friendly consultations');

-- Indexes
CREATE INDEX idx_appointments_patient_phone ON appointments(patient_phone);
CREATE INDEX idx_appointments_date ON appointments(appointment_date);
CREATE INDEX idx_appointments_status ON appointments(status);
CREATE INDEX idx_conversations_phone ON conversations(phone);
CREATE INDEX idx_analytics_event_type ON analytics_events(event_type);
CREATE INDEX idx_analytics_created_at ON analytics_events(created_at);
