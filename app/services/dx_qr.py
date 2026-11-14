"""Minimal QR Code encoder (ISO/IEC 18004) for the report verify link — pure, zero-LLM.

Byte mode, error-correction level M, versions 1–6 (up to 106 bytes: a verify URL is
~50). Returns a square matrix of booleans (True = dark), quiet zone not included.
shortcut: versions 1–6 only (no version-information blocks); raise the cap when a
caller needs more than 106 bytes.
"""

# version -> (EC codewords per block, blocks, data codewords per block); level M
_EC_M = {1: (10, 1, 16), 2: (16, 1, 28), 3: (26, 1, 44), 4: (18, 2, 32), 5: (24, 2, 43), 6: (16, 4, 27)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _gf_mul(x: int, y: int) -> int:
    """Multiply in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_ecc(data: list[int], degree: int) -> list[int]:
    """Reed–Solomon error-correction codewords for one block."""
    divisor = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _gf_mul(root, 0x02)
    result = [0] * degree
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def format_bits(mask: int) -> int:
    """15-bit format information for level M (indicator 00) and `mask`."""
    data = (0 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def codewords(text: str) -> tuple[int, list[int]]:
    """(version, final interleaved codewords) for `text` in byte mode."""
    raw = text.encode("utf-8")
    for version, (ec, blocks, per_block) in _EC_M.items():
        if 4 + 8 + 8 * len(raw) <= blocks * per_block * 8:
            break
    else:
        raise ValueError("qr_text_too_long")
    capacity = blocks * per_block * 8
    bits = [0, 1, 0, 0] + [(len(raw) >> i) & 1 for i in reversed(range(8))]
    for b in raw:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < blocks * per_block:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    chunks = [data[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    eccs = [rs_ecc(c, ec) for c in chunks]
    out = [c[i] for i in range(per_block) for c in chunks] + [e[i] for i in range(ec) for e in eccs]
    return version, out


def _function_grid(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    size = 17 + 4 * version
    mods = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        mods[y][x], func[y][x] = dark, True

    for i in range(size):                                            # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):            # finders + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    pos = _ALIGN[version]
    for i, ax in enumerate(pos):                                     # alignment
        for j, ay in enumerate(pos):
            if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    _draw_format(mods, func, 0)                                      # reserve; real bits drawn after masking
    return mods, func


def _draw_format(mods, func, mask: int) -> None:
    size = len(mods)
    bits = format_bits(mask)

    def put(x, y, dark):
        mods[y][x], func[y][x] = dark, True

    bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
    for i in range(6):
        put(8, i, bit(i))
    put(8, 7, bit(6))
    put(8, 8, bit(7))
    put(7, 8, bit(8))
    for i in range(9, 15):
        put(14 - i, 8, bit(i))
    for i in range(8):
        put(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        put(8, size - 15 + i, bit(i))
    put(8, size - 8, True)                                           # the dark module


def zigzag(size: int, func: list[list[bool]]):
    """Data module coordinates (x, y) in placement order."""
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not func[y][x]:
                    yield x, y
        right -= 2


def _penalty(m: list[list[bool]]) -> int:
    size, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:                                               # runs of 5+ same colour
        run, prev = 0, None
        for v in line:
            run = run + 1 if v == prev else 1
            prev = v
            if run == 5:
                score += 3
            elif run > 5:
                score += 1
        s = "".join("1" if v else "0" for v in line)                 # finder-like patterns
        score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for y in range(size - 1):                                        # 2x2 blocks
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += abs(dark * 20 - size * size * 10) // (size * size) * 10
    return score


def encode(text: str) -> list[list[bool]]:
    """The QR matrix for `text` (the mask with the lowest penalty)."""
    version, data = codewords(text)
    base, func = _function_grid(version)
    size = len(base)
    bits = [(b >> (7 - i)) & 1 == 1 for b in data for i in range(8)]
    coords = list(zigzag(size, func))
    for (x, y), dark in zip(coords, bits + [False] * (len(coords) - len(bits))):
        base[y][x] = dark
    best = None
    for mask, fn in enumerate(_MASKS):
        m = [row[:] for row in base]
        f = [row[:] for row in func]
        for x, y in coords:
            if fn(x, y):
                m[y][x] = not m[y][x]
        _draw_format(m, f, mask)
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]
