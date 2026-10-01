"""External-flash firmware slots, in the format the multi-system firmware reads.

The layout and the header come from the firmware source, not from a datasheet or from
the vendor's partition map:

    mb_mb_flash.h   MB_SLOT_STRIDE 0x20000, slot 1 base 0x040000, backup slot 0 0x020000
                    mb_slot_header_t { magic "FMB1", hdr_version, flags, image_size,
                                       image_crc32, name[16], fw_version[16], reserved[16] }
                    MB_FLAG_COMMITTED = 1

The image itself sits one 4 KiB sector past the slot base, so the header owns the first
sector on its own -- MB_ValidateSlot() checks the magic and the CRC over image_size
bytes, and the firmware reflashes itself from exactly this data when a slot is restored.

Why a small sector of slack: the multiboot code erases and programs in whole sectors, so
a header sharing a sector with the image could not be updated without rewriting the
image. The header's flags field says whether a slot is committed; an erased slot reads
0xFF everywhere and validates as MB_ERR_MAGIC.
"""

from __future__ import annotations

import os
import struct
import zlib

SLOT_MAGIC = 0x31424D46          # "FMB1"
HDR_VERSION = 1
MB_FLAG_COMMITTED = 1

SLOT_BACKUP_BASE = 0x020000      # slot 0: the backup of the internal firmware
SLOT_STRIDE = 0x20000            # 128 KiB per slot
SLOT_IMAGE_OFFSET = 0x1000       # image starts one sector in
SLOT_COUNT = 5                   # backup + 4
SLOT_HEADER_SIZE = 64

# From mb_mb_flash.h: the two redundant active-state sectors.
STATE_A_BASE = 0x00100000
STATE_B_BASE = 0x00101000
STATE_SIZE = 24                  # mb_state_t, packed


def slot_base(slot: int) -> int:
    """External-flash base of @slot: 0 is the backup, 1..4 follow it."""
    if slot == 0:
        return SLOT_BACKUP_BASE
    if 1 <= slot < SLOT_COUNT:
        return SLOT_BACKUP_BASE + slot * SLOT_STRIDE
    raise ValueError("slot %r out of range (0..%d)" % (slot, SLOT_COUNT - 1))


def build_header(image: bytes, name: str = "", fw_version: str = "") -> bytes:
    """The 64-byte header for @image, committed."""
    if len(image) > SLOT_STRIDE - SLOT_IMAGE_OFFSET:
        raise ValueError("image is %d bytes; a slot holds %d"
                         % (len(image), SLOT_STRIDE - SLOT_IMAGE_OFFSET))
    return struct.pack(
        "<IHHII16s16s16s",
        SLOT_MAGIC,
        HDR_VERSION,
        MB_FLAG_COMMITTED,
        len(image),
        zlib.crc32(image) & 0xFFFFFFFF,
        name.encode("ascii", "replace")[:15].ljust(16, b"\x00"),
        fw_version.encode("ascii", "replace")[:15].ljust(16, b"\x00"),
        b"\x00" * 16,
    )


def write_slot(buf: bytearray, slot: int, image: bytes, name: str = "",
               fw_version: str = "") -> None:
    """Lay @image into @slot of @buf, with a committed header."""
    base = slot_base(slot)
    header = build_header(image, name, fw_version)
    buf[base:base + SLOT_HEADER_SIZE] = header
    start = base + SLOT_IMAGE_OFFSET
    buf[start:start + len(image)] = image


def erase_slot(buf: bytearray, slot: int) -> None:
    """Leave @slot as an erased (invalid) slot."""
    base = slot_base(slot)
    buf[base:base + SLOT_STRIDE] = b"\xff" * SLOT_STRIDE


def read_slot(buf: bytes, slot: int):
    """(header dict, image bytes) for @slot, or (None, None) when it is not committed."""
    base = slot_base(slot)
    magic, ver, flags, size, crc = struct.unpack_from("<IHHII", buf, base)
    if magic != SLOT_MAGIC or not (flags & MB_FLAG_COMMITTED):
        return None, None
    name = buf[base + 16:base + 32].split(b"\x00")[0].decode("ascii", "replace")
    fw = buf[base + 32:base + 48].split(b"\x00")[0].decode("ascii", "replace")
    image = bytes(buf[base + SLOT_IMAGE_OFFSET:base + SLOT_IMAGE_OFFSET + size])
    return ({"slot": slot, "base": base, "hdr_version": ver, "flags": flags,
             "image_size": size, "image_crc32": crc, "crc_ok": (zlib.crc32(image) & 0xFFFFFFFF) == crc,
             "name": name, "fw_version": fw}, image)


def erase_state(buf: bytearray) -> None:
    """Erase both active-state sectors, so the firmware treats the flash as fresh.

    This matters: a *corrupt* marker plus a valid slot 0 makes the boot path halt with
    "STATE ERROR" to protect Main (mb_multiboot.c), while a *missing* one lets it look
    at the slots and record what it finds.
    """
    buf[STATE_A_BASE:STATE_A_BASE + STATE_SIZE] = b"\xff" * STATE_SIZE
    buf[STATE_B_BASE:STATE_B_BASE + STATE_SIZE] = b"\xff" * STATE_SIZE


def build(base_image_path: str, slots, out_path: str, erase_state_sectors: bool = True) -> str:
    """Copy @base_image_path and put @slots (a list of (slot, image_path, name, version)) in it.

    @slots entries: (slot_number, path_to_bin, name, fw_version).
    """
    with open(base_image_path, "rb") as fh:
        buf = bytearray(fh.read())
    for slot, image_path, name, version in slots:
        with open(image_path, "rb") as fh:
            image = fh.read()
        write_slot(buf, slot, image, name, version)
    if erase_state_sectors:
        erase_state(buf)
    with open(out_path, "wb") as fh:
        fh.write(buf)
    return out_path


def describe(path: str) -> str:
    """A human-readable summary of every slot in a flash image."""
    with open(path, "rb") as fh:
        buf = fh.read()
    lines = ["%s (%d bytes)" % (path, len(buf))]
    for slot in range(SLOT_COUNT):
        header, image = read_slot(buf, slot)
        if header is None:
            lines.append("  slot %d @0x%06x: empty" % (slot, slot_base(slot)))
        else:
            lines.append("  slot %d @0x%06x: %-16s %-16s %7d bytes crc %08x %s"
                         % (slot, header["base"], header["name"], header["fw_version"],
                            header["image_size"], header["image_crc32"],
                            "ok" if header["crc_ok"] else "MISMATCH"))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="inspect or build external-flash firmware slots")
    ap.add_argument("image", help="flash image to inspect")
    ap.add_argument("--base", help="base image to copy when building")
    ap.add_argument("--slot", type=int, action="append", default=[],
                    help="slot number to fill (repeat; pair with --bin in order)")
    ap.add_argument("--bin", action="append", default=[], help="image for the matching --slot")
    ap.add_argument("--name", action="append", default=[], help="slot name for the matching --slot")
    ap.add_argument("--out", help="where to write the built image")
    args = ap.parse_args()

    if args.base and args.out:
        slots = []
        for i, slot in enumerate(args.slot):
            name = args.name[i] if i < len(args.name) else os.path.basename(args.bin[i])
            slots.append((slot, args.bin[i], name, ""))
        print("wrote", build(args.base, slots, args.out))
    print(describe(args.out or args.image))


# --------------------------------------------------------------------- editing files

def load_image(path: str) -> bytearray:
    with open(path, "rb") as fh:
        return bytearray(fh.read())


def save_image(path: str, buf: bytes) -> None:
    """Write @buf to @path, atomically enough that a crash cannot truncate the image.

    The emulator writes this same file back while it runs, so a half-written image is
    a real possibility -- and a truncated flash image looks like a fresh radio.
    """
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def slots_json(path: str):
    """The slot table as plain data, for a page or a JSON API."""
    buf = load_image(path)
    slots = []
    for slot in range(SLOT_COUNT):
        header, _image = read_slot(buf, slot)
        row = {"slot": slot, "base": slot_base(slot), "empty": header is None}
        if header is not None:
            row.update({k: header[k] for k in
                        ("name", "fw_version", "image_size", "image_crc32", "crc_ok",
                         "hdr_version", "flags")})
        slots.append(row)
    return {"image": os.path.abspath(path), "name": os.path.basename(path),
            "size": len(buf), "slots": slots}


def write_slot_file(path: str, slot: int, image: bytes, name: str = "",
                    fw_version: str = "", erase_state_sectors: bool = True) -> dict:
    """Put @image into @slot of the flash image at @path, in place."""
    buf = load_image(path)
    write_slot(buf, slot, image, name, fw_version)
    if erase_state_sectors:
        # The firmware's boot path treats a corrupt active-state marker next to a valid
        # slot 0 as "halt and protect Main" (mb_multiboot.c), which reads as the radio
        # refusing to boot. Erasing the marker lets it re-decide from the slots.
        erase_state(buf)
    save_image(path, bytes(buf))
    return slots_json(path)["slots"][slot]


def erase_slot_file(path: str, slot: int) -> dict:
    buf = load_image(path)
    erase_slot(buf, slot)
    erase_state(buf)
    save_image(path, bytes(buf))
    return slots_json(path)["slots"][slot]
