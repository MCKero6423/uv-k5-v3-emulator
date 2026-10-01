#!/usr/bin/env python3
"""Wrap a raw V3 application .bin in an ELF32/ARM header so QEMU can load it.

Firmware releases for the UV-K5 V3 / UV-K1 ship as a flat application image: the
file starts at its own vector table, which the linker places at 0x08002800, past
the 10 KB bootloader region. An .elf from the same build carries that address in
its program headers; a .bin has nowhere to put it.

This adds the missing program header, so a release .bin loads exactly where the
.elf would:

    read the initial SP and reset vector from the image's own vector table
    emit one PT_LOAD at p_paddr = 0x08002800, entry = the reset vector

Usage:
    bin2elf.py FIRMWARE.bin [-o FIRMWARE.elf] [--base 0x08002800]

Then, as with any other image:

    qemu-system-arm -M "uv-k5-v3,flash-image=assets/flash.img" -kernel FIRMWARE.elf ...

Guessing is not needed and is not done: the base defaults to the model's
PY32_APP_OFFSET and is checked against the reset vector, so an image linked for a
different base is refused rather than silently loaded in the wrong place.
"""
import argparse
import struct
import sys

APP_BASE_DEFAULT = 0x08002800   # qemu/py32f071.c: PY32_FLASH_BASE | PY32_APP_OFFSET
VECTOR_TABLE_WORDS = 48         # 0xc0 bytes, per the linker script's .isr_vector

EM_ARM = 40
ET_EXEC = 2
PT_LOAD = 1
EF_ARM_EABI_VER5 = 0x05000000


def build_elf(image: bytes, base: int) -> bytes:
    if len(image) < 8:
        raise SystemExit("image is too short to hold a vector table")
    sp, reset = struct.unpack_from("<II", image, 0)
    if not (0x20000000 <= sp <= 0x20004000):
        raise SystemExit(
            f"initial SP 0x{sp:08x} is not in SRAM -- is this an application image?")
    if (reset & ~1) < base or (reset & ~1) >= base + len(image):
        raise SystemExit(
            f"reset vector 0x{reset:08x} does not land inside the image loaded at "
            f"0x{base:08x} -- wrong --base?")

    p_offset = base & 0xFFFF          # keeps p_offset congruent with p_vaddr
    ehdr = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\x00" * 9,  # 32-bit LE, System V ABI
        ET_EXEC, EM_ARM, 1, reset, 52, 0, EF_ARM_EABI_VER5,
        52, 32, 1, 40, 0, 0)
    phdr = struct.pack(
        "<IIIIIIII",
        PT_LOAD, p_offset, base, base, len(image), len(image), 5, 0x1000)
    pad = b"\x00" * (p_offset - len(ehdr) - len(phdr))
    return ehdr + phdr + pad + image


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("-o", "--out")
    ap.add_argument("--base", type=lambda s: int(s, 0), default=APP_BASE_DEFAULT)
    args = ap.parse_args()

    data = open(args.image, "rb").read()
    out = args.out or (args.image.rsplit(".", 1)[0] + ".elf")
    elf = build_elf(data, args.base)
    open(out, "wb").write(elf)

    sp, reset = struct.unpack_from("<II", data, 0)
    print(f"{args.image} -> {out}")
    print(f"  {len(data)} bytes at 0x{args.base:08x}")
    print(f"  initial SP 0x{sp:08x}, entry 0x{reset:08x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
