CREATE TABLE IF NOT EXISTS lab_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_phone TEXT NOT NULL,
    patient_name TEXT,
    report_name TEXT NOT NULL,
    report_type TEXT DEFAULT 'General',
    file_path TEXT NOT NULL,
    ai_summary TEXT,
    has_abnormal_values BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'pending',
    uploaded_by TEXT DEFAULT 'admin',
    uploaded_at TIMESTAMPTZ DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_lab_reports_phone ON lab_reports(patient_phone);
CREATE INDEX IF NOT EXISTS idx_lab_reports_uploaded ON lab_reports(uploaded_at DESC);

CREATE TABLE IF NOT EXISTS prescriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_phone TEXT NOT NULL,
    patient_name TEXT,
    medicine_name TEXT NOT NULL,
    dosage TEXT NOT NULL,
    frequency TEXT NOT NULL,
    reminder_times TEXT[] NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prescriptions_phone ON prescriptions(patient_phone);
CREATE INDEX IF NOT EXISTS idx_prescriptions_active ON prescriptions(is_active, end_date);
