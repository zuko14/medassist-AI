"""KK-P3-03: Kriya Ketu mic UI (openrouter-and-speech-spec.md B.2-B.5; ux-design-spec.md W4-W6, W15).

Pure widget helpers are extracted from admin/index.html and run under node (pattern:
tests/test_assistant_panel_composer.py). Covers KK-FR-006..009, VOICE-04 (no submit path from a
transcription), VOICE-06 (tracks stopped on every exit) and the B.3 failure copy.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
JS = HTML[HTML.index("// ── Kriya Ketu"):HTML.index("// ── end Kriya Ketu")]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found in admin/index.html"
    return m.group(0)


def _run(fns, expr, prelude=""):
    src = prelude + "\n" + "\n".join(_fn(f) for f in fns) + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _method(name: str) -> str:
    """One method of the `ketu` object: from `name(` to the next method at the same indent."""
    m = re.search(rf"\n    (?:async )?{name}\(.*?\n    \}},?\n", JS, re.S)
    assert m, f"ketu.{name}() not found"
    return m.group(0)


ESC = "function esc(s){return String(s ?? '').replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';');}"


# ── KK-FR-009: the mic shows only with the flag and browser support ───────────

@needs_node
@pytest.mark.parametrize("flag, nav, mr, ok", [
    (True, "{mediaDevices:{getUserMedia(){}}}", "function(){}", True),
    (False, "{mediaDevices:{getUserMedia(){}}}", "function(){}", False),
    (True, "{}", "function(){}", False),                        # no getUserMedia (http, old browser)
    (True, "{mediaDevices:{getUserMedia(){}}}", "undefined", False),  # no MediaRecorder
    (True, "undefined", "function(){}", False),
])
def test_mic_gate(flag, nav, mr, ok):
    got = _run(["ketuMicAllowed"], f"ketuMicAllowed({json.dumps(flag)}, {nav}, {mr})")
    assert got is ok


@needs_node
@pytest.mark.parametrize("supported, expected", [
    (["audio/webm;codecs=opus", "audio/webm", "audio/mp4"], "audio/webm;codecs=opus"),  # Chrome/Firefox
    (["audio/mp4"], "audio/mp4"),                                                        # Safari
    (["audio/webm"], "audio/webm"),
    (["audio/ogg"], None),                                                               # nothing we accept
])
def test_mime_negotiation(supported, expected):
    got = _run(["ketuPickMime"], f"ketuPickMime(t => {json.dumps(supported)}.includes(t))")
    assert got == expected


@needs_node
def test_mime_negotiation_survives_a_throwing_probe():
    assert _run(["ketuPickMime"], "ketuPickMime(() => { throw new Error('x'); })") is None


# ── B.3: every failure state has its own copy ─────────────────────────────────

B3 = {
    "PERMISSION_DENIED": "Microphone blocked. Allow it in the browser's site settings, or type instead.",
    "NO_DEVICE": "No microphone found.",
    "CAPTURE_FAILED": "Recording failed. Try again.",
    "NO_SPEECH": "Didn't catch anything. Try again closer to the mic.",
    "STT_FAILED": "Voice service unavailable. Type your request.",
    "UNSUPPORTED": "Voice isn't supported in this browser. Type your request.",
}


@needs_node
@pytest.mark.parametrize("kind", list(B3))
def test_failure_copy(kind):
    assert _run(["ketuMicCopy"], f"ketuMicCopy({json.dumps(kind)})") == B3[kind]


@needs_node
@pytest.mark.parametrize("name, kind", [
    ("NotAllowedError", "PERMISSION_DENIED"), ("SecurityError", "PERMISSION_DENIED"),
    ("NotFoundError", "NO_DEVICE"), ("OverconstrainedError", "NO_DEVICE"),
    ("NotReadableError", "CAPTURE_FAILED"), ("AbortError", "CAPTURE_FAILED"), ("Whatever", "CAPTURE_FAILED"),
])
def test_getusermedia_errors_map_to_states(name, kind):
    assert _run(["ketuMicErrorKind"], f"ketuMicErrorKind({{name: {json.dumps(name)}}})") == kind


@needs_node
@pytest.mark.parametrize("status, body, expected", [
    (504, {"detail": {"error_code": "stt_timeout", "message": B3["STT_FAILED"]}}, B3["STT_FAILED"]),  # STT_TIMEOUT
    (502, {"detail": {"error_code": "stt_failed", "message": B3["STT_FAILED"]}}, B3["STT_FAILED"]),
    (429, {"detail": {"error_code": "rate_limited", "message": "Too many voice requests."}}, "Too many voice requests."),
    (413, None, B3["CAPTURE_FAILED"]),
    (415, None, B3["UNSUPPORTED"]),
    (500, {"detail": "Traceback (most recent call last) secret"}, B3["STT_FAILED"]),  # never raw text
    (0, None, B3["STT_FAILED"]),
])
def test_transcribe_failure_text(status, body, expected):
    got = _run(["ketuMicCopy", "ketuSttErrorText"], f"ketuSttErrorText({status}, {json.dumps(body)})")
    assert got == expected


# ── transcript → composer chip (UNSUPPORTED_LANGUAGE, NO_SPEECH, edited) ─────

@needs_node
@pytest.mark.parametrize("lang, edited, text, warn", [
    ("te-IN", False, "From voice · Telugu · check before sending", False),
    ("hi-IN", False, "From voice · Hindi · check before sending", False),
    ("en-IN", False, "From voice · English · check before sending", False),
    (None, False, "From voice · check before sending", False),
    ("ta-IN", False, "Detected ta-IN — please check", True),
    ("te-IN", True, "From voice · edited", False),
    ("ta-IN", True, "From voice · edited", False),
])
def test_voice_chip(lang, edited, text, warn):
    got = _run(["ketuVoiceChip"], f"ketuVoiceChip({json.dumps(lang)}, {json.dumps(edited)})")
    assert got == {"text": text, "warn": warn}


@needs_node
@pytest.mark.parametrize("text, no_speech", [("", True), (" ", True), ("a", True), ("ok", False), ("  hi  ", False)])
def test_no_speech_rule(text, no_speech):
    assert _run(["ketuNoSpeech"], f"ketuNoSpeech({json.dumps(text)})") is no_speech


# ── KK-FR-007: what Send carries ──────────────────────────────────────────────

@needs_node
def test_send_fields_for_typed_text():
    got = _run(["ketuVoiceFields"], "ketuVoiceFields(null, 'open leads')")
    assert got == {"input_mode": "text"}


@needs_node
def test_send_fields_for_voice_keep_the_original_even_when_edited():
    got = _run(["ketuVoiceFields"], """[
        ketuVoiceFields({original: 'mark ravi on leave', lang: 'te-IN'}, 'mark ravi on leave'),
        ketuVoiceFields({original: 'mark ravi on leave', lang: null}, 'mark Dr Ravi on leave tomorrow'),
    ]""")
    assert got == [
        {"input_mode": "voice", "transcript_original": "mark ravi on leave", "language": "te-IN"},
        {"input_mode": "voice", "transcript_original": "mark ravi on leave"},
    ]


@needs_node
def test_user_bubble_shows_the_edited_chip_only_for_edited_voice():
    prelude = ESC + "\nfunction ketuPageLabel(p){return p;}"
    got = _run(["ketuCardHtml"], """[
        ketuCardHtml({type: 'user', text: 'a'}, ketuPageLabel),
        ketuCardHtml({type: 'user', text: 'a', edited: true}, ketuPageLabel),
    ]""", prelude)
    assert "edited" not in got[0]
    assert "edited" in got[1] and "ketu-user" in got[1]


# ── recording clock: warn from 25 s, hard stop at 30 s ────────────────────────

@needs_node
@pytest.mark.parametrize("ms, clock, warn, done", [
    (0, "0:00 / 0:30", False, False), (7400, "0:07 / 0:30", False, False),
    (24999, "0:24 / 0:30", False, False), (25000, "0:25 / 0:30", True, False),
    (30000, "0:30 / 0:30", True, True), (31000, "0:30 / 0:30", True, True),
])
def test_recording_clock(ms, clock, warn, done):
    got = _run(["ketuRecState"], f"ketuRecState({ms})")
    assert got == {"clock": clock, "warn": warn, "done": done}


# ── VOICE-04: no code path submits after transcription ────────────────────────

@needs_node
def test_apply_transcript_fills_the_composer_and_never_sends():
    got = _run(["ketuApplyTranscript"], """(() => {
        let sends = 0, submits = 0;
        globalThis.ketu = { send() { sends++; } };
        const input = { value: 'old', focus() { this.focused = true; },
                        setSelectionRange(a, b) { this.sel = [a, b]; },
                        form: { requestSubmit() { submits++; }, submit() { submits++; } } };
        const voice = ketuApplyTranscript(input, { text: 'mark ravi on leave', language: 'te-IN' });
        return { value: input.value, sel: input.sel, focused: input.focused, voice, sends, submits };
    })()""")
    assert got == {"value": "mark ravi on leave", "sel": [18, 18], "focused": True,
                   "voice": {"original": "mark ravi on leave", "lang": "te-IN"}, "sends": 0, "submits": 0}


def test_no_voice_method_can_reach_send():
    """Static guard: the mic methods never call send/submit or the requests endpoint."""
    voice_src = "".join(_method(m) for m in ("micStart", "micStop", "micStopped", "micUpload", "micCancel",
                                              "micRelease", "micTick", "micFail", "micView"))
    voice_src += _fn("ketuApplyTranscript")
    for banned in ("this.send(", "ketu.send(", "requestSubmit", ".submit(", "/admin/assistant/requests"):
        assert banned not in voice_src, banned
    assert "/admin/assistant/transcribe" in voice_src


# ── VOICE-06: the mic track is stopped on every exit ──────────────────────────

@needs_node
def test_stop_tracks_stops_every_track():
    got = _run(["ketuStopTracks"], """(() => {
        const t = [{readyState: 'live', stop() { this.readyState = 'ended'; }},
                   {readyState: 'live', stop() { this.readyState = 'ended'; }}];
        ketuStopTracks({ getTracks: () => t });
        ketuStopTracks(null);  // nothing to stop: no throw
        return t.map(x => x.readyState);
    })()""")
    assert got == ["ended", "ended"]


def test_every_exit_path_releases_the_mic():
    assert "ketuStopTracks(" in _method("micRelease")
    for m in ("micCancel", "micStopped", "micFail"):
        assert "this.micRelease()" in _method(m), m
    assert "this.micCancel()" in _method("close")       # WIDGET_CLOSED
    assert "this.micCancel()" in _method("teardown")
    # Esc cancels a recording or upload before it closes the panel (ux W17).
    esc_handler = JS[JS.index("if (e.key !== 'Escape') return;"):][:300]
    assert esc_handler.index("micCancel") < esc_handler.index("this.close()")


def test_getusermedia_resolving_after_cancel_is_stopped_at_once():
    start = _method("micStart")
    assert re.search(r"if \(this\.mic !== m\)\s*\{\s*ketuStopTracks\(stream\)", start)


# ── wiring, disclosure, accessibility ─────────────────────────────────────────

def test_stt_flag_is_read_from_admin_me_and_cleared_on_logout():
    assert HTML.count("myKetuStt = !!me.assistant_stt_enabled") == 1
    assert HTML.count("myKetuStt = !!scoped.assistant_stt_enabled") == 1
    assert re.search(r"myKetu = false;\s*myKetuStt = false;", HTML)


def test_one_time_disclosure_text_and_storage():
    assert "Voice is sent to our speech provider to turn it into text. Audio isn't stored." in JS
    assert "localStorage" in _method("micNotice") and "try" in _method("micNotice")


def test_language_menu_offers_the_spec_languages():
    for value, label in (("auto", "Auto"), ("te", "Telugu"), ("hi", "Hindi"), ("en", "English"), ("mixed", "Mixed")):
        assert f'<option value="{value}">{label}</option>' in JS


def test_recording_is_announced_and_controls_are_labelled():
    for needle in ("Recording started", "Recording stopped, transcribing", "Transcript ready, edit and press Send",
                   'aria-label="Record voice"', 'aria-label="Stop recording"', 'aria-label="Cancel recording"',
                   "It won't be sent until you press Send"):
        assert needle in JS, needle
    css = HTML[HTML.index("/* ── Kriya Ketu"):HTML.index("/* ── end Kriya Ketu")]
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert re.search(r"\.ketu-meter\s*\{\s*display:\s*none", reduced)


def test_upload_sends_the_blob_raw_with_its_mime_and_duration():
    up = _method("micUpload")
    assert "'Content-Type'" in up and "body: blob" in up and "duration_ms=" in up and "language=" in up
    assert "signal" in up                                  # CANCELLED: AbortController
    assert "FormData" not in up                            # raw body (status.md §5 #18)
