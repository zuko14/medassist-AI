"""What THIS hospital's receptionist offers, decided by its plan: the same rule WhatsApp uses.

A diagnostic centre's receptionist books tests and never offers a doctor; a clinic without
lab booking never takes a test booking; a hospital with both offers both. The caller's words
never widen this: "book an appointment" at a diagnostic centre is a test booking, and a
request for something this hospital does not offer is answered honestly with what it does.

Mirrors app/services/conversation.py (_is_diagnostics_only, _send_help_guide):
  labs     = has_feature(clinic, "lab_test_booking")
  doctors  = has_feature(clinic, "booking") and not (labs and the clinic has no doctors)
Plan data comes from the clinic row (CallContext), never from the call.
"""

from dataclasses import dataclass
from typing import Optional

SCOPE_VERSION = "scope-2026.11.03"


@dataclass(frozen=True)
class Scope:
    doctors: bool = True      # consultations: book, reschedule, doctor fees
    labs: bool = True         # lab tests / scans / packages: price, book

    @property
    def kind(self) -> str:
        if self.doctors and self.labs:
            return "hospital"
        if self.labs:
            return "diagnostic"
        if self.doctors:
            return "clinic"
        return "none"


#: Everything on: the behaviour before plan scoping, and the default wherever no clinic is known
#: (unit tests, the eval harness) or a read failed. A read failure never narrows what a caller gets.
FULL = Scope()


def needs_doctor_count(clinic: Optional[dict]) -> bool:
    """Only a plan with BOTH consultations and lab booking needs the doctor list to decide."""
    from app.services.tenant import has_feature
    return bool(clinic) and has_feature(clinic, "booking") and has_feature(clinic, "lab_test_booking")


def scope_for(clinic: Optional[dict], doctors: Optional[list] = None) -> Scope:
    """doctors: the clinic's active doctors, or None when unknown (read failed / not needed)."""
    if not clinic:
        return FULL
    from app.services.tenant import PLAN_FEATURES, has_feature
    if clinic.get("plan") not in PLAN_FEATURES:
        return FULL      # a plan slug this build does not know: never narrower than before
    labs = has_feature(clinic, "lab_test_booking")
    diagnostics_only = labs and doctors is not None and not doctors
    return Scope(doctors=has_feature(clinic, "booking") and not diagnostics_only, labs=labs)


# Workflows that need a capability, and what serves the same request where it is missing.
_NEEDS_DOCTORS = {"BOOKING": "LAB_BOOKING", "FEES": "LAB_PRICE"}
_NEEDS_LABS = frozenset({"LAB_BOOKING", "LAB_PRICE"})


def route(wf: str, scope: Scope) -> str:
    """The workflow that serves `wf` in this hospital, or NOT_OFFERED (said honestly, never guessed).
    RESCHEDULE is left alone: it only ever acts on the caller's own existing booking."""
    if wf in _NEEDS_DOCTORS and not scope.doctors:
        return _NEEDS_DOCTORS[wf] if scope.labs else "NOT_OFFERED"
    if wf in _NEEDS_LABS and not scope.labs:
        return "NOT_OFFERED"
    return wf
