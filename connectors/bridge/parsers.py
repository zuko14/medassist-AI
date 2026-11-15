"""Analyser message parsers (ADR-DX-13, DX-61). Pure: bytes / text in, plain dicts out, no I/O.

Both return {"message_id": str | None, "specimens": [{"barcode": str, "results": [{"code", "value", "unit",
"flag"}]}]}. `code` is what Kriya matches against lab_test_parameters.machine_code; `flag` is the analyser's
own abnormal flag, kept for the record only — Kriya computes every flag from its own reference bands.

ASTM: E1381 low-level frames (<STX> FN text <ETB|ETX> C1 C2 <CR><LF>, checksum = sum of FN..ETB/ETX mod 256)
carrying E1394 records H, P, O, R, C, L. The ENQ / ACK / EOT handshake belongs to the serial / TCP loop,
not here. HL7 v2.3 / v2.5 ORU^R01: MSH, PID, OBR, OBX.
"""

from typing import Optional, Union

STX, ETX, ETB, CR, LF = "\x02", "\x03", "\x17", "\r", "\n"
CONTROL = {"\x04", "\x05", "\x06", "\x15"}           # EOT, ENQ, ACK, NAK between frames


class ParseError(ValueError):
    """The message is not well-formed; the bridge NAKs it and the raw text is kept for a human."""


def _text(data: Union[bytes, str]) -> str:
    return data.decode("latin-1") if isinstance(data, bytes) else data


def astm_checksum(frame_body: str) -> str:
    """Two upper-case hex digits: sum of the characters from FN through ETB / ETX, modulo 256."""
    return f"{sum(ord(ch) for ch in frame_body) % 256:02X}"


def astm_frame(fn: int, text: str, last: bool = True) -> str:
    """One E1381 frame (used by tests and the bridge's own echo checks)."""
    body = f"{fn % 8}{text}{ETX if last else ETB}"
    return f"{STX}{body}{astm_checksum(body)}{CR}{LF}"


def unframe_astm(data: Union[bytes, str]) -> str:
    """E1381 frames -> the E1394 record text (records separated by CR). Intermediate (ETB) frames are joined to
    the next; frame numbers must run 1..7, 0, 1...; a bad checksum raises. Unframed text passes through."""
    s = "".join(ch for ch in _text(data) if ch not in CONTROL)
    if STX not in s:
        return s
    out, expected, i = [], None, 0
    while True:
        start = s.find(STX, i)
        if start < 0:
            break
        end = min((p for p in (s.find(ETX, start), s.find(ETB, start)) if p >= 0), default=-1)
        if end < 0 or end + 3 > len(s):
            raise ParseError("truncated ASTM frame")
        body, check = s[start + 1:end + 1], s[end + 1:end + 3]
        if not body[:1].isdigit():
            raise ParseError("ASTM frame without a frame number")
        if check.upper() != astm_checksum(body):
            raise ParseError(f"ASTM checksum mismatch in frame {body[0]}")
        fn = int(body[0])
        if expected is not None and fn != expected:
            raise ParseError(f"ASTM frame {fn} out of sequence (expected {expected})")
        expected = (fn + 1) % 8
        out.append(body[1:-1])
        i = end + 3
    return "".join(out)


def _component(field: str, comp: str) -> list[str]:
    return field.split(comp)


def parse_astm(data: Union[bytes, str]) -> dict:
    text = unframe_astm(data)
    records = [r for r in text.replace(LF, CR).split(CR) if r.strip()]
    if not records or not records[0].startswith("H"):
        raise ParseError("ASTM message must start with an H record")
    h = records[0]
    if len(h) < 5:
        raise ParseError("ASTM header too short")
    fs, rep, comp = h[1], h[2], h[3]
    hf = h.split(fs)
    message_id = hf[2].strip() if len(hf) > 2 and hf[2].strip() else None
    specimens: list[dict] = []
    current: Optional[dict] = None
    for rec in records[1:]:
        f = rec.split(fs)
        kind = f[0].strip().upper()
        if kind == "O":
            sid = (f[2] if len(f) > 2 else "").split(rep)[0]
            barcode = _component(sid, comp)[0].strip()
            if not barcode and len(f) > 3:
                barcode = _component(f[3], comp)[0].strip()
            if not barcode:
                raise ParseError("ASTM order record without a specimen id")
            current = next((sp for sp in specimens if sp["barcode"] == barcode), None)
            if current is None:
                current = {"barcode": barcode, "results": []}
                specimens.append(current)
        elif kind == "R":
            if current is None:
                raise ParseError("ASTM result record before any order record")
            parts = _component(f[2] if len(f) > 2 else "", comp)
            code = next((p.strip() for p in parts[3:] if p.strip()), "") or next(
                (p.strip() for p in reversed(parts) if p.strip()), "")
            if not code:
                raise ParseError("ASTM result record without a test code")
            current["results"].append({
                "code": code, "value": (f[3] if len(f) > 3 else "").split(comp)[0].strip(),
                "unit": (f[4] if len(f) > 4 else "").strip() or None,
                "flag": (f[6] if len(f) > 6 else "").strip() or None})
        elif kind == "L":
            break
    return {"message_id": message_id, "specimens": specimens}


def _hl7_unescape(v: str, enc: str) -> str:
    comp, rep, esc, sub = (list(enc) + ["", "", "", ""])[:4]
    if not esc or esc not in v:
        return v
    out, i = [], 0
    table = {"F": "|", "S": comp, "T": sub, "R": rep, "E": esc}
    while i < len(v):
        if v[i] == esc:
            j = v.find(esc, i + 1)
            if j > i + 1 and v[i + 1:j] in table:
                out.append(table[v[i + 1:j]])
                i = j + 1
                continue
        out.append(v[i])
        i += 1
    return "".join(out)


def parse_hl7(data: Union[bytes, str]) -> dict:
    text = _text(data).strip("\x0b\x1c\r\n ")         # MLLP start / end blocks
    segs = [s for s in text.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s.strip()]
    if not segs or not segs[0].startswith("MSH") or len(segs[0]) < 8:
        raise ParseError("HL7 message must start with MSH")
    fs = segs[0][3]
    enc = segs[0].split(fs)[1]
    comp = enc[0] if enc else "^"
    msh = [""] + segs[0].split(fs)                     # MSH-1 is the separator itself: keep HL7 numbering
    message_id = msh[10].strip() if len(msh) > 10 and msh[10].strip() else None

    def field(seg: list, n: int) -> str:
        return _hl7_unescape(seg[n], enc) if len(seg) > n else ""

    specimens: list[dict] = []
    current: Optional[dict] = None
    for s in segs[1:]:
        f = s.split(fs)
        name = f[0].strip()
        if name == "OBR":
            barcode = (field(f, 2).split(comp)[0] or field(f, 3).split(comp)[0]).strip()
            if not barcode:
                raise ParseError("HL7 OBR without a placer or filler specimen number")
            current = next((sp for sp in specimens if sp["barcode"] == barcode), None)
            if current is None:
                current = {"barcode": barcode, "results": []}
                specimens.append(current)
        elif name == "OBX":
            if current is None:
                raise ParseError("HL7 OBX before any OBR")
            code = field(f, 3).split(comp)[0].strip()
            if not code:
                raise ParseError("HL7 OBX without an observation identifier")
            current["results"].append({"code": code, "value": field(f, 5).strip(),
                                       "unit": field(f, 6).split(comp)[0].strip() or None,
                                       "flag": field(f, 8).strip() or None})
    return {"message_id": message_id, "specimens": specimens}


def parse(protocol: str, data: Union[bytes, str]) -> dict:
    if protocol == "astm":
        return parse_astm(data)
    if protocol == "hl7v2":
        return parse_hl7(data)
    raise ParseError(f"unknown protocol {protocol!r}")
