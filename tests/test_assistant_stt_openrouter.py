"""Kriya Ketu voice: WAV uploads and the OpenRouter speech-to-text provider (ASSISTANT_STT_PROVIDER)."""

import asyncio
import base64
import json
from unittest.mock import patch

import httpx
import pytest

from app.assistant import stt
from app.config import settings

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 2000


def test_wav_is_accepted_by_magic():
    assert stt.audio_mime("audio/wav") == "audio/wav"
    assert stt.looks_like("audio/wav", WAV)
    assert not stt.looks_like("audio/wav", b"\x1a\x45\xdf\xa3" + b"\x00" * 20)


@pytest.mark.parametrize("prov, sarvam, orkey, ok", [
    ("sarvam", "k", "", True), ("sarvam", "", "k", False),
    ("openrouter", "", "k", True), ("openrouter", "k", "", False),
])
def test_configured_follows_the_chosen_provider(prov, sarvam, orkey, ok):
    with patch.object(settings, "assistant_stt_provider", prov), \
         patch.object(settings, "sarvam_api_key", sarvam), patch.object(settings, "openrouter_api_key", orkey):
        assert stt.configured() is ok
        assert isinstance(stt.provider(), stt.OpenRouterAudio if prov == "openrouter" else stt.SarvamRest)


def _run(handler, mime="audio/wav", audio=WAV, lang="unknown"):
    p = stt.OpenRouterAudio(transport=httpx.MockTransport(handler))
    with patch.object(settings, "openrouter_api_key", "or-key"):
        return asyncio.run(p.transcribe(audio, mime, lang, "transcribe"))


def test_openrouter_request_shape_and_transcript():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": ' "Dr Meena fee 600" \n'}}]})

    heard = _run(handler, lang="te-IN")
    assert heard.text == "Dr Meena fee 600" and heard.language == "te-IN"
    assert seen["auth"] == "Bearer or-key"
    parts = seen["body"]["messages"][0]["content"]
    assert seen["body"]["model"] == settings.assistant_stt_openrouter_model
    assert "Telugu" in parts[0]["text"]
    assert parts[1]["input_audio"] == {"data": base64.b64encode(WAV).decode(), "format": "wav"}


@pytest.mark.parametrize("handler, code", [
    (lambda r: httpx.Response(500, json={}), "failed"),
    (lambda r: httpx.Response(200, json={"choices": []}), "failed"),
    (lambda r: httpx.Response(200, text="not json"), "failed"),
])
def test_openrouter_failures_are_fixed_codes(handler, code):
    with pytest.raises(stt.SttError) as e:
        _run(handler)
    assert e.value.code == code


def test_openrouter_refuses_webm_without_calling():
    calls = []
    with pytest.raises(stt.SttError):
        _run(lambda r: calls.append(r) or httpx.Response(200), mime="audio/webm")
    assert not calls


@pytest.mark.parametrize("raw, clean", [
    ("Dr Meena fee 600", "Dr Meena fee 600"),
    ('"open appointments"', "open appointments"),
    ("[background noise] add leave for Dr Ravi tomorrow", "add leave for Dr Ravi tomorrow"),
    ("cancel booking (inaudible) for Sita", "cancel booking for Sita"),
    ("No speech detected.", ""), ("[silence]", ""), ("...", ""), ("Inaudible", ""),
    ("no fever since yesterday", "no fever since yesterday"),  # a clinical "no" is speech, not a tag
    ("డాక్టర్ రవి రేపు సెలవు", "డాక్టర్ రవి రేపు సెలవు"),
])
def test_noise_tags_and_no_speech_replies_are_dropped(raw, clean):
    assert stt.clean_transcript(raw) == clean


def test_prompt_tells_the_model_to_ignore_the_ward():
    seen = {}

    def handler(req):
        seen["text"] = json.loads(req.content)["messages"][0]["content"][0]["text"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "[noise]"}}]})

    assert _run(handler).text == ""
    assert "background voices" in seen["text"] and "Do not guess" in seen["text"]
