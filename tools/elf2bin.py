#!/usr/bin/env python3
"""Extract the loadable bytes of an overlay-app ELF as a flat binary.

objcopy -O binary is the normal tool; this reads the ELF itself. Allocated *sections*
are used rather than program headers, because lld maps the ELF header and program
header table as a LOAD of its own (52 + 4x32 = 180 bytes at the image base) with no
section content in it -- following the program headers puts a phantom 180-byte region
below the overlay VMA and makes the image start at the wrong address.
"""
import argparse
import struct
import sys

SHT_SYMTAB, SHT_NOBITS = 2, 8
SHF_ALLOC, SHF_WRITE, SHF_EXECINSTR = 0x2, 0x1, 0x4


def sections(blob: bytes):
    if len(blob) < 52 or blob[0] != 0x7F or blob[1:4] != b"ELF" or blob[4] != 1 or blob[5] != 1:
        raise SystemExit("not a 32-bit little-endian ELF")
    e_shoff, = struct.unpack_from("<I", blob, 0x20)
    e_shentsize, e_shnum, _ = struct.unpack_from("<HHH", blob, 0x2E)
    if not e_shoff or not e_shnum:
        return None
    out = []
    for i in range(e_shnum):
        f = struct.unpack_from("<IIIIIIIIII", blob, e_shoff + i * e_shentsize)
        addr, offset, size, flags, typ = f[3], f[4], f[5], f[2], f[1]
        if typ == SHT_NOBITS:                     # .bss has no file bytes
            out.append((addr, offset, 0, size, typ))
        elif flags & SHF_ALLOC and size:
            out.append((addr, offset, size, size, typ))
    return out or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("elf")
    ap.add_argument("bin")
    ap.add_argument("--min", type=lambda v: int(v, 0), default=None,
                    help="require the image to start here (the overlay VMA)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    blob = open(args.elf, "rb").read()
    secs = sections(blob)
    if not secs:
        raise SystemExit("no allocated sections; nothing to extract")
    base = min(s[0] for s in secs if s[2])        # the first section with file content
    if args.min is not None and base != args.min:
        raise SystemExit("image starts at 0x%08X but the overlay VMA is 0x%08X -- the blob "
                         "would be rejected with APP_ERR_VMA" % (base, args.min))
    end = max(addr + memsz for addr, _, _, memsz, _ in secs)
    out = bytearray(end - base)                   # .bss stays zero, as the loader wants
    for addr, offset, filesz, memsz, _ in secs:
        if filesz:
            out[addr - base:addr - base + filesz] = blob[offset:offset + filesz]
    open(args.bin, "wb").write(out)
    name = lambda p: p.replace("\\", "/").split("/")[-1]
    if not args.quiet:
        print("%s -> %s: %d bytes at 0x%08X (%d allocated section(s), %d of them with content)"
              % (name(args.elf), name(args.bin), len(out), base,
                 len(secs), sum(1 for s in secs if s[2])))
        if len(out) > 0x1000 and args.min == 0x20000280:
            print("WARNING: %d bytes exceeds the 4096-byte overlay budget" % len(out), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
