import base64
import json

import pytest

from app.voice import exotel_protocol as X
from app.voice.nlu_rules import NLUResult
from app.voice.policy import asr_confidence, gate, needs_llm


def test_parse_start_and_media_and_stop():
    start = {"event": "start", "sequence_number": 1, "stream_sid": "S1",
             "start": {"stream_sid": "S1", "call_sid": "C1", "account_sid": "A", "from": "09876543210",
                       "to": "+914071234567", "custom_parameters": {"k": "tok"},
                       "media_format": {"encoding": "raw", "sample_rate": "8000", "bit_rate": "128"}}}
    e = X.parse(json.dumps(start))
    assert (e.kind, e.stream_sid, e.call_sid, e.from_number, e.to_number, e.custom, e.sample_rate) == \
        ("start", "S1", "C1", "09876543210", "+914071234567", {"k": "tok"}, 8000)
    pcm = b"\x01\x00" * 160
    m = X.parse(json.dumps({"event": "media", "stream_sid": "S1",
                            "media": {"chunk": 1, "timestamp": "10", "payload": base64.b64encode(pcm).decode()}}))
    assert m.audio == pcm
    s = X.parse(json.dumps({"event": "stop", "stream_sid": "S1", "stop": {"call_sid": "C1", "reason": "callended"}}))
    assert s.reason == "callended"
    assert X.parse('{"event":"dtmf","stream_sid":"S1","dtmf":{"digit":"5","duration":"200"}}').digit == "5"
    assert X.parse('{"event":"mark","stream_sid":"S1","mark":{"name":"u3"}}').mark == "u3"


@pytest.mark.parametrize("raw", ["not json", "[]", '{"event":"bogus"}'])
def test_parse_rejects(raw):
    with pytest.raises(ValueError):
        X.parse(raw)


def test_outbound_messages():
    assert json.loads(X.clear("S1")) == {"event": "clear", "stream_sid": "S1"}
    assert json.loads(X.mark("S1", "u1"))["mark"]["name"] == "u1"
    msg = json.loads(X.media("S1", b"\x00\x01"))
    assert base64.b64decode(msg["media"]["payload"]) == b"\x00\x01"


def test_policy():
    assert asr_confidence("umm hmm") == 0.3 and asr_confidence("") == 0.0 and asr_confidence("repu doctor") == 1.0
    n = NLUResult(["BOOK_APPOINTMENT"], {"date": "x"}, 0.95)
    assert gate(n, 1.0, 0.6).intents == ["BOOK_APPOINTMENT"]
    assert gate(n, 0.3, 0.6).intents == []
    assert needs_llm(NLUResult([], {}, 0.0), "something unusual here") is True
    assert needs_llm(NLUResult([], {}, 0.0), "hmm") is False
