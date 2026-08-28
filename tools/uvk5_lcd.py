#!/usr/bin/env python3
"""LCD framebuffer decoding for the UV-K5 emulator.

The firmware keeps the display in gStatusLine (page 0) and gFrameBuffer
(pages 1-7), one byte per column, 8 vertical pixels per byte, LSB at the top --
the layout the ST7565 expects. Extracted from tools/screenshot.py so the web UI
and the CLI screenshotter cannot drift apart.
"""
import os
import struct
import zlib

LCD_WIDTH = 128
STATUS_ROWS = 1
FRAME_ROWS = 7
TOTAL_ROWS = STATUS_ROWS + FRAME_ROWS       # 8 pages of 8 pixels = 64 lines
LCD_HEIGHT = TOTAL_ROWS * 8
FRAME_BYTES = FRAME_ROWS * LCD_WIDTH
STATUS_BYTES = LCD_WIDTH


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


def encode_png(pixels, scale: int = 4) -> bytes:
    """1-bit greyscale PNG, no third-party dependency.

    Compression level 6 rather than 9: at streaming rates the CPU saving matters
    more than the last few bytes on loopback.
    """
    width, height = LCD_WIDTH * scale, LCD_HEIGHT * scale
    raw = bytearray()
    for row in pixels:
        line = bytearray()
        for value in row:
            # Radio LCD is dark-on-light: 0 -> white, 1 -> black.
            line.extend([0x00 if value else 0xFF] * scale)
        for _ in range(scale):
            raw.append(0)                           # filter type 0
            raw.extend(line)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class FrameGrabber:
    """Reads the LCD out of guest memory over QMP.

    memsave, not pmemsave. The framebuffer symbols are CPU virtual addresses;
    pmemsave interprets its argument as a *physical* address and silently returns
    a block of zeros for these, which renders as a blank screen with no error
    anywhere. memsave takes the virtual address and returns the real contents --
    verified against the gdb path, both reporting 1693 lit bits on the same frame.

    QMP, not gdb: measured ~1.35 ms per frame with the guest still reporting
    status "running". The gdb path used by screenshot.py halts the guest on every
    attach, which is unusable for a live stream and also perturbs key debounce
    timing (see AGENTS.md). Do not reintroduce gdb here.
    """

    def __init__(self, client, frame_addr: int, status_addr: int,
                 spool_dir: str = "/dev/shm"):
        self._client = client
        self._frame_addr = frame_addr
        self._status_addr = status_addr
        # pmemsave writes to a path, so a tmpfs avoids disk I/O every frame.
        self._frame_path = os.path.join(spool_dir, "uvk5-frame.bin")
        self._status_path = os.path.join(spool_dir, "uvk5-status.bin")

    def raw(self) -> tuple[bytes, bytes]:
        """Return (status, frame) exactly as the firmware holds them."""
        self._client.command("memsave", val=self._frame_addr,
                             size=FRAME_BYTES, filename=self._frame_path)
        self._client.command("memsave", val=self._status_addr,
                             size=STATUS_BYTES, filename=self._status_path)
        with open(self._frame_path, "rb") as fh:
            frame = fh.read(FRAME_BYTES)
        with open(self._status_path, "rb") as fh:
            status = fh.read(STATUS_BYTES)
        return status, frame

    def png(self, scale: int = 4) -> bytes:
        status, frame = self.raw()
        return encode_png(unpack(status, frame), scale)
