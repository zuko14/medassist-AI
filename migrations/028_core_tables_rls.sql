-- Security audit finding: the 7 core tables from 001_initial_schema.sql
-- (patients, appointments, conversations, doctors, doctor_leaves,
-- hospital_holidays, analytics_events) were never given RLS, unlike every
-- table added from migration 002 onward. The backend's Supabase client
-- (app/database.py) uses the service_role key exclusively, which bypasses
-- RLS regardless of policies — so this is a defense-in-depth fix, not a
-- response to an active breach. Same service_role-only pattern as every
-- other table in this schema (see migrations/009, 013, 027).

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_patients" ON patients
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_appointments" ON appointments
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_conversations" ON conversations
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE doctors ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_doctors" ON doctors
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE doctor_leaves ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_doctor_leaves" ON doctor_leaves
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE hospital_holidays ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_hospital_holidays" ON hospital_holidays
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE analytics_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_analytics_events" ON analytics_events
    FOR ALL TO service_role USING (true) WITH CHECK (true);
