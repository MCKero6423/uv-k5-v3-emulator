#!/usr/bin/env python3
"""Build the 2 MB SPI flash image the emulator boots from.

Starts from erased flash (0xFF) and drops the calibration dump at physical
0x010000, which is where driver/eeprom_compat.c maps the 512-byte calibration
block. Without it the firmware takes error branches in the frequency and power
paths, so the emulated radio would not represent a real one.

The image itself is not committed: it is 2 MB and fully derived from
assets/calibration.bin.

Firmware that keeps data outside its own 128 KB -- the f4hwn/Chinese builds put
their font packs there -- needs that data placed too, at the same offsets the
real radio uses:

    --blob 0xA0000:pack.uf2          # raw file, or a .uf2 written by address
    --blob 0xA0000:font.bin          # plain blob at an explicit offset

A .uf2 is decoded block by block and each 256-byte payload is written to its own
target address, so the offsets come from the file rather than from a guess.

Usage: make_flash.py [--calibration FILE] [--out FILE] [--blob ADDR:FILE ...]
"""

import argparse
import pathlib
import struct
import sys

FLASH_SIZE = 2 * 1024 * 1024
CALIBRATION_ADDR = 0x010000
CALIBRATION_SIZE = 512

UF2_MAGIC0 = 0x0A324655
UF2_MAGIC1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"


def apply_uf2(image: bytearray, data: bytes, label: str) -> None:
    """Write every UF2 block to the address recorded in that block."""
    if len(data) % 512:
        raise SystemExit(f"{label}: not a UF2 image (size is not a multiple of 512)")
    written = 0
    for i in range(0, len(data), 512):
        m0, m1, _flags, addr, plen, _no, _num, _family = struct.unpack_from(
            "<IIIIIIII", data, i)
        # The closing magic sits at offset 508; the 476 bytes between the header
        # and it are the payload, of which payloadSize is meaningful.
        (mend,) = struct.unpack_from("<I", data, i + 508)
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or mend != UF2_MAGIC_END:
            raise SystemExit(f"{label}: bad UF2 magic at block {i // 512}")
        if plen > 476:
            raise SystemExit(f"{label}: block {i // 512} claims {plen} payload bytes")
        end = addr + plen
        if end > len(image):
            raise SystemExit(
                f"{label}: block {i // 512} writes 0x{addr:x}..0x{end:x}, "
                f"past the end of the {len(image)}-byte flash")
        image[addr:end] = data[i + 32:i + 32 + plen]
        written += plen
    print(f"  {label}: {written} bytes from UF2")


def apply_blob(image: bytearray, addr: int, data: bytes, label: str) -> None:
    end = addr + len(data)
    if end > len(image):
        raise SystemExit(f"{label}: 0x{addr:x}..0x{end:x} does not fit in flash")
    image[addr:end] = data
    print(f"  {label}: {len(data)} bytes at 0x{addr:08x}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibration", type=pathlib.Path,
                    default=ASSETS / "calibration.bin")
    ap.add_argument("--out", type=pathlib.Path, default=ASSETS / "flash.img")
    ap.add_argument("--blob", action="append", default=[], metavar="ADDR:FILE",
                    help="extra data to place; a .uf2 is decoded by its own "
                         "block addresses, anything else lands at ADDR")
    args = ap.parse_args()

    if not args.calibration.is_file():
        raise SystemExit(f"calibration dump not found: {args.calibration}")

    cal = args.calibration.read_bytes()
    if len(cal) != CALIBRATION_SIZE:
        print(f"warning: calibration is {len(cal)} bytes, expected {CALIBRATION_SIZE}",
              file=sys.stderr)

    image = bytearray(b"\xff" * FLASH_SIZE)
    image[CALIBRATION_ADDR:CALIBRATION_ADDR + len(cal)] = cal

    for spec in args.blob:
        if ":" not in spec:
            raise SystemExit(f"--blob wants ADDR:FILE, got {spec!r}")
        addr_text, name = spec.split(":", 1)
        path = pathlib.Path(name)
        if not path.is_file():
            raise SystemExit(f"--blob file not found: {path}")
        data = path.read_bytes()
        if path.suffix.lower() == ".uf2":
            apply_uf2(image, data, path.name)
        else:
            apply_blob(image, int(addr_text, 0), data, path.name)

    args.out.write_bytes(image)
    print(f"wrote {args.out} ({len(image)} bytes)")
    print(f"  calibration at {CALIBRATION_ADDR:#08x}: "
          + " ".join(f"{b:02X}" for b in image[CALIBRATION_ADDR:CALIBRATION_ADDR + 8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
