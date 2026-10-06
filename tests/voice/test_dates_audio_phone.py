import base64, struct
from datetime import date, datetime
import pytest
from app.voice.dates import IST, parse_date, parse_time_period, parse_clock_time, MORNING, EVENING, AFTERNOON
from app.voice.audio import strip_wav_header, PcmChunker
from app.voice.phone import to_e164, mask

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=IST)  # a Tuesday

@pytest.mark.parametrize("text, expected", [
    ("Naaku repu heart doctor appointment kavali", date(2026, 10, 7)),
    ("నాకు రేపు కార్డియాలజీ డాక్టర్ అపాయింట్మెంట్ కావాలి", date(2026, 10, 7)),
    ("मुझे कल कार्डियोलॉजी का अपॉइंटमेंट चाहिए", date(2026, 10, 7)),
    ("I need a cardiologist tomorrow morning.", date(2026, 10, 7)),
    ("Tomorrow... No, actually Friday.", date(2026, 10, 9)),
    ("ellundi", date(2026, 10, 8)),
    ("day after tomorrow", date(2026, 10, 8)),
    ("parso", date(2026, 10, 8)),
    ("ee roju evening", date(2026, 10, 6)),
    ("ఆదివారం", date(2026, 10, 11)),
    ("next tuesday", date(2026, 10, 13)),
    ("tuesday", date(2026, 10, 6)),
    ("15th", date(2026, 10, 15)),
    ("5th", date(2026, 11, 5)),
    ("7th october", date(2026, 10, 7)),
    ("october 20", date(2026, 10, 20)),
    ("20/10", date(2026, 10, 20)),
    ("1/1", date(2027, 1, 1)),
    ("kalyan nagar branch", None),
    ("I want an appointment", None),
    ("10:30 ki book cheyyandi", None),
])
def test_parse_date(text, expected):
    assert parse_date(text, NOW) == expected

@pytest.mark.parametrize("text, expected", [
    ("morning", MORNING), ("udayam", MORNING), ("సాయంత్రం", EVENING), ("No, evening.", EVENING),
    ("morning kaadu evening", EVENING), ("dopahar", AFTERNOON), ("I am free", None), ("शाम को", EVENING),
])
def test_parse_period(text, expected):
    assert parse_time_period(text) == expected

@pytest.mark.parametrize("text, expected", [
    ("10:30", "10:30"), ("10.30 ki", "10:30"), ("4 pm", "16:00"), ("saayantram 5", "17:00"),
    ("5:30", "17:30"), ("9 am", "09:00"), ("second one", None), ("2", None), ("15th", None),
    ("ఉదయం 11", "11:00"), ("20/10", None),
])
def test_clock(text, expected):
    assert parse_clock_time(text) == expected

def test_wav_header_stripped_and_raw_passthrough():
    pcm = b"\x01\x02" * 100
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + struct.pack("<I", len(pcm))
    assert strip_wav_header(hdr + pcm) == pcm
    assert strip_wav_header(pcm) == pcm

def test_chunker_legal_sizes():
    c = PcmChunker()
    out = list(c.feed(b"\x00" * 10000)) + list(c.flush())
    assert all(len(x) % 320 == 0 and len(x) >= 3200 for x in out)
    assert sum(len(x) for x in out) >= 10000
    with pytest.raises(ValueError):
        PcmChunker(3000)

@pytest.mark.parametrize("raw, e164", [("09876543210", "+919876543210"), ("+91 98765-43210", "+919876543210"),
    ("919876543210", "+919876543210"), ("9876543210", "+919876543210"), ("abc", None), ("", None)])
def test_phone(raw, e164):
    assert to_e164(raw) == e164

def test_mask():
    assert mask("+919876543210") == "+91XXXXXX3210"
