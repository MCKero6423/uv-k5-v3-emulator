#!/usr/bin/env python3
"""The buffer search must count *matches*, not just bytes compared.

Its first version summed one per zipped pair and forgot `if a == b`, so every
offset scored the full total and the search always answered "the first offset" --
which looked like a working discovery and pointed the fallback at whatever happened
to sit at the start of SRAM. These tests use synthetic memory, so they need no
emulator and fail on that code.
"""
import struct
import unittest

import uvk5_buffers
from uvk5_lcd import FRAME_BYTES, STATUS_BYTES


def panel_memory(seed=1):
    """A controller memory with a recognisable, non-repeating-ish pattern."""
    frame = bytes(((i * 13) + seed) & 0xFF for i in range(FRAME_BYTES))
    status = bytes(((i * 29) + seed) & 0xFF for i in range(STATUS_BYTES))
    return status + frame          # the panel's order: the status page comes first


class TestLocate(unittest.TestCase):
    def test_it_finds_the_buffers_where_they_are(self):
        gram = panel_memory()
        pad = 0x1234
        # In SRAM the frame comes first and the status line follows it.
        sram = bytes(pad) + gram[STATUS_BYTES:] + gram[:STATUS_BYTES] + bytes(0x100)

        frame, status, best, total = uvk5_buffers.locate(sram, gram)

        self.assertEqual(frame, uvk5_buffers.SRAM_BASE + pad)
        self.assertEqual(status, uvk5_buffers.SRAM_BASE + pad + FRAME_BYTES)
        self.assertEqual(best, total, "a perfect match must score the whole window")
        self.assertEqual(total, FRAME_BYTES + STATUS_BYTES)

    def test_a_single_match_does_not_win_by_being_first(self):
        """The bug this guards: scoring pairs instead of matches picked offset 0."""
        gram = panel_memory()
        pad = 0x0800
        sram = bytes(pad) + gram[STATUS_BYTES:] + gram[:STATUS_BYTES] + bytes(0x40)
        _, _, best, total = uvk5_buffers.locate(sram, gram)
        self.assertGreater(best, total * 0.9)
        self.assertNotEqual(uvk5_buffers.score(sram, 0, gram), total,
                            "offset 0 is blank here and must not score full marks")

    def test_a_partly_stale_buffer_still_wins(self):
        """The panel can be a frame ahead of the buffer it was copied from."""
        gram = panel_memory()
        pad = 0x0400
        frame = bytearray(gram[STATUS_BYTES:])
        for i in range(0, 60):
            frame[i] ^= 0x5A
        sram = bytes(pad) + bytes(frame) + gram[:STATUS_BYTES] + bytes(0x80)
        found, _, best, total = uvk5_buffers.locate(sram, gram)
        self.assertEqual(found, uvk5_buffers.SRAM_BASE + pad)
        # What matters is that the right offset wins, not that it is perfect: the
        # panel can be a frame ahead of the buffer it was copied from.
        runner_up = max(uvk5_buffers.score(sram, off, gram)
                        for off in range(0, len(sram) - total + 1)
                        if off != pad)
        self.assertGreater(best, runner_up)


class TestSymbols(unittest.TestCase):
    def test_a_minimal_elf_has_no_symbols_to_offer(self):
        """tools/bin2elf.py writes program headers only, which is the usual case."""
        elf = bytearray(0x34 + 32 + 0x40)
        elf[0:4] = b"\x7fELF"
        elf[4] = 1                     # 32-bit
        elf[5] = 1                     # little endian
        struct.pack_into("<I", elf, 0x1C, 0x34)     # one program header
        struct.pack_into("<H", elf, 0x2A, 32)
        struct.pack_into("<H", elf, 0x2C, 1)
        struct.pack_into("<I", elf, 0x20, 0)        # no section headers
        struct.pack_into("<H", elf, 0x30, 0)
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "minimal.elf")
        with open(path, "wb") as fh:
            fh.write(bytes(elf))
        self.assertEqual(uvk5_buffers.from_symbols(path), (None, None))


if __name__ == "__main__":
    unittest.main()
