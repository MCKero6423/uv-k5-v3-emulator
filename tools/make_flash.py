#!/usr/bin/env python3
"""Build the 2 MB SPI flash image the emulator boots from.

Starts from erased flash (0xFF) and drops the calibration dump at physical
0x010000, which is where driver/eeprom_compat.c maps the 512-byte calibration
block. Without it the firmware takes error branches in the frequency and power
paths, so the emulated radio would not represent a real one.

The image itself is not committed: it is 2 MB and fully derived from
assets/calibration.bin.

Usage: make_flash.py [--calibration FILE] [--out FILE]
"""

import argparse
import pathlib
import sys

FLASH_SIZE = 2 * 1024 * 1024
CALIBRATION_ADDR = 0x010000
CALIBRATION_SIZE = 512

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibration", type=pathlib.Path,
                    default=ASSETS / "calibration.bin")
    ap.add_argument("--out", type=pathlib.Path, default=ASSETS / "flash.img")
    args = ap.parse_args()

    if not args.calibration.is_file():
        raise SystemExit(f"calibration dump not found: {args.calibration}")

    cal = args.calibration.read_bytes()
    if len(cal) != CALIBRATION_SIZE:
        print(f"warning: calibration is {len(cal)} bytes, expected {CALIBRATION_SIZE}",
              file=sys.stderr)

    image = bytearray(b"\xff" * FLASH_SIZE)
    image[CALIBRATION_ADDR:CALIBRATION_ADDR + len(cal)] = cal

    args.out.write_bytes(image)
    print(f"wrote {args.out} ({len(image)} bytes)")
    print(f"  calibration at {CALIBRATION_ADDR:#08x}: "
          + " ".join(f"{b:02X}" for b in image[CALIBRATION_ADDR:CALIBRATION_ADDR + 8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
