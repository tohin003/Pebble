"""Phase 46 (1.9.29, D214): the QR code of a pairing link, for Settings > iPhone (ISO/IEC 18004, byte mode).

Pebble ships no QR library and adds none: this is the standard's construction, kept small. Byte mode only, error
correction level M by default (a screen is clean; M leaves room for glare), the smallest version that fits, and the
mask with the lowest penalty. The result is the module matrix as rows of '0' and '1', which the Settings page draws as
SVG squares. Nothing here reads or writes anything.
"""

import re

LEVELS = {'L': 1, 'M': 0, 'Q': 3, 'H': 2}  # the two format bits of each error correction level
# Error correction codewords per block, and blocks, for versions 1 to 40 (the standard's table 9).
ECC_PER_BLOCK = {
    'L': (7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30,
          30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    'M': (10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28,
          28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    'Q': (13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30,
          30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    'H': (17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30,
          30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30)}
BLOCKS = {
    'L': (1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18,
          19, 19, 20, 21, 22, 24, 25),
    'M': (1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29,
          31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    'Q': (1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40,
          43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    'H': (1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45,
          48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81)}
MASKS = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
         lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
         lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
         lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)
RUNS = re.compile(r'0{5,}|1{5,}')
FINDER_LIKE = ('10111010000', '00001011101')


def raw_modules(version):
    """Modules left for data and error correction once the function patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def data_codewords(version, level):
    return raw_modules(version) // 8 - ECC_PER_BLOCK[level][version - 1] * BLOCKS[level][version - 1]


def alignment_positions(version):
    if version == 1:
        return []
    count, size = version // 7 + 2, version * 4 + 17
    step = 26 if version == 32 else (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    return [6] + [size - 7 - i * step for i in range(count - 1)][::-1]


def gf_multiply(x, y):
    """Product in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = gf_multiply(root, 0x02)
    return result


def rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= gf_multiply(coefficient, factor)
    return result


def format_bits(level, mask):
    data = LEVELS[level] << 3 | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    return (data << 10 | remainder) ^ 0x5412


def version_bits(version):
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
    return version << 12 | remainder


def _interleave(data, version, level):
    blocks, ecc = BLOCKS[level][version - 1], ECC_PER_BLOCK[level][version - 1]
    total = raw_modules(version) // 8
    short_count, short_length = blocks - total % blocks, total // blocks
    divisor = rs_divisor(ecc)
    rows, start = [], 0
    for index in range(blocks):
        chunk = data[start:start + short_length - ecc + (0 if index < short_count else 1)]
        start += len(chunk)
        rows.append(chunk + ([0] if index < short_count else []) + rs_remainder(chunk, divisor))
    out = []
    for i in range(len(rows[0])):
        for j, row in enumerate(rows):
            if i != short_length - ecc or j >= short_count:  # the short blocks' padding place is skipped
                out.append(row[i])
    return out


def _codewords(text, level):
    data = text.encode('utf-8')
    for version in range(1, 41):
        capacity, count_bits = data_codewords(version, level) * 8, 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= capacity:
            break
    else:
        raise ValueError('That is too long for a QR code.')
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in reversed(range(length)))
    put(0b0100, 4)
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    return version, _interleave(words, version, level)


def penalty(rows):
    """The standard's four penalty rules (runs, 2x2 blocks, finder-like patterns, dark balance)."""
    size = len(rows)
    lines = rows + [''.join(row[x] for row in rows) for x in range(size)]
    score = 0
    for line in lines:
        score += sum(len(run.group()) - 2 for run in RUNS.finditer(line))
        padded = '0000' + line + '0000'
        for pattern in FINDER_LIKE:
            at = padded.find(pattern)
            while at >= 0:
                score += 40
                at = padded.find(pattern, at + 1)
    for y in range(size - 1):
        upper, lower = rows[y], rows[y + 1]
        score += 3 * sum(1 for x in range(size - 1) if upper[x] == upper[x + 1] == lower[x] == lower[x + 1])
    dark = sum(row.count('1') for row in rows)
    return score + 10 * int(abs(dark * 100 / (size * size) - 50) // 5)


def encode(text, level='M'):
    """The QR code of `text` as rows of '0' (light) and '1' (dark), without the quiet zone."""
    version, words = _codewords(text, level)
    size = version * 4 + 17
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, value):
        dark[y][x], fixed[y][x] = value, True

    def draw_format(mask):
        bits = format_bits(level, mask)
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):
            put(8, i, bit[i])
        put(8, 7, bit[6])
        put(8, 8, bit[7])
        put(7, 8, bit[8])
        for i in range(9, 15):
            put(14 - i, 8, bit[i])
        for i in range(8):
            put(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            put(8, size - 15 + i, bit[i])
        put(8, size - 8, True)

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    places = alignment_positions(version)
    last = len(places) - 1
    for i, ax in enumerate(places):
        for j, ay in enumerate(places):
            if (i, j) not in ((0, 0), (0, last), (last, 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    draw_format(0)
    if version >= 7:
        bits = version_bits(version)
        for i in range(18):
            value = (bits >> i) & 1 == 1
            put(size - 11 + i % 3, i // 3, value)
            put(i // 3, size - 11 + i % 3, value)
    index = 0
    for right in range(size - 1, 0, -2):
        right = right - 1 if right <= 6 else right
        for vertical in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vertical if (right + 1) & 2 == 0 else vertical
                if not fixed[y][x] and index < len(words) * 8:
                    dark[y][x] = (words[index >> 3] >> (7 - (index & 7))) & 1 == 1
                    index += 1

    def masked(mask):
        rule = MASKS[mask]
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and rule(x, y):
                    dark[y][x] = not dark[y][x]

    best = None
    for mask in range(8):
        masked(mask)
        draw_format(mask)
        score = penalty([''.join('1' if m else '0' for m in row) for row in dark])
        if best is None or score < best[0]:
            best = (score, mask)
        masked(mask)  # the same mask again undoes it
    masked(best[1])
    draw_format(best[1])
    return [''.join('1' if m else '0' for m in row) for row in dark]
