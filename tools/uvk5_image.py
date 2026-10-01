#!/usr/bin/env python3
"""Which image to boot, and where the loader has to put it.

Firmware arrives in two shapes and the difference is not cosmetic:

* an **application** image is linked for 0x08002800. Its first bytes *are* its vector
  table, and address 0 aliases the application region so the reset fetch finds it.
* a **full-flash** image starts at 0x08000000 -- a bootloader, whose entry point
  lives in the bootloader region (0x08000000..0x080027FF), with the application
  embedded behind it.

Get that wrong and nothing complains: the image lands 0x2800 bytes off and the first
fetch reads whatever data is there (0xFF, usually, so it faults). The shape is
readable from the image itself, so this reads the vector table rather than asking the
user or guessing from a file name.
"""
import os
import struct

FLASH_BASE = 0x08000000
APP_OFFSET = 0x2800
SRAM_BASE = 0x20000000
SRAM_TOP = 0x20004000                  # 16 KB of SRAM, so SP starts at the top
ELF_MAGIC = b"\x7fELF"
MAX_IMAGE = 4 * 1024 * 1024            # a full 2 MB flash dump plus slack


class ImageError(ValueError):
    """The file is not an image this machine can boot."""


class ImageInfo:
    def __init__(self, path, kind, app_offset, size, sp=None, reset=None):
        self.path = path
        self.kind = kind                 # "application" | "full-flash" | "elf"
        self.app_offset = app_offset     # flash offset address 0 must alias
        self.size = size
        self.sp = sp
        self.reset = reset

    def as_dict(self):
        return {
            "name": os.path.basename(self.path),
            "path": self.path,
            "kind": self.kind,
            "app_offset": self.app_offset,
            "size": self.size,
            "sp": None if self.sp is None else "0x%08x" % self.sp,
            "reset": None if self.reset is None else "0x%08x" % self.reset,
        }

    def __repr__(self):
        return "<ImageInfo %s %s %dB app_offset=0x%x>" % (
            self.kind, os.path.basename(self.path), self.size, self.app_offset)


def _vector_at(data, offset):
    """(SP, reset) if a plausible vector table starts at @offset, else None.

    Both words are checked against what the hardware can accept: SP inside SRAM, and
    a reset handler inside the image itself. A single plausible word is a
    coincidence in code, so a half-match is treated as no match.
    """
    if offset + 8 > len(data):
        return None
    sp, reset = struct.unpack_from("<II", data, offset)
    if not (SRAM_BASE < sp <= SRAM_TOP):
        return None
    if not (FLASH_BASE <= reset < FLASH_BASE + len(data)):
        return None
    return sp, reset


def detect(path):
    """Work out how to load @path. Raises ImageError if it cannot be booted."""
    if not os.path.isfile(path):
        raise ImageError("no such file: %s" % path)
    size = os.path.getsize(path)
    if size == 0:
        raise ImageError("empty file")
    if size > MAX_IMAGE:
        raise ImageError("%d bytes is larger than any UV-K5 image" % size)
    with open(path, "rb") as fh:
        data = fh.read()

    if data[:4] == ELF_MAGIC:
        # An ELF carries its own program headers, so the loader base is irrelevant;
        # the app offset only decides what address 0 aliases, which for an
        # application ELF is the application region.
        return ImageInfo(path, "elf", APP_OFFSET, size)

    vector = _vector_at(data, 0)
    if vector is None:
        raise ImageError(
            "not a bootable image: no vector table at offset 0 (the first word must "
            "be an SRAM address and the second a handler inside the file)")
    sp, reset = vector

    # A full-flash image's entry point lives in the bootloader region, ahead of where
    # the application starts. That is the whole distinction -- not the size, and not
    # a second vector table, because an application's code can contain anything at
    # offset 0x2800.
    if reset < FLASH_BASE + APP_OFFSET:
        return ImageInfo(path, "full-flash", 0, size, sp, reset)
    return ImageInfo(path, "application", APP_OFFSET, size, sp, reset)


class ImageSlot:
    """The image the next launch boots.

    Mutable on purpose: an upload has to take effect at the next power-on without
    restarting the server, and the launcher reads this at spawn time rather than
    closing over a path chosen when the server started.
    """

    def __init__(self, path=None):
        self.current = detect(path) if path else None

    def set(self, path_or_info):
        """Load an image, or adopt one that has already been detected.

        Accepting an ImageInfo lets a caller validate a file *before* disturbing
        anything: an upload that turns out not to be an image must not leave the
        radio powered off.
        """
        self.current = (path_or_info if isinstance(path_or_info, ImageInfo)
                        else detect(path_or_info))
        return self.current

    def clear(self):
        self.current = None

    @property
    def path(self):
        return self.current.path if self.current else None

    @property
    def app_offset(self):
        return self.current.app_offset if self.current else None
