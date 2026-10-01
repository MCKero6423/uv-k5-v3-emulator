#!/usr/bin/env python3
"""The app installer must refuse anything the firmware would show as APP ERROR.

The format is the firmware's own (App/apps/app_overlay.h): a 64-byte FAP1 header with a
zlib CRC-32 over the code, code at slot_base + 0x1000, 16 slots of 8 KiB from 0x102000.
These tests build blobs here, so they need no emulator and no third-party binary.
"""
import os
import struct
import unittest
import zlib

import uvk5_apps as A


def image(size=2 * 1024 * 1024, fill=0xFF):
    return bytearray([fill]) * size


class TestFormat(unittest.TestCase):
    def test_a_built_blob_round_trips(self):
        blob = A.build(b"\x00\x01\x02\x03" * 8, "Beam", "1.0", shortcut="beam")
        info = A.parse(blob)
        self.assertEqual(info["name"], "Beam")
        self.assertEqual(info["version"], "1.0")
        self.assertEqual(info["code_size"], 32)
        self.assertEqual(info["shortcut"], "beam")
        self.assertTrue(info["committed"])
        self.assertEqual(info["vma"], A.OVERLAY_VMA)      # 0x20000280, in SRAM
        self.assertEqual(info["capabilities"], 0)        # build() sets none by default

    def test_the_magic_is_the_one_the_firmware_defines(self):
        self.assertEqual(A.MAGIC.to_bytes(4, "little"), b"FAP1")
        self.assertEqual(A.REGION_BASE, 0x00102000)
        self.assertEqual(A.SLOT_STRIDE, 0x2000)
        self.assertEqual(A.SLOT_COUNT, 16)
        self.assertEqual(A.build(b"x", "A")[:4], b"FAP1")
        self.assertEqual(A.HDR_SIZE, 64, "the header is 64 bytes in app_overlay.h")
        self.assertEqual(A.OVERLAY_VMA, 0x20000280, "the overlay runs from SRAM")

    def test_a_bad_magic_is_refused(self):
        blob = bytearray(A.build(b"code", "A"))
        blob[0:4] = b"FMB1"
        with self.assertRaises(A.AppError):
            A.parse(bytes(blob))

    def test_a_crc_that_does_not_match_the_code_is_refused(self):
        blob = bytearray(A.build(b"code", "A"))
        blob[A.HDR_SIZE] ^= 0xFF          # change the code, leave the CRC
        with self.assertRaises(A.AppError) as caught:
            A.parse(bytes(blob))
        self.assertIn("CRC-32", str(caught.exception))

    def test_code_over_the_overlay_budget_is_refused(self):
        with self.assertRaises(A.AppError):
            A.build(b"x" * (A.APP_OVERLAY_MAX + 1), "Big")

    def test_a_truncated_blob_is_refused(self):
        blob = A.build(b"x" * 100, "A")
        with self.assertRaises(A.AppError):
            A.parse(blob[:A.HDR_SIZE + 50])

    def test_capabilities_round_trip(self):
        """An app may require a resident facility; it lives in the header's spare bytes."""
        blob = A.build(b"code", "FM", capabilities=0x01)
        self.assertEqual(A.parse(blob)["capabilities"], 0x01)

    def test_an_unknown_shortcut_is_refused(self):
        with self.assertRaises(A.AppError):
            A.build(b"x", "A", shortcut="solitaire")


class TestInstall(unittest.TestCase):
    def test_install_puts_the_header_and_the_code_where_the_firmware_looks(self):
        img = image()
        blob = A.build(b"\xAA" * 200, "Tetris", "1.0")
        info = A.install(img, 1, blob)
        base = A.REGION_BASE + A.SLOT_STRIDE
        self.assertEqual(bytes(img[base:base + 4]), b"FAP1")
        self.assertEqual(bytes(img[base + A.CODE_OFFSET:base + A.CODE_OFFSET + 4]), b"\xAA" * 4)
        # the gap a real erase leaves is 0xFF, not zeroes
        self.assertEqual(bytes(img[base + 64:base + A.CODE_OFFSET]), b"\xFF" * (A.CODE_OFFSET - 64))
        self.assertEqual(info["name"], "Tetris")

    def test_install_erases_what_was_there_before(self):
        img = image()
        A.install(img, 3, A.build(b"\x11" * 400, "Old", "1.0"))
        A.install(img, 3, A.build(b"\x22" * 100, "New", "1.0"))
        base = A.REGION_BASE + 3 * A.SLOT_STRIDE
        self.assertNotIn(b"\x11", bytes(img[base:base + A.SLOT_STRIDE]))
        self.assertEqual(A.read_slot(bytes(img), 3)["name"], "New")

    def test_listing_reports_the_slot_and_the_name(self):
        img = image()
        A.install(img, 0, A.build(b"\x01" * 64, "Beam"))
        A.install(img, 15, A.build(b"\x02" * 64, "Plasma"))
        found = A.list_apps(bytes(img))
        self.assertEqual([(a["slot"], a["name"]) for a in found], [(0, "Beam"), (15, "Plasma")])

    def test_an_empty_slot_is_not_an_app(self):
        self.assertEqual(A.list_apps(bytes(image())), [])

    def test_a_firmware_in_the_same_slot_is_named_as_one(self):
        """The 64-byte headers are shared: FMB1 is a firmware, FAP1 is an app."""
        img = image()
        base = A.REGION_BASE
        img[base:base + 4] = b"FMB1"
        img[base + 8:base + 12] = struct.pack("<I", 114 * 1024)
        found = A.list_apps(bytes(img))
        self.assertEqual(found[0]["kind"], "firmware")

    def test_erase_clears_the_slot(self):
        img = image()
        A.install(img, 2, A.build(b"\x33" * 64, "Gone"))
        A.erase(img, 2)
        self.assertIsNone(A.read_slot(bytes(img), 2))

    def test_it_refuses_to_overwrite_something_that_is_not_an_app(self):
        """Measured on a real image: the factory resource block can overlap the region."""
        img = image()
        base = A.REGION_BASE
        img[base:base + 8] = b"RESDATA1"
        with self.assertRaises(A.AppError) as caught:
            A.install(img, 0, A.build(b"\\x01" * 64, "Beam"))
        self.assertIn("already holds", str(caught.exception))
        A.install(img, 0, A.build(b"\\x01" * 64, "Beam"), force=True)
        self.assertEqual(A.read_slot(bytes(img), 0)["name"], "Beam")

    def test_installing_over_an_app_needs_no_force(self):
        img = image()
        A.install(img, 0, A.build(b"\\x11" * 64, "Old"))
        A.install(img, 0, A.build(b"\\x22" * 64, "New"))
        self.assertEqual(A.read_slot(bytes(img), 0)["name"], "New")

    def test_a_slot_outside_the_region_is_refused(self):
        with self.assertRaises(A.AppError):
            A.install(image(), 16, A.build(b"x", "A"))


class TestResolve(unittest.TestCase):
    def test_a_bare_name_is_found_under_work_apps(self):
        """The first attempt at this used 'Beam.app' from the repository root."""
        path = A.resolve("Beam.app")
        self.assertTrue(os.path.exists(path), path)

    def test_a_missing_name_says_where_it_looked(self):
        with self.assertRaises(A.AppError) as caught:
            A.resolve("NoSuchGame.app")
        self.assertIn("looked in", str(caught.exception))
        self.assertIn("work", str(caught.exception))


class TestRealFile(unittest.TestCase):
    """Runs against a downloaded Beam.app when one is present; skipped otherwise."""

    def test_a_real_upstream_app_parses(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "work", "apps", "Beam.app")
        if not os.path.exists(path):
            self.skipTest("no work/apps/Beam.app; download one from the upstream archive")
        with open(path, "rb") as fh:
            info = A.parse(fh.read())
        self.assertEqual(info["name"], "Beam")
        self.assertEqual(info["shortcut"], "beam")
        self.assertTrue(info["committed"])


if __name__ == "__main__":
    unittest.main()


class TestRadioReply(unittest.TestCase):
    """The firmware's own answer about a slot, decoded without a radio."""

    def test_a_reply_carrying_an_app_header_decodes(self):
        blob = A.build(b"\x01" * 32, "Beam", "1.0", shortcut="beam")
        reply = bytes([2, 0]) + blob[:A.HDR_SIZE]
        info = A.parse_radio_reply(reply)
        self.assertEqual(info["slot"], 2)
        self.assertEqual(info["status"], 0)
        self.assertEqual(info["name"], "Beam")
        self.assertEqual(info["version"], "1.0")
        self.assertEqual(info["code_size"], 32)
        self.assertEqual(info["shortcut"], "beam")
        self.assertTrue(info["committed"])

    def test_a_reply_from_a_slot_holding_something_else_is_not_an_app(self):
        """Measured on a real image: slots 1..3 answer status 2 with unrelated data."""
        info = A.parse_radio_reply(bytes([1, 2]) + b"\xd4\x56\xd1\xf4" + b"\x00" * 60)
        self.assertEqual(info["status"], 2)
        self.assertNotEqual(info.get("magic"), "FAP1")
        self.assertNotIn("name", info)

    def test_a_short_reply_says_so_instead_of_inventing_a_header(self):
        info = A.parse_radio_reply(bytes([0]))
        self.assertIsNone(info["status"])
        self.assertIn("short", info["note"])
