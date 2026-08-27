#!/usr/bin/env python3
"""Render the emulated radio's LCD by reading its framebuffer over GDB.

The firmware keeps the display in two globals -- gStatusLine (the top status
row) and gFrameBuffer[7] (the seven text rows) -- in the layout the ST7565
expects: one byte per column, each byte holding 8 vertical pixels, LSB at the
top. The K5Viewer serial protocol repacks that per bit-plane for compression;
reading the buffers directly gives the same pixels without implementing either
the SPI display controller or the wire protocol.

Usage:
    screenshot.py --elf firmware.elf --port 1234 [--out screen.png] [--scale 4]

Requires the emulator started with -gdb tcp::PORT.
"""

import argparse
import re
import subprocess
import sys

LCD_WIDTH = 128
STATUS_ROWS = 1
FRAME_ROWS = 7
TOTAL_ROWS = STATUS_ROWS + FRAME_ROWS  # 8 pages of 8 pixels = 64 lines
LCD_HEIGHT = TOTAL_ROWS * 8


def symbol_address(elf: str, name: str) -> int:
    """Look up a symbol in the ELF, so addresses are never hard-coded."""
    out = subprocess.run(
        ["arm-none-eabi-nm", elf],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        # Fall back to the toolchain inside the build container.
        out = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{elf}:/f.elf", "uvk1-uvk5v3",
             "arm-none-eabi-nm", "/f.elf"],
            capture_output=True, text=True, check=False,
        )
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == name:
            return int(parts[0], 16)
    raise SystemExit(f"symbol {name} not found in {elf}")


def read_memory(port: int, address: int, length: int) -> bytes:
    """Dump guest memory through gdb-multiarch in batch mode."""
    script = f"""
set confirm off
set pagination off
target remote :{port}
dump binary memory /tmp/_screen_dump.bin {address:#x} {address + length:#x}
detach
quit
"""
    # A real file rather than /dev/stdin: gdb rejects the latter as a script
    # source ("Invalid argument") because it seeks in it.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".gdb", delete=False) as fh:
        fh.write(script)
        script_path = fh.name
    proc = subprocess.run(
        ["gdb-multiarch", "-batch", "-x", script_path],
        capture_output=True, text=True, check=False,
    )
    try:
        with open("/tmp/_screen_dump.bin", "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise SystemExit(
            "gdb produced no dump. Is the emulator running with -gdb tcp::"
            f"{port}?\n{proc.stdout}\n{proc.stderr}"
        )
    if len(data) < length:
        raise SystemExit(f"short read: {len(data)} of {length} bytes")
    return data[:length]


def unpack(status: bytes, frame: bytes) -> list[list[int]]:
    """Column-major, LSB-at-top bytes -> a row-major pixel grid."""
    pixels = [[0] * LCD_WIDTH for _ in range(LCD_HEIGHT)]

    for page in range(TOTAL_ROWS):
        src = status if page == 0 else frame[(page - 1) * LCD_WIDTH:page * LCD_WIDTH]
        for col in range(LCD_WIDTH):
            byte = src[col]
            for bit in range(8):
                if byte & (1 << bit):
                    pixels[page * 8 + bit][col] = 1
    return pixels


def write_png(pixels, path: str, scale: int) -> None:
    """Minimal 1-bit PNG writer, so the tool has no third-party dependency."""
    import struct
    import zlib

    width, height = LCD_WIDTH * scale, LCD_HEIGHT * scale
    raw = bytearray()
    for row in pixels:
        line = bytearray()
        for value in row:
            # Radio LCD is dark-on-light: 0 -> white, 1 -> black.
            line.extend([0x00 if value else 0xFF] * scale)
        for _ in range(scale):
            raw.append(0)  # filter type 0
            raw.extend(line)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def write_text(pixels) -> str:
    """ASCII rendering, for when a picture is not needed."""
    out = []
    for y in range(0, LCD_HEIGHT, 2):
        line = []
        for x in range(LCD_WIDTH):
            top = pixels[y][x]
            bottom = pixels[y + 1][x] if y + 1 < LCD_HEIGHT else 0
            line.append(" ▀▄█"[(top << 0) | (bottom << 1)])
        out.append("".join(line))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elf", help="look symbol addresses up from this ELF")
    ap.add_argument("--frame-addr", type=lambda v: int(v, 0),
                    help="gFrameBuffer address, when no ARM nm is available")
    ap.add_argument("--status-addr", type=lambda v: int(v, 0),
                    help="gStatusLine address")
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--out", default="screen.png")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--text", action="store_true", help="also print ASCII art")
    args = ap.parse_args()

    if args.frame_addr is not None and args.status_addr is not None:
        frame_addr, status_addr = args.frame_addr, args.status_addr
    elif args.elf:
        frame_addr = symbol_address(args.elf, "gFrameBuffer")
        status_addr = symbol_address(args.elf, "gStatusLine")
    else:
        raise SystemExit("pass either --elf or both --frame-addr and --status-addr")

    frame = read_memory(args.port, frame_addr, FRAME_ROWS * LCD_WIDTH)
    status = read_memory(args.port, status_addr, LCD_WIDTH)

    pixels = unpack(status, frame)
    lit = sum(sum(row) for row in pixels)

    write_png(pixels, args.out, args.scale)
    print(f"gFrameBuffer @ {frame_addr:#010x}, gStatusLine @ {status_addr:#010x}")
    print(f"{lit} of {LCD_WIDTH * LCD_HEIGHT} pixels lit -> {args.out}")
    if lit == 0:
        print("screen is blank: the firmware has not drawn yet, or it faulted "
              "before reaching the UI")
    if args.text:
        print(write_text(pixels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
