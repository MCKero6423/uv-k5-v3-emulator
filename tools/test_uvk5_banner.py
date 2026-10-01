#!/usr/bin/env python3
"""The page must report what the device says, not what it was asked to boot."""
import os
import tempfile
import unittest

import uvk5_banner


class TestBanner(unittest.TestCase):
    def test_it_finds_the_last_banner(self):
        lines = ["[qemu] SERIAL booting", "[qemu] SERIAL UV-K5 Firmware, EGZUMER+F4HWN v6.0.0.CN",
                 "[qemu] SERIAL noise", "[qemu] SERIAL UV-K5 Firmware, EGZUMER+F4HWN v5.9.0.CN"]
        self.assertEqual(uvk5_banner.latest(lines), "EGZUMER+F4HWN v5.9.0.CN")

    def test_no_banner_is_not_a_banner(self):
        self.assertIsNone(uvk5_banner.latest(["[qemu] SERIAL nothing to see", ""]))

    def test_a_name_that_differs_from_the_banner_is_not_a_mismatch(self):
        """f4hwn.fusion.bin legitimately reports v6.0.0.CN -- the string is in the file."""
        path = os.path.join(tempfile.mkdtemp(), "f4hwn.fusion.bin")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 32 + b"EGZUMER+F4HWN v6.0.0.CN" + b"\x00" * 32)
        self.assertTrue(uvk5_banner.image_mentions(path, "EGZUMER+F4HWN v6.0.0.CN"))

    def test_a_banner_the_image_does_not_contain_is_a_mismatch(self):
        """This is the 'the bootloader restored a slot instead' case."""
        path = os.path.join(tempfile.mkdtemp(), "wanted.bin")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 64 + b"EGZUMER+F4HWN v5.9.0.CN" + b"\x00" * 64)
        self.assertFalse(uvk5_banner.image_mentions(path, "EGZUMER+F4HWN v6.0.0.CN"))

    def test_a_missing_file_does_not_accuse_anything(self):
        self.assertTrue(uvk5_banner.image_mentions("/nonexistent/fw.bin", "anything"))


if __name__ == "__main__":
    unittest.main()
