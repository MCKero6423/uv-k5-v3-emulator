#!/usr/bin/env python3
"""Overlay apps: where they live in the external flash, and what a .app contains.

The Labs edition of the F4HWN firmware runs small "overlay" apps -- Tetris, Breakout,
Plasma, Cube3D, Beam, Beacon, FoxHunt, BroadcastFM -- which the upstream UVStudio page
installs over WebSerial. This tool does the same thing to a flash **image**, which is
what the browser page in this repository owns: no serial protocol, no browser permission,
the same bytes at the same offsets.

The layout is the firmware's own, read out of the header it compiles rather than inferred
(`App/apps/app_overlay.h`):

    APP_REGION_BASE  0x00102000   first app slot, right behind the two state markers
    APP_SLOT_STRIDE  0x00002000   8 KiB per slot
    APP_CODE_OFFSET  0x00001000   code starts after the slot's 4 KiB header sector
    APP_SLOT_COUNT   16

So slot *n* holds a 64-byte header at `base + n*0x2000` and its code at
`base + n*0x2000 + 0x1000`. The header is `app_header_t`, little-endian and packed,
with magic `FAP1` and a zlib CRC-32 over the code -- the same CRC the multiboot loader
uses to validate a slot before offering RUN, which is why this tool checks it too.

The 64-byte header is shared with the multiboot slots: a slot whose header says `FMB1`
is a firmware, one that says `FAP1` is an app. That is how the power-on menu can list
both in the same four rows, and why "install to slot N" in UVStudio and "put a firmware
in slot N" in this page's slot table touch the same external flash.

    tools/uvk5_apps.py list    work/user-flash.img
    tools/uvk5_apps.py install work/user-flash.img 1 Beam.app
    tools/uvk5_apps.py erase   work/user-flash.img 1
"""
import argparse
import os
import struct
import sys
import zlib

MAGIC = 0x31504146           # "FAP1"
HDR_VERSION = 1
APP_OVERLAY_MAX = 0x1000     # 4 KiB of code per app
NAME_LEN = 16
VERSION_LEN = 16
HDR_SIZE = 64

REGION_BASE = 0x00102000
SLOT_STRIDE = 0x0002000
CODE_OFFSET = 0x00001000
SLOT_COUNT = 16
# The firmware's own names for these, so a reader can hold both side by side.
APP_REGION_BASE = REGION_BASE
APP_SLOT_STRIDE = SLOT_STRIDE
APP_CODE_OFFSET = CODE_OFFSET
APP_SLOT_COUNT = SLOT_COUNT
APP_OVERLAY_MAX_BYTES = APP_OVERLAY_MAX

FLAG_COMMITTED = 0x0001
FLAG_SCREEN_SAVER = 0x0002
FLAG_SHORTCUT_MASK = 0x0F00
FLAG_SHORTCUT_SHIFT = 8

SHORTCUTS = {0x01: "fm", 0x02: "foxhunt", 0x04: "beacon", 0x08: "beam"}

# magic | hdr_version | abi | api_min | code_size | crc32 | entry_off | flags | name | ver
#   | vma | capabilities | reserved
#
# vma is where the overlay is loaded and run: 0x20000280, inside SRAM, which is what the
# packer passes as --vma and what a real Beam.app carries at offset 52. Getting this field
# wrong is how the header came out 60 bytes instead of 64 in the first version here.
OVERLAY_VMA = 0x20000280
_HEADER = struct.Struct("<IHBBIIHH16s16sIII")
assert _HEADER.size == HDR_SIZE, _HEADER.size


class AppError(Exception):
    """A blob that is not an app this firmware could run."""


def slot_base(slot: int) -> int:
    if not 0 <= slot < SLOT_COUNT:
        raise AppError("slot %s is outside 0..%d" % (slot, SLOT_COUNT - 1))
    return REGION_BASE + slot * SLOT_STRIDE


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def parse(blob: bytes, strict: bool = True) -> dict:
    """The 64-byte header of a .app blob, validated.

    Strict by default: a bad magic, an oversized code section or a CRC that does not
    match the code means the firmware would show APP ERROR, so it is refused here where
    the reason can be said out loud.
    """
    if len(blob) < HDR_SIZE:
        raise AppError("only %d bytes: too short to hold the 64-byte header" % len(blob))
    (magic, hdr_version, abi, api_min, code_size, crc32, entry_off, flags,
     name, version, vma, cap, reserved) = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise AppError("magic is %r, not FAP1 -- this is not an app blob"
                       % blob[:4].decode("latin1", "replace"))
    info = dict(magic=magic, hdr_version=hdr_version, abi=abi, api_min=api_min,
                code_size=code_size, crc32=crc32, entry_off=entry_off, flags=flags,
                name=_text(name), version=_text(version), vma=vma, capabilities=cap,
                committed=bool(flags & FLAG_COMMITTED),
                screen_saver=bool(flags & FLAG_SCREEN_SAVER),
                shortcut=SHORTCUTS.get((flags & FLAG_SHORTCUT_MASK) >> FLAG_SHORTCUT_SHIFT,
                                       "none"),
                total=HDR_SIZE + code_size)
    if not strict:
        return info
    if hdr_version != HDR_VERSION:
        raise AppError("header version %d, this firmware writes %d" % (hdr_version, HDR_VERSION))
    if code_size == 0 or code_size > APP_OVERLAY_MAX:
        raise AppError("code_size %d is outside 1..%d" % (code_size, APP_OVERLAY_MAX))
    if len(blob) < HDR_SIZE + code_size:
        raise AppError("header says %d bytes of code but the file holds %d"
                       % (code_size, len(blob) - HDR_SIZE))
    actual = zlib.crc32(blob[HDR_SIZE:HDR_SIZE + code_size]) & 0xFFFFFFFF
    if actual != crc32:
        raise AppError("CRC-32 over the code is 0x%08X but the header says 0x%08X"
                       % (actual, crc32))
    if vma != OVERLAY_VMA:
        raise AppError("vma is 0x%08X; this firmware loads overlays at 0x%08X"
                       % (vma, OVERLAY_VMA))
    return info


def build(code: bytes, name: str, version: str = "1.0", abi: int = 1, api_min: int = 1,
          shortcut: str = "none", flags: int = FLAG_COMMITTED,
          capabilities: int = 0) -> bytes:
    """A .app blob around @code. The packer's inverse, for tests and for repacking."""
    if len(code) > APP_OVERLAY_MAX:
        raise AppError("code is %d bytes; the overlay budget is %d" % (len(code), APP_OVERLAY_MAX))
    for label, text in (("name", name), ("version", version)):
        if not text or len(text) >= (NAME_LEN if label == "name" else VERSION_LEN):
            raise AppError("%s must be 1..%d characters" % (label, NAME_LEN - 1))
    if shortcut != "none":
        matches = [k for k, v in SHORTCUTS.items() if v == shortcut]
        if not matches:
            raise AppError("unknown shortcut %r; known: %s"
                           % (shortcut, ", ".join(sorted(SHORTCUTS.values()))))
        flags |= matches[0] << FLAG_SHORTCUT_SHIFT
    header = _HEADER.pack(MAGIC, HDR_VERSION, abi, api_min, len(code),
                          zlib.crc32(code) & 0xFFFFFFFF, 0, flags,
                          name.encode("ascii")[:NAME_LEN - 1].ljust(NAME_LEN, b"\x00"),
                          version.encode("ascii")[:VERSION_LEN - 1].ljust(VERSION_LEN, b"\x00"),
                          OVERLAY_VMA, capabilities, 0)
    return header + code


def read_slot(image: bytes, slot: int) -> dict:
    """What slot @slot holds, or None when its header is not an app."""
    base = slot_base(slot)
    if base + SLOT_STRIDE > len(image):
        return None
    raw = image[base:base + SLOT_STRIDE]
    if raw[:4] == b"\xff\xff\xff\xff" or not any(raw):
        return None
    if raw[:4] != b"FAP1":
        return dict(slot=slot, base=base, magic=raw[:4].decode("latin1", "replace"),
                    kind="firmware" if raw[:4] == b"FMB1" else "unknown")
    try:
        info = parse(raw[:HDR_SIZE] + raw[CODE_OFFSET:CODE_OFFSET + struct.unpack_from("<I", raw, 8)[0]],
                     strict=False)
    except AppError:
        return dict(slot=slot, base=base, magic="FAP1", kind="app (unreadable header)")
    info.update(slot=slot, base=base, kind="app")
    return info


def list_apps(image: bytes):
    return [info for info in (read_slot(image, i) for i in range(SLOT_COUNT)) if info]


def install(image: bytearray, slot: int, blob: bytes, force: bool = False) -> dict:
    """Write @blob into @slot: header at the base, code at +0x1000, rest erased.

    The header sector is erased first, the way the flash would be: an install must not
    leave a byte of the previous app behind for the loader to trip over.

    Refuses to overwrite a slot that holds something which is neither empty nor an app.
    Measured on a real image: 0x102000..0x122000 can already carry data (the factory
    resource block of a localised build overlaps it), and an install there silently
    destroys it. `force` is for when that is what you meant.
    """
    info = parse(blob)
    base = slot_base(slot)
    if base + SLOT_STRIDE > len(image):
        raise AppError("the image is too small for slot %d" % slot)
    if not force:
        occupied = read_slot(bytes(image), slot)
        if occupied is not None and occupied.get("kind") not in ("app",):
            raise AppError(
                "slot %d at 0x%06X already holds %s (%r), not an app; erase it first "
                "or pass force to overwrite" % (slot, base, occupied.get("kind", "data"),
                                                (occupied.get("name") or "")[:16]))
    for i in range(base, base + SLOT_STRIDE):
        image[i] = 0xFF
    image[base:base + HDR_SIZE] = blob[:HDR_SIZE]
    code = blob[HDR_SIZE:HDR_SIZE + info["code_size"]]
    image[base + CODE_OFFSET:base + CODE_OFFSET + len(code)] = code
    return info


def erase(image: bytearray, slot: int) -> int:
    base = slot_base(slot)
    if base + SLOT_STRIDE > len(image):
        raise AppError("the image is too small for slot %d" % slot)
    for i in range(base, base + SLOT_STRIDE):
        image[i] = 0xFF
    return base


def resolve(name: str) -> str:
    """A path to the .app named @name, looking in the places it usually is.

    The first person to try this typed `tools/uvk5_apps.py install image 1 Beam.app` from the
    repository root, where no such file exists: the tool said FileNotFoundError and nothing
    else. The apps live in work/apps/ once downloaded, and the bare name is what everybody
    types, so both are accepted and the refusal lists where it looked.
    """
    if os.path.isabs(name) or os.path.exists(name):
        return name
    roots = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "work", "apps"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "apps"),
             "."]
    tried = []
    for root in roots:
        candidate = os.path.join(root, name)
        tried.append(candidate)
        if os.path.exists(candidate):
            return candidate
    raise AppError("no %s; looked in %s" % (name, ", ".join(os.path.normpath(t) for t in tried)))


def apps_json(path: str) -> dict:
    """Every app slot as a row, for the page: one entry per slot, occupied or not.

    The page shows all of them rather than only the occupied ones, because choosing the
    slot is part of installing, and the radio itself prints Empty for a free one.
    """
    with open(path, "rb") as fh:
        image = fh.read()
    occupied = {info["slot"]: info for info in list_apps(image)}
    rows = []
    for slot in range(SLOT_COUNT):
        row = dict(slot=slot, base=slot_base(slot))
        info = occupied.get(slot)
        if info is None:
            row["state"] = "empty"
        elif info.get("kind") == "app":
            row.update(state="app", name=info["name"], version=info["version"],
                       code_size=info["code_size"], shortcut=info["shortcut"],
                       committed=info["committed"], crc32=info["crc32"])
        else:
            row.update(state=info.get("kind", "unknown"), name=info.get("magic", "?"))
        rows.append(row)
    return dict(region=REGION_BASE, slot_count=SLOT_COUNT, stride=SLOT_STRIDE,
                code_offset=CODE_OFFSET, overlay_max=APP_OVERLAY_MAX, slots=rows)


def install_file(path: str, slot: int, blob: bytes, force: bool = False) -> dict:
    """Install @blob into @slot of the flash image at @path, in place."""
    image = _read(path)
    info = install(image, slot, blob, force=force)
    with open(path, "wb") as fh:
        fh.write(image)
    return info


def erase_file(path: str, slot: int) -> int:
    """Clear @slot of the flash image at @path."""
    image = _read(path)
    base = erase(image, slot)
    with open(path, "wb") as fh:
        fh.write(image)
    return base



def parse_radio_reply(raw: bytes) -> dict:
    """The firmware's 0x0731 answer: [slot, status, app_header_t].

    A pure function so the decoding can be tested without a radio. The header comes back
    as the same 64-byte structure this module writes, which is the point: the firmware
    reading its own region and answering is what proves an install, not the file we wrote.
    """
    raw = bytes(raw)
    if len(raw) < 2:
        return dict(status=None, note="short reply")
    out = dict(slot=raw[0], status=raw[1])
    if len(raw) >= 2 + HDR_SIZE:
        header = raw[2:2 + HDR_SIZE]
        if header[:4] == b"FAP1":
            try:
                info = parse(header, strict=False)   # no code here, so no CRC to check
            except AppError as exc:
                out["note"] = str(exc)
                return out
            out.update(name=info["name"], version=info["version"], code_size=info["code_size"],
                       crc32=info["crc32"], shortcut=info["shortcut"],
                       committed=info["committed"], magic="FAP1")
            return out
        out["magic"] = header[:4].decode("latin1", "replace")
    return out


def _read(path: str) -> bytearray:
    with open(path, "rb") as fh:
        return bytearray(fh.read())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="what each app slot holds")
    p.add_argument("image")
    p = sub.add_parser("install", help="write a .app into a slot")
    p.add_argument("image")
    p.add_argument("slot", type=int)
    p.add_argument("app")
    p.add_argument("--force", action="store_true",
                   help="overwrite a slot holding something other than an app")
    p = sub.add_parser("erase", help="clear a slot")
    p.add_argument("image")
    p.add_argument("slot", type=int)
    p = sub.add_parser("info", help="describe a .app file without installing it")
    p.add_argument("app")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "info":
            with open(resolve(args.app), "rb") as fh:
                info = parse(fh.read())
            print("%s %s  %d bytes of code (blob %d)  ABI %d  api>=%d  shortcut %s  CRC 0x%08X"
                  % (info["name"], info["version"], info["code_size"], info["total"], info["abi"],
                     info["api_min"], info["shortcut"], info["crc32"]))
            return 0
        image = _read(args.image)
        if args.cmd == "list":
            found = list_apps(image)
            if not found:
                print("no apps installed (%d slots at 0x%06X)" % (SLOT_COUNT, REGION_BASE))
            for info in found:
                if info.get("kind") == "app":
                    print("slot %2d @0x%06X  %-16s %-6s %5d bytes  %s"
                          % (info["slot"], info["base"], info["name"], info["version"],
                             info["code_size"], info["shortcut"]))
                else:
                    print("slot %2d @0x%06X  %s" % (info["slot"], info["base"], info["kind"]))
            return 0
        with open(resolve(args.app), "rb") as fh:
            blob = fh.read()
        if args.cmd == "install":
            info = install(image, args.slot, blob, force=args.force)
            with open(args.image, "wb") as fh:
                fh.write(image)
            print("installed %s %s into slot %d at 0x%06X (%d bytes)"
                  % (info["name"], info["version"], args.slot, slot_base(args.slot),
                     info["code_size"]))
            return 0
        if args.cmd == "erase":
            base = erase(image, args.slot)
            with open(args.image, "wb") as fh:
                fh.write(image)
            print("erased slot %d at 0x%06X" % (args.slot, base))
            return 0
    except AppError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
