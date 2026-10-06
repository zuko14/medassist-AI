import pytest
from app.voice.lexicon import (find_specialties, resolve_department, romanize, match_doctors,
                           tenant_department_for, apply_pronunciations)


@pytest.mark.parametrize("text, expected", [
    ("Naaku repu heart doctor appointment kavali", ["CARDIOLOGY"]),
    ("హార్ట్ డాక్టర్", ["CARDIOLOGY"]), ("గుండె డాక్టర్", ["CARDIOLOGY"]), ("cardio doctor", ["CARDIOLOGY"]),
    ("నాకు రేపు కార్డియాలజీ డాక్టర్ అపాయింట్మెంట్ కావాలి", ["CARDIOLOGY"]),
    ("मुझे कल कार्डियोलॉजी का अपॉइंटमेंट चाहिए", ["CARDIOLOGY"]),
    ("I need a cardiologist tomorrow morning.", ["CARDIOLOGY"]),
    ("I want an appointment", []),  # "ent" inside "appointment" must not match ENT
    ("pillala doctor", ["PEDIATRICS"]), ("gundeki doctor", ["CARDIOLOGY"]),
    ("ENT doctor", ["ENT"]), ("bone and skin", ["ORTHOPEDICS", "DERMATOLOGY"]),
])
def test_find_specialties(text, expected):
    assert find_specialties(text) == expected


def test_resolve_department_uses_clinic_names():
    assert resolve_department("CARDIOLOGY", ["General Medicine", "Cardiologist"]) == "Cardiologist"
    assert resolve_department("CARDIOLOGY", ["General Medicine"]) is None


def test_tenant_synonym():
    e = [{"kind": "specialty_synonym", "phrase": "dil ka opd", "canonical": "Heart Care"}]
    assert tenant_department_for("Dil ka OPD kal", e, ["Heart Care"]) == "Heart Care"
    assert tenant_department_for("Dil ka OPD kal", e, ["Other"]) is None


@pytest.mark.parametrize("src, expected", [("శ్రీనివాస్", "srinivas"), ("श्रीनिवास", "shrinivas"),
                                           ("రమేష్", "ramesh"), ("राहुल", "rahul")])
def test_romanize(src, expected):
    assert romanize(src) == expected


DOCS = [{"id": "1", "name": "Dr. Srinivas Rao", "department": "Cardiology"},
        {"id": "2", "name": "Dr. Lakshmi Prasanna", "department": "Gynecology"},
        {"id": "3", "name": "Dr. Ramesh Kumar", "department": "Orthopedics"}]


@pytest.mark.parametrize("text, ids", [
    ("Dr Srinivas garu repu unnara", ["1"]), ("డాక్టర్ శ్రీనివాస్ గారు", ["1"]), ("डॉक्टर श्रीनिवास", ["1"]),
    ("lakshmi madam", ["2"]), ("naaku repu heart doctor kavali", []), ("ramesh", ["3"]),
])
def test_match_doctors(text, ids):
    assert [d["id"] for d in match_doctors(text, DOCS)] == ids


def test_doctor_alias():
    e = [{"kind": "doctor_alias", "phrase": "pedda doctor", "canonical": "Dr. Ramesh Kumar"}]
    assert [d["id"] for d in match_doctors("pedda doctor kavali", DOCS, e)] == ["3"]


def test_pronunciation():
    e = [{"kind": "pronunciation", "phrase": "KIMS", "canonical": "కిమ్స్"}]
    assert apply_pronunciations("Welcome to KIMS hospital", e) == "Welcome to కిమ్స్ hospital"
