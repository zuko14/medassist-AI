"""Kriya Ketu voice mode (Siri-style overlay): WAV encoding, end-of-speech detection and spoken yes/no.

Pure helpers are extracted from admin/index.html and run under node (pattern: test_assistant_panel_voice.py).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name):
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, name
    return m.group(0)


def _run(fns, expr, prelude=""):
    src = prelude + "\n".join(_fn(f) for f in fns) + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_wav_header_and_samples():
    got = _run(["ketuWavBytes"], "(() => { const b = ketuWavBytes(new Float32Array([0, 1, -1, 2]), 16000);"
               " const v = new DataView(b.buffer); return [String.fromCharCode(...b.slice(0,4)),"
               " String.fromCharCode(...b.slice(8,12)), v.getUint32(24,true), v.getUint16(22,true), v.getUint32(40,true),"
               " v.getInt16(44,true), v.getInt16(46,true), v.getInt16(48,true), v.getInt16(50,true), b.length]; })()")
    assert got == ["RIFF", "WAVE", 16000, 1, 8, 0, 32767, -32768, 32767, 52]


VAD = ["ketuVad"]
VAD_PRE = None


def _vad(levels_js):
    """Run the detector over a level function f(ms) -> dB, 100 ms ticks up to 30 s: [first act, its ms]."""
    pre = _consts("KETU_VAD")
    return _run(VAD, "(() => { let s = null; const f = " + levels_js + "; for (let ms = 0; ms <= 30000; ms += 100) {"
                " s = ketuVad(s, f(ms), ms); if (s.act) return [s.act, ms]; } return [null, 30000]; })()", pre)


@needs_node
def test_quiet_room_ends_1_5_s_after_speech():
    assert _vad("ms => ms >= 300 && ms < 1500 ? -45 : -80") == ["stop", 3000]


@needs_node
def test_noisy_ward_speech_is_heard_over_steady_noise_and_ends():
    # crowd / fan at -50 dB; the talker 15 dB above it with natural gaps between words
    act, ms = _vad("ms => (ms >= 1000 && ms < 4000 && (ms % 700) < 500) ? -35 : -50 + ((ms / 100) % 3)")
    assert act == "stop" and 5000 <= ms <= 6000


@needs_node
def test_short_pauses_do_not_end_the_turn():
    act, ms = _vad("ms => (ms < 6000 && (ms % 2000) < 1200) ? -40 : -80")  # 0.8 s pauses
    assert act == "stop" and ms >= 6000


@needs_node
def test_noise_that_starts_mid_turn_is_learnt_and_does_not_hold_the_mic_open():
    act, ms = _vad("ms => ms < 500 ? -80 : -58")  # an AC switches on; nobody speaks
    assert act in ("stop", "nospeech") and ms < 10000


@needs_node
def test_silence_gives_up_after_8_s():
    assert _vad("ms => -85") == ["nospeech", 8000]


@needs_node
def test_speech_band_level_ignores_out_of_band_bins():
    got = _run(["ketuBandDb"], "[ketuBandDb(Float32Array.from({length: 512}, (_, i) => i * 46.875 < 300 || i * 46.875 > 3400"
               " ? 0 : -60), 46.875), ketuBandDb(new Float32Array(4).fill(-Infinity), 46.875)]")
    assert abs(got[0] - (-60)) < 1e-9 and got[1] is None  # -Infinity serialises as null


@needs_node
def test_a_formant_peak_lifts_the_band_level_over_flat_noise():
    # 66 band bins of -60 dB noise; one bin 40 dB up (a vowel's formant) must lift the level by > 9 dB
    got = _run(["ketuBandDb"], "[ketuBandDb(Float32Array.from({length: 512}, () => -60), 46.875),"
               " ketuBandDb(Float32Array.from({length: 512}, (_, i) => i === 21 ? -20 : -60), 46.875)]")
    assert got[1] - got[0] > 9


@needs_node
def test_trim_keeps_the_speech_with_padding_and_never_cuts_a_quiet_clip():
    js = ("(() => { const r = 16000, x = new Float32Array(r * 3); let seed = 1;"
          " const noise = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 0.002;"
          " for (let i = 0; i < x.length; i++) x[i] = noise() + (i >= r && i < r * 1.5 ? 0.3 * Math.sin(i / 5) : 0);"
          " const quiet = new Float32Array(r).map(noise);"
          " return [ketuTrimBounds(x, r), ketuTrimBounds(quiet, r)[0], ketuTrimBounds(quiet, r)[1]]; })()")
    (a, b), qa, qb = _run(["ketuTrimBounds"], js)
    assert 16000 - 4000 - 320 <= a <= 16000 - 4000 and 24000 + 5600 - 320 <= b <= 24000 + 5600 + 320
    assert (qa, qb) == (0, 16000)


@needs_node
@pytest.mark.parametrize("text, verdict", [
    ("Yes.", "confirm"), ("  OK ", "confirm"), ("haan ji", "confirm"), ("అవును", "confirm"), ("हाँ", "confirm"),
    ("No", "cancel"), ("cancel it!", "cancel"), ("వద్దు", "cancel"), ("नहीं", "cancel"),
    ("yes add leave for dr ravi", None), ("no wait change the date", None), ("", None),
])
def test_spoken_verdict_is_exact_phrase_only(text, verdict):
    assert _run(["ketuVoxVerdict"], f"ketuVoxVerdict({json.dumps(text)})") == verdict


def test_voice_mode_never_bypasses_typed_or_ack_confirmations():
    body = HTML[HTML.index("async voxHeard(body)"):HTML.index("voxAfter() {")]
    assert "ketuTyped-" in body and "ketuAck-" in body
    # a spoken confirm presses the card's own Confirm path (plan_hash + server CAS), never a direct POST
    assert "this.confirm(id, btn)" in body and "/confirm" not in body


@needs_node
@pytest.mark.parametrize("current, words, mx, out", [
    ("", "fever for three days", 0, "fever for three days"),
    ("Fever x 3 days", "dry cough", 0, "Fever x 3 days. Dry cough"),
    ("Fever.", "dry cough", 0, "Fever. Dry cough"),
    ("Fever\n", "cough", 0, "Fever. Cough"),
    ("abc", "  ", 0, "abc"),
    ("abcdef", "ghij", 9, "abcdef. G"),
])
def test_dictation_joins_and_never_replaces(current, words, mx, out):
    assert _run(["ketuDictateJoin"], f"ketuDictateJoin({json.dumps(current)}, {json.dumps(words)}, {mx})") == out


@needs_node
def test_dictation_only_on_clinical_note_boxes():
    prelude = _consts("KETU_DICTATE_IDS")
    t = lambda **kw: "{tagName:'TEXTAREA', disabled:false, readOnly:false, hasAttribute:(a)=>" + \
        json.dumps(kw.get("icd", False)) + ", id:" + json.dumps(kw["id"]) + "}"
    got = _run(["ketuDictateTarget"], "[" + ",".join([
        f"ketuDictateTarget({t(id='opdCC')})", f"ketuDictateTarget({t(id='ipdDs_course_in_hospital')})",
        f"ketuDictateTarget({t(id='ipdDs_final_diagnosis', icd=True)})", f"ketuDictateTarget({t(id='leadMsgText')})",
        f"ketuDictateTarget({t(id='ketuInput')})", "ketuDictateTarget({tagName:'INPUT', id:'opdCC'})"]) + "]",
        prelude)
    assert got == [True, True, False, False, False, False]


def test_dictation_text_is_never_sent_to_the_assistant():
    js = HTML[HTML.index("        if (m.dictate) {\n            const area"):]
    branch = js[:js.index("return;\n        }") + 8]
    assert "/admin/assistant/requests" not in branch and "send(" not in branch and "ketuDictateJoin" in branch


def _consts(name):
    m = re.search(rf"const {name} = .*?;\n", HTML, re.S)
    assert m, name
    return m.group(0)
