#!/usr/bin/env python3
"""Unit tests for firmware image detection. No emulator needed.

The distinction matters because getting it wrong is silent: an image loaded at the
wrong offset runs 0x2800 bytes off and the first fetch reads whatever data is there.
"""
import os
import struct
import tempfile
import unittest

import uvk5_image as ui


def write(tmp, name, data):
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def image(tmp, name, sp, reset, size=0x4000, extra_at_2800=None):
    """A file whose first two words are a vector table."""
    buf = bytearray(size)
    struct.pack_into("<II", buf, 0, sp, reset)
    if extra_at_2800 is not None:
        struct.pack_into("<II", buf, 0x2800, *extra_at_2800)
    return write(tmp, name, bytes(buf))


class TestDetect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_application_image(self):
        """Reset handler past the application offset: linked for 0x08002800."""
        path = image(self.tmp, "app.bin", 0x20004000, 0x08002d49)
        info = ui.detect(path)
        self.assertEqual(info.kind, "application")
        self.assertEqual(info.app_offset, ui.APP_OFFSET)

    def test_full_flash_image(self):
        """Entry point inside the bootloader region: address 0 aliases the base."""
        path = image(self.tmp, "bl.bin", 0x200032c0, 0x08000901,
                     extra_at_2800=(0x20004000, 0x08002d49))
        info = ui.detect(path)
        self.assertEqual(info.kind, "full-flash")
        self.assertEqual(info.app_offset, 0)

    def test_elf_is_passed_through(self):
        path = write(self.tmp, "app.elf", b"\x7fELF" + bytes(64))
        info = ui.detect(path)
        self.assertEqual(info.kind, "elf")
        self.assertEqual(info.app_offset, ui.APP_OFFSET)

    def test_rejects_a_file_with_no_vector_table(self):
        path = write(self.tmp, "junk.bin", b"not a firmware at all" * 8)
        with self.assertRaises(ui.ImageError):
            ui.detect(path)

    def test_rejects_an_sp_outside_sram(self):
        """A plausible reset handler is not enough; both words have to hold."""
        path = image(self.tmp, "bad.bin", 0x12345678, 0x08002d49)
        with self.assertRaises(ui.ImageError):
            ui.detect(path)

    def test_rejects_an_empty_file(self):
        path = write(self.tmp, "empty.bin", b"")
        with self.assertRaises(ui.ImageError):
            ui.detect(path)

    def test_rejects_a_missing_file(self):
        with self.assertRaises(ui.ImageError):
            ui.detect(os.path.join(self.tmp, "nope.bin"))

    def test_slot_can_be_swapped(self):
        slot = ui.ImageSlot()
        self.assertIsNone(slot.path)
        slot.set(image(self.tmp, "app.bin", 0x20004000, 0x08002d49))
        self.assertEqual(slot.app_offset, ui.APP_OFFSET)
        slot.set(image(self.tmp, "bl.bin", 0x200032c0, 0x08000901))
        self.assertEqual(slot.app_offset, 0)


if __name__ == "__main__":
    unittest.main()
