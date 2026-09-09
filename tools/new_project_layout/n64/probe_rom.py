#!/usr/bin/env python3
"""probe_rom.py — cartridge identity for an N64 dump, with no build required.

Reads the header, resolves the CIC from the IPL2 checksum, and digests the
file. Everything it prints is READ from the dump; nothing is inferred from a
filename, and a field it cannot establish comes back empty rather than guessed.

    python3 probe_rom.py "Mario Kart 64 (USA).z64"
    python3 probe_rom.py rom.z64 --json
    python3 probe_rom.py rom.z64 --shell     # KEY=VALUE, for `eval`

WHY THIS RE-IMPLEMENTS THE IPL2 CHECKSUM. Scaffolding runs BEFORE the framework
is built -- naming the project is the first thing that happens -- so it cannot
call into runtime/src/devices/cic.c. This is a transcription of that file's
algorithm, and it is not trusted on its own: setup_project.sh cross-checks the
CIC it reports against the one n64lle-harvest prints from the real detector
during the generate step, and fails loudly if the two disagree. Verify the tool
(PRINCIPLES.md, "Tool Skepticism").

BYTE ORDER. .v64 (byte-swapped) and .n64 (word-swapped) dumps are normalized in
memory for parsing, exactly as runtime/src/devices/boot.c does when it loads
one. The SHA-256 is always of the file AS IT SITS ON DISK, because that is what
the launcher hashes when it verifies a player's copy.
"""

import argparse
import hashlib
import json
import sys

# --- the CIC variant table, transcribed from runtime/src/devices/cic.c -------
# (model, seed, 48-bit IPL2 checksum). The 71xx rows are the PAL-region twins
# of the 61xx rows and share their checksum; detection keys on the checksum, so
# the NTSC name is what comes back and the header's region byte is the separate
# fact. That mirrors n64_cic_detect()'s own comment.
CIC_TABLE = [
    ("CIC-NUS-6101", 0x3F, 0x45CC73EE317A),
    ("CIC-NUS-6102", 0x3F, 0xA536C0F1D859),
    ("CIC-NUS-7102", 0x3F, 0x44160EC5D9AF),
    ("CIC-NUS-6103", 0x78, 0x586FD4709867),
    ("CIC-NUS-6105", 0x91, 0x8618A45BC2D3),
    ("CIC-NUS-6106", 0x85, 0x2BBAD4E6EB74),
    ("CIC-NUS-5101", 0xAC, 0x93E983A8F152),
]
SEEDS = (0x3F, 0x78, 0x91, 0x85, 0xAC)

M32 = 0xFFFFFFFF


def _rotl(v, s):
    s &= 31
    return v if s == 0 else ((v << s) | (v >> (32 - s))) & M32


def _rotr(v, s):
    s &= 31
    return v if s == 0 else ((v >> s) | (v << (32 - s))) & M32


def _ck(a0, a1, a2):
    if a1 == 0:
        a1 = a2
    prod = a0 * a1
    hi = (prod >> 32) & M32
    lo = prod & M32
    diff = (hi - lo) & M32
    return diff if diff else a0


def _be32(b, off):
    return int.from_bytes(b[off:off + 4], "big")


def ipl2_checksum(seed, ipl3):
    """The published IPL2 checksum over the 1008-word IPL3 boot block."""
    pos = 0
    data = _be32(ipl3, pos)
    pos += 4
    init = ((0x6C078965 * (seed & 0xFF)) + 1) & M32
    init ^= data
    st = [init] * 16

    data_next = data
    loop = 1
    while True:
        data_last = data
        data = data_next

        st[0] = (st[0] + _ck((1007 - loop) & M32, data, loop)) & M32
        st[1] = _ck(st[1], data, loop)
        st[2] ^= data
        st[3] = (st[3] + _ck((data + 5) & M32, 0x6C078965, loop)) & M32
        st[9] = _ck(st[9], data, loop) if data_last < data else (st[9] + data) & M32
        st[4] = (st[4] + _rotr(data, data_last & 0x1F)) & M32
        st[7] = _ck(st[7], _rotl(data, data_last & 0x1F), loop)
        if data < st[6]:
            st[6] = ((st[3] + st[6]) & M32) ^ ((data + loop) & M32)
        else:
            st[6] = ((st[4] + data) & M32) ^ st[6]
        st[5] = (st[5] + _rotl(data, data_last >> 27)) & M32
        st[8] = _ck(st[8], _rotr(data, data_last >> 27), loop)

        if loop == 1008:
            break

        data_next = _be32(ipl3, pos)
        pos += 4
        st[15] = _ck(_ck(st[15], _rotl(data, data_last >> 27), loop),
                     _rotl(data_next, data >> 27), loop)
        st[14] = _ck(_ck(st[14], _rotr(data, data_last & 0x1F), loop),
                     _rotr(data_next, data & 0x1F), loop)
        st[13] = (st[13] + _rotr(data, data & 0x1F)
                  + _rotr(data_next, data_next & 0x1F)) & M32
        st[10] = _ck((st[10] + data) & M32, data_next, loop)
        st[11] = _ck(st[11] ^ data, data_next, loop)
        st[12] = (st[12] + (st[8] ^ data)) & M32
        loop += 1

    buf = [st[0]] * 4
    for loop in range(16):
        d = st[loop]
        tmp = (buf[0] + _rotr(d, d & 0x1F)) & M32
        buf[0] = tmp
        buf[1] = (buf[1] + d) & M32 if d < tmp else _ck(buf[1], d, loop)
        b1 = (d & 0x02) >> 1
        b0 = d & 0x01
        buf[2] = (buf[2] + d) & M32 if b1 == b0 else _ck(buf[2], d, loop)
        buf[3] = buf[3] ^ d if b0 == 1 else _ck(buf[3], d, loop)

    checksum = (_ck(buf[0], buf[1], 16) << 32) | (buf[3] ^ buf[2])
    return checksum & 0xFFFFFFFFFFFF


def detect_cic(ipl3):
    for seed in SEEDS:
        total = ipl2_checksum(seed, ipl3)
        for model, s, csum in CIC_TABLE:
            if s == seed and csum == total:
                return model, seed
    return "", 0


# --- header -----------------------------------------------------------------
REGION_LABELS = {
    "E": "USA", "J": "Japan", "P": "Europe", "U": "Australia", "D": "Germany",
    "F": "France", "I": "Italy", "S": "Spain", "H": "Netherlands", "K": "Korea",
    "N": "Canada", "B": "Brazil", "C": "China", "A": "Asia", "W": "Scandinavia",
    "X": "Europe", "Y": "Europe", "Z": "Europe",
}


def normalize(raw):
    """.v64/.n64 -> .z64 in memory (runtime/src/devices/boot.c n64_rom_adopt)."""
    magic = int.from_bytes(raw[0:4], "big")
    if magic == 0x80371240:
        return bytes(raw), "z64"
    if magic == 0x37804012:                      # .v64, byte-swapped pairs
        b = bytearray(raw)
        b[0:len(b) & ~1] = b''.join(
            bytes((b[i + 1], b[i])) for i in range(0, len(b) & ~1, 2))
        return bytes(b), "v64"
    if magic == 0x40123780:                      # .n64, word-swapped
        b = bytearray(raw)
        out = bytearray(b)
        for i in range(0, len(b) & ~3, 4):
            out[i:i + 4] = bytes((b[i + 3], b[i + 2], b[i + 1], b[i]))
        return bytes(out), "n64"
    return bytes(raw), ""


def probe(path):
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 0x1000:
        raise SystemExit(f"{path}: too small to be an N64 ROM ({len(raw)} bytes)")

    rom, byte_order = normalize(raw)
    if not byte_order:
        raise SystemExit(
            f"{path}: not an N64 ROM — header magic is "
            f"{int.from_bytes(raw[0:4],'big'):08x}, expected 80371240 (.z64), "
            f"37804012 (.v64) or 40123780 (.n64)")

    title = rom[0x20:0x34].decode("ascii", "replace").rstrip("\x00 ").strip()
    cartid = rom[0x3B:0x3F].decode("ascii", "replace")
    region = chr(rom[0x3E]) if 32 <= rom[0x3E] < 127 else ""
    cic, seed = detect_cic(rom[0x40:0x40 + 0xFC0])

    return {
        "path": str(path),
        "size": len(raw),
        "byte_order": byte_order,
        "header_title": title,
        "cartid": cartid,
        "region": region,
        "region_label": REGION_LABELS.get(region, ""),
        "revision": rom[0x3F],
        "entry_pc": f"0x{_be32(rom, 0x08):08X}",
        "crc1": f"{_be32(rom, 0x10):08x}",
        "crc2": f"{_be32(rom, 0x14):08x}",
        "cic": cic,
        "cic_seed": f"0x{seed:02x}" if cic else "",
        # The challenge is the 6105 anti-tamper LUT feedback loop; every other
        # variant answers with the dummy bitwise NOT (cic.c n64_cic_challenge).
        "cic_challenge": "real" if cic == "CIC-NUS-6105" else "dummy",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sha1": hashlib.sha1(raw).hexdigest(),
        "md5": hashlib.md5(raw).hexdigest(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rom")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="machine-readable")
    g.add_argument("--shell", action="store_true", help="KEY=VALUE for eval")
    args = ap.parse_args()

    info = probe(args.rom)

    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    if args.shell:
        for k, v in info.items():
            print(f"ROM_{k.upper()}='{str(v)}'")
        return 0

    width = max(len(k) for k in info)
    for k, v in info.items():
        print(f"{k.rjust(width)}  {v}")
    if not info["cic"]:
        print("\nWARNING: no CIC matched the IPL2 checksum. The dump may be "
              "modified, or it may use a variant this table does not carry.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
