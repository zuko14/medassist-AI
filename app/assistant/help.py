"""Static help index for "how do I…" questions (KK-FR-015). Zero tokens, no writes.

Each answer names a button that really exists on that page; the registry test
checks it against admin/index.html so the help cannot drift from the panel.
Matching a question to an entry is the router's job (KK-P1-06).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HelpEntry:
    keywords: tuple[str, ...]
    page: str
    button: str
    answer: str


HELP: tuple[HelpEntry, ...] = (
    HelpEntry(("leave", "chutti", "selavu", "సెలవు", "छुट्टी"), "leaves", "Add Leave",
              "Open Leaves, choose the doctor and dates, then press Add Leave."),
    HelpEntry(("doctor", "డాక్టర్", "डॉक्टर"), "doctors", "Add Doctor",
              "Open Doctors, fill in the doctor's details and hours, then press Add Doctor."),
    HelpEntry(("holiday", "హాలిడే", "अवकाश"), "holidays", "Add Holiday",
              "Open Holidays, pick the date, then press Add Holiday."),
    HelpEntry(("treatment", "procedure"), "treatments", "Add Treatment",
              "Open Treatments and press Add Treatment."),
    HelpEntry(("register", "new patient", "రిజిస్టర్", "रजिस्टर"), "opddesk", "Register Patient",
              "Open Front Desk and use Register Patient."),
    HelpEntry(("connector", "sync reports"), "connectors", "Run Now",
              "Open Report Connector and press Run Now to sync reports immediately."),
    # KK-P4-09 hand-offs (owner, 2026-10-15): these need a person's choice on the page.
    HelpEntry(("price list", "ధరల జాబితా", "रेट लिस्ट"), "labtests", "Import Price List",
              "Open Test Catalogue, press Import Price List and choose the file. You see every change "
              "before anything is saved."),
    HelpEntry(("duplicate", "duplicates", "clean up", "cleanup"), "labtests", "Clean-Up Suggestions",
              "Open Test Catalogue and press Clean-Up Suggestions to choose which duplicates to delete and "
              "which prices to correct."),
    HelpEntry(("description", "test details"), "labtests", "Add Lab Test",
              "Open Test Catalogue, edit the test (or press Add Lab Test), use Generate with AI next to "
              "Details, check the draft, then save."),
    HelpEntry(("treatment description",), "treatments", "Generate all 3 languages",
              "Open Treatments, edit the treatment and press Generate all 3 languages; check the draft, then save."),
)

#: Instructions Kriya hands to the page instead of doing (H tier, KK-P4-09): trigger -> help button.
HANDOFFS: tuple[tuple[str, str], ...] = (
    ("import+price list", "Import Price List"), ("upload+price list", "Import Price List"),
    ("from+price list", "Import Price List"), ("delete+duplicate", "Clean-Up Suggestions"),
    ("remove+duplicate", "Clean-Up Suggestions"), ("merge+duplicate", "Clean-Up Suggestions"),
    ("write+description", "Add Lab Test"), ("generate+description", "Add Lab Test"),
    ("write+description", "Generate all 3 languages"), ("generate+description", "Generate all 3 languages"),
)
