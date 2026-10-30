"""Kriya Ketu: requests as speech-to-text writes them (wake word, fillers, punctuation, "Doctor" / "Dr.",
code-mixed Telugu / Hindi). VOICE_SET was used to tune the rules; HELD_OUT was written after and run
once, so it measures the router on unseen wording. Safety rows must never become an action.
"""

from datetime import datetime

import pytest

from app.assistant import rules
from app.assistant.registry import REGISTRY
from app.voice.dates import IST

DOC = [{"id": "d1", "name": "Dr. Srinivas Rao"}, {"id": "d3", "name": "Dr. Anitha"}, {"id": "d4", "name": "Dr. Meena Patel"}]
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=IST)

VOICE_SET = [
 ("Kriya, open the appointments page.", "nav:appointments"),
 ("Umm, show me today's appointments please.", "appointments.search"),
 ("Doctor Anitha is on leave tomorrow.", "doctor_leave.create"),
 ("Dr. Anitha leave tomorrow", "doctor_leave.create"),
 ("Add leave for Doctor Srinivas Rao on 3rd November.", "doctor_leave.create"),
 ("Put Dr Meena Patel on leave from November 3 to November 5.", "doctor_leave.create"),
 ("Anitha madam ki kal chutti hai.", "doctor_leave.create"),
 ("Doctor Anitha ki kal chhutti hai", "doctor_leave.create"),
 ("Anitha doctor ki repu selavu.", "doctor_leave.create"),
 ("డాక్టర్ అనిత రేపు సెలవు", "doctor_leave.create"),
 ("डॉक्टर अनिता कल छुट्टी पर हैं", "doctor_leave.create"),
 ("How many beds are free right now?", "ipd.census"),
 ("Kitne beds khali hain?", "ipd.census"),
 ("Show the bed occupancy.", "ipd.census"),
 ("Who is admitted?", "ipd.inpatients"),
 ("How many samples are pending for collection?", "orders.to_collect"),
 ("Any critical results?", "lis.pending_signoff"),
 ("Who is there in casualty?", "emergency.board"),
 ("What is the price of X-ray chest?", "services.prices"),
 ("What's the IPD outstanding?", "ipd_finance.receivables"),
 ("Show today's collection.", "opd.collections"),
 ("How much did we collect today?", "opd.collections"),
 ("Show unpaid bills.", "opd.unpaid"),
 ("List the upcoming holidays.", "holidays.list"),
 ("Add a holiday on 14th November for Children's Day.", "holiday.create"),
 ("Change Doctor Meena's consultation fee to six hundred.", "doctor.fee"),
 ("What is Dr Anitha's fee?", "doctors.list"),
 ("Show me the doctor list.", "nav:doctors"),
 ("Price of CBC test.", "lab_tests.list"),
 ("What is the rate of thyroid profile test?", "lab_tests.list"),
 ("Show pending lab reports.", "reports.queue"),
 ("Show WhatsApp leads.", "nav:leads"),
 ("Find patient Ravi Kumar.", "patients.search"),
 ("Search patient 98765 43210.", "patients.search"),
 ("Register a new patient Ravi Kumar, phone 98765 43210.", "patient.register"),
 ("Check in Ravi Kumar.", "appointment.check_in"),
 ("Ravi Kumar has arrived.", "appointment.check_in"),
 ("Show the OPD queue.", "opd.queue"),
 ("Admit a patient.", "ipd.admit"),
 ("Discharge the patient in bed 12.", "ipd.discharge_start"),
 ("Record vitals for bed 5.", "ipd.vitals"),
 ("Rapid admit, road accident case.", "emergency.register"),
 ("Register an emergency patient.", "emergency.register"),
 ("Collect sample for Ravi.", "orders.collect"),
 ("Sign the report for Meena.", "lis.sign"),
 ("Issue a credit note on bill 104.", "billing.credit_note"),
 ("Download GSTR-1 for October.", "billing.gstr1"),
 ("Give a medical certificate to Ravi.", "documents.certificate"),
 ("Take consent for surgery.", "documents.consent"),
 ("Take an advance deposit of 20,000 for bed 12.", "ipd_finance.deposit"),
 ("Add a new staff login for the receptionist.", "staff.create"),
 ("Open dashboard.", "nav:dashboard"),
 ("Go to OPD billing.", "nav:opdbilling"),
 ("Open casualty.", "nav:casualty"),
 ("Open lab sign off.", "nav:lisqueue"),
 ("Show no-show rate.", "stats.summary"),
 ("What's the summary for this month?", "stats.summary"),
 ("Hey Kriya, how many appointments today?", "appointments.search"),
 ("OK so, uh, show today's appointments.", "appointments.search"),
 ("Show appointments for tomorrow.", "appointments.search"),
 ("Aaj ke appointments dikhao.", "appointments.search"),
 ("Ee roju appointments chupinchu.", "appointments.search"),
 ("Cancel Ravi Kumar's appointment.", "appointment.cancel"),
]

HELD_OUT = [
 ("Kriya Ketu, how many beds are empty?", "ipd.census"),
 ("Hey Kriya, show the casualty board.", "emergency.board"),
 ("Okay so how many free beds do we have", "ipd.census"),
 ("Dr Srinivas Rao will be on leave on Monday.", "doctor_leave.create"),
 ("Mark leave for Dr. Meena Patel next Friday.", "doctor_leave.create"),
 ("Meena madam repu chutti", "doctor_leave.create"),
 ("Show tomorrow's appointments.", "appointments.search"),
 ("Kal ke appointments dikhao", "appointments.search"),
 ("Who is waiting in the queue?", "opd.queue"),
 ("What is today's collection?", "opd.collections"),
 ("Aaj ka collection kitna hua?", "opd.collections"),
 ("Show bills that are not paid.", "opd.unpaid"),
 ("Which patients are admitted now?", "ipd.inpatients"),
 ("Who is waiting for a bed?", "ipd.waiting"),
 ("Show pending discharges.", "ipd.discharges"),
 ("How many critical values are pending?", "lis.pending_signoff"),
 ("Show reports pending sign off.", "lis.pending_signoff"),
 ("Show pending samples.", "orders.to_collect"),
 ("What are the charges for dressing?", "services.prices"),
 ("How much is due from TPA?", "ipd_finance.receivables"),
 ("Upcoming holidays?", "holidays.list"),
 ("Find patient Sita.", "patients.search"),
 ("Open front desk.", "nav:opddesk"),
 ("Take me to the bed board.", "nav:ipdboard"),
 ("Open service catalogue.", "nav:tariffs"),
 ("Issue a fitness certificate.", "documents.certificate"),
 ("Record an MLC.", "emergency.mlc"),
 ("Sign off the CBC report.", "lis.sign"),
 ("Enter results for the LFT.", "lis.enter_results"),
 ("Collect the sample for bed 4.", "orders.collect"),
 # safety: must NOT become an action
 ("Don't cancel Ravi's appointment.", "rejected:negated"),
 ("What if Dr Anitha takes leave tomorrow?", "rejected:hypothetical"),
 ("Which medicine should I give for fever?", "rejected:clinical"),
 ("What dose of paracetamol?", "rejected:clinical"),
 ("Sign all reports.", "!lis.sign"),
 ("Discharge all patients.", "!ipd.discharge"),
 ("the the uh", "!any"),
 ("hmm", "!any"),
 ("Kriya", "!any"),
 ("Thank you.", "!any"),
]


def _got(r):
    return (f"nav:{r.page}" if r.kind == "nav" else f"rejected:{r.error_code}" if r.kind == "rejected"
            else (r.action_id or r.kind))


@pytest.mark.parametrize("text, expect", VOICE_SET + HELD_OUT, ids=[t for t, _ in VOICE_SET + HELD_OUT])
def test_spoken_request_routes(text, expect):
    r = rules.route(text, doctors=DOC, now=NOW)
    if expect == "!any":
        assert r.action_id is None, r
    elif expect.startswith("!"):
        assert r.action_id != expect[1:] and not (r.kind == "action" and REGISTRY[r.action_id].risk != "R0"), r
    else:
        assert _got(r) == expect, r


@pytest.mark.parametrize("text, rest", [
    ("Kriya, open leads", "open leads"), ("Hey Kriya Ketu: show beds", "show beds"),
    ("ok so, uh, show appointments", "show appointments"), ("Umm please open dashboard", "open dashboard"),
    ("Kriya", ""), ("so", "so"), ("sonia's appointment", "sonia's appointment"), ("open kriya settings", "open kriya settings"),
])
def test_wake_word_and_fillers_are_dropped_only_at_the_start(text, rest):
    assert rules.strip_spoken_lead(text) == rest
