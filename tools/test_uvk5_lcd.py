#!/usr/bin/env python3
"""Unit tests for LCD unpacking and PNG encoding. No emulator needed."""
import os
import struct
import tempfile
import unittest
import zlib

from uvk5_lcd import (FRAME_BYTES, LCD_HEIGHT, LCD_WIDTH, STATUS_BYTES,
                      FrameGrabber, encode_png, unpack)


class TestUnpack(unittest.TestCase):
    def test_dimensions(self):
        pixels = unpack(bytes(LCD_WIDTH), bytes(7 * LCD_WIDTH))
        self.assertEqual(len(pixels), LCD_HEIGHT)
        self.assertEqual(len(pixels[0]), LCD_WIDTH)

    def test_lsb_is_top_pixel_of_its_page(self):
        # status row (page 0), column 0, bit 0 set -> pixel (0, 0)
        status = bytearray(LCD_WIDTH)
        status[0] = 0x01
        pixels = unpack(bytes(status), bytes(7 * LCD_WIDTH))
        self.assertEqual(pixels[0][0], 1)
        self.assertEqual(pixels[1][0], 0)

    def test_msb_is_bottom_pixel_of_its_page(self):
        status = bytearray(LCD_WIDTH)
        status[0] = 0x80
        pixels = unpack(bytes(status), bytes(7 * LCD_WIDTH))
        self.assertEqual(pixels[7][0], 1)
        self.assertEqual(pixels[6][0], 0)

    def test_frame_page_one_starts_at_row_eight(self):
        frame = bytearray(7 * LCD_WIDTH)
        frame[0] = 0x01                      # page 1, column 0, top bit
        pixels = unpack(bytes(LCD_WIDTH), bytes(frame))
        self.assertEqual(pixels[8][0], 1)

    def test_columns_are_independent(self):
        frame = bytearray(7 * LCD_WIDTH)
        frame[5] = 0xFF                      # page 1, column 5, all eight rows
        pixels = unpack(bytes(LCD_WIDTH), bytes(frame))
        for row in range(8, 16):
            self.assertEqual(pixels[row][5], 1, f"row {row} col 5")
            self.assertEqual(pixels[row][4], 0, f"row {row} col 4 should be clear")


class TestEncodePng(unittest.TestCase):
    def test_emits_a_png_signature(self):
        pixels = unpack(bytes(LCD_WIDTH), bytes(7 * LCD_WIDTH))
        self.assertTrue(encode_png(pixels, scale=2).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_header_reports_scaled_dimensions(self):
        pixels = unpack(bytes(LCD_WIDTH), bytes(7 * LCD_WIDTH))
        blob = encode_png(pixels, scale=3)
        # IHDR payload starts 8 bytes of signature + 4 length + 4 tag in.
        width, height = struct.unpack(">II", blob[16:24])
        self.assertEqual((width, height), (LCD_WIDTH * 3, LCD_HEIGHT * 3))

    def test_pixels_are_dark_on_light(self):
        """A set bit must render black; the radio LCD is dark-on-light."""
        status = bytearray(LCD_WIDTH)
        status[0] = 0x01                     # top-left pixel on
        pixels = unpack(bytes(status), bytes(7 * LCD_WIDTH))
        blob = encode_png(pixels, scale=1)

        # Pull IDAT back out and inflate it to check actual sample values.
        offset, idat = 8, b""
        while offset < len(blob):
            (length,) = struct.unpack(">I", blob[offset:offset + 4])
            tag = blob[offset + 4:offset + 8]
            if tag == b"IDAT":
                idat += blob[offset + 8:offset + 8 + length]
            offset += 12 + length
        raw = zlib.decompress(idat)

        # Each row is one filter byte followed by LCD_WIDTH samples at scale 1.
        self.assertEqual(raw[0], 0)          # filter type 0
        self.assertEqual(raw[1], 0x00)       # lit pixel -> black
        self.assertEqual(raw[2], 0xFF)       # neighbour -> white


class StubClient:
    """Stands in for QmpClient; writes the files memsave would write.

    Rejects pmemsave on purpose. pmemsave takes a *physical* address and returns
    zeros for the framebuffer's virtual address -- a blank screen with no error
    reported anywhere, which is exactly the bug this stub is here to catch.
    """

    def __init__(self):
        self.calls = []

    def command(self, name, **args):
        self.calls.append((name, args))
        if name == "pmemsave":
            raise AssertionError(
                "pmemsave reads physical addresses and silently returns zeros "
                "for gFrameBuffer; use memsave")
        if name != "memsave":
            raise AssertionError(f"unexpected command {name}")
        with open(args["filename"], "wb") as fh:
            fh.write(bytes(args["size"]))
        return {}


class TestFrameGrabber(unittest.TestCase):
    def test_uses_memsave_and_returns_png(self):
        client = StubClient()
        tmp = tempfile.mkdtemp()
        grabber = FrameGrabber(client, frame_addr=0x200013DC,
                               status_addr=0x2000175C, spool_dir=tmp)
        png = grabber.png(scale=2)

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual([c[0] for c in client.calls], ["memsave", "memsave"])

    def test_reads_the_right_addresses_and_sizes(self):
        client = StubClient()
        grabber = FrameGrabber(client, frame_addr=0x200013DC,
                               status_addr=0x2000175C,
                               spool_dir=tempfile.mkdtemp())
        grabber.raw()

        by_addr = {args["val"]: args["size"] for _, args in client.calls}
        self.assertEqual(by_addr[0x200013DC], FRAME_BYTES)
        self.assertEqual(by_addr[0x2000175C], STATUS_BYTES)

    def test_raw_returns_status_then_frame(self):
        client = StubClient()
        grabber = FrameGrabber(client, frame_addr=0x1000, status_addr=0x2000,
                               spool_dir=tempfile.mkdtemp())
        status, frame = grabber.raw()
        self.assertEqual(len(status), STATUS_BYTES)
        self.assertEqual(len(frame), FRAME_BYTES)

    def test_never_uses_pmemsave(self):
        """Regression guard: pmemsave silently reads the wrong memory.

        The framebuffer symbols are CPU virtual addresses. pmemsave treats its
        argument as physical and returns zeros, so the screen renders blank with
        no error raised. Verified against a live emulator: pmemsave gave 0 lit
        bits, memsave gave 1693, matching the gdb path exactly.
        """
        source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "uvk5_lcd.py")).read()
        code = "\n".join(line for line in source.splitlines()
                          if not line.lstrip().startswith("#")
                          and not line.lstrip().startswith("*"))
        # Allow the word inside the explanatory docstring, forbid a real call.
        self.assertNotIn('command("pmemsave"', code)

    def test_does_not_invoke_gdb(self):
        """Guard the design decision, not just the current behaviour.

        A gdb attach halts the guest, which would both stutter a live stream and
        perturb key debounce timing (see AGENTS.md). pmemsave leaves the guest
        running -- measured ~1.3 ms per frame with status still "running".

        Checks for the machinery needed to shell out, not for the word "gdb":
        the module mentions gdb in prose precisely to explain why it is avoided.
        """
        source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "uvk5_lcd.py")).read()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("subprocess", "os.system", "popen", "gdb-multiarch"):
            self.assertNotIn(forbidden, code.lower(),
                             f"{forbidden} must not appear: reading frames has "
                             "to stay on the QMP path")


if __name__ == "__main__":
    unittest.main()
