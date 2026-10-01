#!/usr/bin/env python3
"""Where a firmware keeps its screen: found by looking, not assumed.

The display controller's memory is the screen: every firmware pushes its pixels
through the same controller, so the panel needs no per-build knowledge and is the
path the page uses. The *guest* buffers are the fallback, and they move between
builds -- so passing one build's addresses for another renders a picture that is
plausible and wrong, which is exactly how "the other firmware looks shifted" arrived.

The firmware files here are minimal ELFs: one program header, no section headers and
no symbol table (tools/bin2elf.py writes them), so there are no `gFrameBuffer`
symbols to read. What there *is*, is behaviour: the firmware's own buffers hold the
same bytes the controller holds, because that is where the driver copied them from.
So the addresses are found by sliding the controller's memory through SRAM and
keeping the offset that agrees.

    tools/uvk5_buffers.py --qmp 127.0.0.1:4444      # say where this firmware keeps it

Agreement is not assumed to be perfect: the panel can be a frame ahead of the buffer
it was copied from, so the score is reported and a caller decides what to trust.
"""
import argparse
import os
import struct
import sys
import tempfile

import uvk5_lcd

# 16 KB of SRAM. The buffers are inside it; nothing else is near.
SRAM_BASE = 0x20000000
SRAM_SIZE = 0x4000

# The frame is seven pages and the status line is one, and in SRAM the *frame* comes
# first (gFrameBuffer at 0x200012BE, gStatusLine at 0x2000163E for the 5.9.0.CN build).
# The panel's own memory has them the other way round, because page 0 is the top line.
FRAME_BYTES = uvk5_lcd.FRAME_BYTES
STATUS_BYTES = uvk5_lcd.STATUS_BYTES

# Below this fraction of bytes agreeing, the match is a coincidence rather than a
# buffer. Measured: a real match scores above 0.99 unless the screen is mid-update.
CONFIDENT = 0.90


def score(sram: bytes, offset: int, gram: bytes) -> int:
    """How many bytes at @offset match the controller's memory, frame then status."""
    if offset < 0 or offset + FRAME_BYTES + STATUS_BYTES > len(sram):
        return -1
    frame = sram[offset:offset + FRAME_BYTES]
    status = sram[offset + FRAME_BYTES:offset + FRAME_BYTES + STATUS_BYTES]
    return (sum(1 for a, b in zip(frame, gram[STATUS_BYTES:]) if a == b)
            + sum(1 for a, b in zip(status, gram[:STATUS_BYTES]) if a == b))


def locate(sram: bytes, gram: bytes):
    """(frame address, status address, matching bytes, total) for the best offset."""
    total = FRAME_BYTES + STATUS_BYTES
    best, best_at = -1, None
    for offset in range(0, len(sram) - total + 1):
        value = score(sram, offset, gram)
        if value > best:
            best, best_at = value, offset
    if best_at is None:
        return None, None, 0, total
    return SRAM_BASE + best_at, SRAM_BASE + best_at + FRAME_BYTES, best, total


def from_symbols(path: str):
    """(frame, status) from a real ELF symbol table, or (None, None).

    Kept because it is authoritative when it works -- a fully linked ELF (the CW
    timing build, for instance) does name these buffers. The images this project
    usually runs do not.
    """
    try:
        data = open(path, "rb").read()
    except OSError:
        return None, None
    if len(data) < 52 or data[0] != 0x7F or data[1:4] != b"ELF" or data[4] != 1:
        return None, None
    e_shoff, = struct.unpack_from("<I", data, 0x20)
    e_shentsize, e_shnum, _ = struct.unpack_from("<HHH", data, 0x2E)
    if not e_shoff or not e_shnum:
        return None, None
    sections = []
    for i in range(e_shnum):
        fields = struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize)
        sections.append(dict(type=fields[1], offset=fields[4], size=fields[5],
                             link=fields[6], entsize=fields[9]))
    found = {}
    for section in sections:
        if section["type"] not in (2, 11):
            continue
        strings = sections[section["link"]]
        blob = data[strings["offset"]:strings["offset"] + strings["size"]]
        step = section["entsize"] or 16
        for k in range(section["size"] // step):
            name, value = struct.unpack_from("<II", data, section["offset"] + k * step)
            if not name or not value:
                continue
            end = blob.find(b"\x00", name)
            found[blob[name:end].decode("ascii", "replace")] = value
    frame = found.get("gFrameBuffer")
    status = found.get("gStatusLine")
    if frame and status:
        return frame, status
    return None, None


def sram_from(client) -> bytes:
    """Read the whole of SRAM through QMP memsave."""
    path = os.path.join(tempfile.mkdtemp(prefix="uvk5-buffers-"), "sram.bin")
    client.command("memsave", val=SRAM_BASE, size=SRAM_SIZE, filename=path)
    with open(path, "rb") as fh:
        return fh.read()


def discover(client, gram: bytes = None, image_path: str = None) -> dict:
    """{frame, status, how, score, total} -- symbols first, then the search."""
    if image_path:
        frame, status = from_symbols(image_path)
        if frame:
            return dict(frame=frame, status=status, how="elf symbols",
                        score=None, total=None)
    if client is None:
        return dict(frame=None, status=None, how="no emulator", score=0, total=0)
    gram = gram if gram is not None else uvk5_lcd.FrameGrabber(client, 0, 0).panel_gram()
    frame, status, best, total = locate(sram_from(client), gram)
    how = "sram search" if best >= CONFIDENT * total else "sram search (weak match)"
    return dict(frame=frame, status=status, how=how, score=best, total=total)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qmp", default="127.0.0.1:4444")
    ap.add_argument("--image", help="firmware file, to try its symbols first")
    args = ap.parse_args(argv)

    from uvk5_qmp import QmpClient
    try:
        client = QmpClient(args.qmp, timeout=20)
    except Exception as exc:
        print("cannot reach QMP at %s: %s" % (args.qmp, str(exc).splitlines()[0]), file=sys.stderr)
        print("if the web UI is running it holds the one QMP client", file=sys.stderr)
        return 2
    found = discover(client, image_path=args.image)
    print("frame  0x%08X" % found["frame"] if found["frame"] else "frame  (not found)")
    print("status 0x%08X" % found["status"] if found["status"] else "status (not found)")
    print("how    %s" % found["how"])
    if found["score"] is not None:
        print("match  %d/%d bytes" % (found["score"], found["total"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
