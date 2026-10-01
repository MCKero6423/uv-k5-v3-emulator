#!/usr/bin/env python3
"""Fetch a released firmware build, so the tests are not anchored to one machine.

Several emulator tests need a real image to run against: the multi-system slot tests
need a build with ENABLE_FEAT_F4HWN_MULTIBOOT (the 0x0720..0x0727 commands are inside
that ifdef), and a plain application image is useful for the rest. Neither is in this
repository -- firmware is not ours to redistribute -- and pointing tests at one
developer's build directory is what makes a suite unreproducible.

So the tests read a documented directory, `assets/firmware/`, and skip with a message
naming this tool when it is empty. Run it once:

    python3 tools/fetch_firmware.py                 # fieldops-v6.0.0, the multiboot one
    python3 tools/fetch_firmware.py --list
    python3 tools/fetch_firmware.py --all

Files come from the upstream project's own archive, through jsDelivr, and each one
prints its SHA-256 so a fetch can be compared with someone else's.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEST_DIR = os.path.join(ROOT, "assets", "firmware")

# armel/uv-k1-k5v3-firmware-custom, the firmware the emulator is built for. The v6.0.0
# builds are the ones with the multi-system boot menu; the localised releases floating
# around are often built without it.
BASE = "https://cdn.jsdelivr.net/gh/armel/uv-k1-k5v3-firmware-custom@main/"

RELEASES = {
    "fieldops-v6.0.0": ("archive/f4hwn.fieldops.v6.0.0.bin",
                        "application image with the multi-system boot menu"),
    "fusion-v6.0.0": ("archive/f4hwn.fusion.v6.0.0.bin",
                      "application image with the multi-system boot menu"),
    "fusion-v5.9.0": ("archive/f4hwn.fusion.v5.9.0.bin", "application image, no boot menu"),
}


def fetch(name: str, dest_dir: str = DEST_DIR) -> str:
    if name not in RELEASES:
        raise SystemExit("unknown release %r; --list shows what there is" % name)
    path, _why = RELEASES[name]
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, os.path.basename(path))
    url = BASE + path
    print("fetching %s" % url)
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read()
    if not blob:
        raise SystemExit("empty response from %s" % url)
    with open(out, "wb") as fh:
        fh.write(blob)
    print("wrote %s (%d bytes)" % (out, len(blob)))
    print("sha256 %s" % hashlib.sha256(blob).hexdigest())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="show the known releases")
    ap.add_argument("--all", action="store_true", help="fetch everything")
    ap.add_argument("names", nargs="*", help="release names (default: fieldops-v6.0.0)")
    args = ap.parse_args(argv)

    if args.list:
        for name, (path, why) in sorted(RELEASES.items()):
            print("%-18s %-40s %s" % (name, path, why))
        return 0
    names = sorted(RELEASES) if args.all else (args.names or ["fieldops-v6.0.0"])
    for name in names:
        fetch(name)
    print("")
    print("tests look in %s now; UVK5_MULTIBOOT_IMAGE overrides the path"
          % DEST_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
