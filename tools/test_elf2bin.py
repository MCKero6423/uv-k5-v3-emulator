#!/usr/bin/env python3
"""The ELF extractor must follow *sections*, not program headers.

lld maps the ELF header and program-header table as a LOAD of its own -- 52 + 4x32 = 180
bytes at the image base, with no section content in it. An extractor that follows program
headers therefore puts a phantom 180-byte region below the first real section, and the
image is refused with APP_ERR_VMA or, worse, built from the wrong base. These tests build
the ELFs by hand, so they need no toolchain.
"""
import struct
import unittest
import os
import tempfile

import elf2bin


def build_elf(body=b"\x11" * 64, with_phantom_phdr=False):
    """A minimal 32-bit little-endian ELF with one allocated section holding @body.

    With @with_phantom_phdr the first program header is a LOAD covering the ELF header and
    the program-header table itself -- no section content, exactly what lld emits -- which
    must not become part of the extracted image.
    """
    ehsize, phentsize, shentsize = 52, 32, 40
    phnum = 2 if with_phantom_phdr else 1
    phoff = ehsize
    data_off = phoff + phnum * phentsize
    shoff = data_off + len(body)
    blob = bytearray(shoff + shentsize)
    blob[0:4] = b"\x7fELF"
    blob[4] = 1                                  # 32-bit
    blob[5] = 1                                  # little endian
    struct.pack_into("<I", blob, 0x1C, phoff)
    struct.pack_into("<H", blob, 0x2A, phentsize)
    struct.pack_into("<H", blob, 0x2C, phnum)
    struct.pack_into("<I", blob, 0x20, shoff)
    struct.pack_into("<H", blob, 0x2E, shentsize)
    struct.pack_into("<H", blob, 0x30, 1)
    blob[data_off:data_off + len(body)] = body   # the section's file content
    i = 0
    if with_phantom_phdr:
        struct.pack_into("<IIIIIIII", blob, phoff, 1, 0, 0x20000000, 0x20000000,
                         ehsize + phentsize, 0, 4, 4)
        i = 1
    struct.pack_into("<IIIIIIII", blob, phoff + i * phentsize, 1, data_off, 0x20000280,
                     0x20000280, len(body), len(body), 5, 4)
    struct.pack_into("<IIIIIIIIII", blob, shoff, 0, 1, 0x6, 0x20000280,
                     data_off, len(body), 0, 0, 4, 0)
    return bytes(blob)


class TestExtract(unittest.TestCase):
    def _extract(self, blob, minimum=0x20000280):
        path = os.path.join(tempfile.mkdtemp(), "in.elf")
        out = os.path.join(os.path.dirname(path), "out.bin")
        with open(path, "wb") as fh:
            fh.write(blob)
        elf2bin.main([path, out, "--min", hex(minimum), "--quiet"])
        with open(out, "rb") as fh:
            return fh.read()

    def test_a_plain_single_section_elf(self):
        blob = build_elf()
        self.assertEqual(self._extract(blob), b"\x11" * 64)

    def test_the_phantom_header_segment_is_not_part_of_the_image(self):
        """This is the case that broke the first version: 0x20000000 is not the base."""
        blob = build_elf(with_phantom_phdr=True)
        self.assertEqual(self._extract(bytes(blob)), b"\x11" * 64)

    def test_a_wrong_base_is_refused_rather_than_silently_shifted(self):
        blob = build_elf()
        with self.assertRaises(SystemExit):
            self._extract(bytes(blob), minimum=0x20000000)

    def test_something_that_is_not_an_elf_is_refused(self):
        with self.assertRaises(SystemExit):
            self._extract(b"not an elf at all" + b"\x00" * 64)


if __name__ == "__main__":
    unittest.main()
