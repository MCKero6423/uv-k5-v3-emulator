#!/usr/bin/env python3
"""Unit tests for LCD unpacking and PNG encoding. No emulator needed."""
import struct
import unittest
import zlib

from uvk5_lcd import LCD_HEIGHT, LCD_WIDTH, encode_png, unpack


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


if __name__ == "__main__":
    unittest.main()
