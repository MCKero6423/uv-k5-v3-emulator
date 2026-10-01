#!/usr/bin/env python3
"""LCD framebuffer decoding for the UV-K5 emulator.

The firmware keeps the display in gStatusLine (page 0) and gFrameBuffer
(pages 1-7), one byte per column, 8 vertical pixels per byte, LSB at the top --
the layout the ST7565 expects. Extracted from tools/screenshot.py so the web UI
and the CLI screenshotter cannot drift apart.
"""
import os
import struct
import sys
import tempfile
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


# The display controller's own settings. Inversion and display-on are panel state,
# not framebuffer content, so nothing in guest RAM reflects them -- which is exactly
# why a menu entry that changes them looks like it did nothing.
PANEL_PATH = "/machine/panel"


def default_spool_dir() -> str:
    """A tmpfs when the host has one, the system temp directory otherwise."""
    if os.path.isdir("/dev/shm"):
        return "/dev/shm"
    return tempfile.gettempdir()


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
                 spool_dir: str = None):
        self._client = client
        self._frame_addr = frame_addr
        self._status_addr = status_addr
        # memsave writes to a path, so a tmpfs avoids disk I/O every frame --
        # where there is one. /dev/shm does not exist on Windows, and naming it
        # there makes every frame grab fail, which shows up only as a blank
        # screen with "no frame available" from the web UI.
        self._panel_warned = False
        self._spool_dir = spool_dir or default_spool_dir()
        self._frame_path = os.path.join(self._spool_dir, "uvk5-frame.bin")
        self._status_path = os.path.join(self._spool_dir, "uvk5-status.bin")

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

    def panel_state(self):
        """(invert, contrast, display_on) as the display controller holds them.

        Read from the panel model rather than the framebuffer: 0xA6/0xA7, 0x81 and
        0xAE/0xAF live in the controller. If the model is not there (an older
        emulator build) the defaults describe an ordinary, unobstructed panel.
        """
        try:
            invert = bool(self._client.command("qom-get", path=PANEL_PATH,
                                               property="invert"))
            contrast = int(self._client.command("qom-get", path=PANEL_PATH,
                                                property="contrast"))
            display_on = bool(self._client.command("qom-get", path=PANEL_PATH,
                                                   property="display-on"))
        except Exception as exc:
            # Not fatal -- an emulator built without the panel model, or one that
            # just went away, still has a framebuffer worth showing. But say so
            # once: swallowing this silently is how a wrong render looks like a
            # firmware that ignores the setting. (It hid a stub bug in the test
            # for this very method.)
            if not self._panel_warned:
                self._panel_warned = True
                print(f"panel state unavailable, rendering as-is: {exc}",
                      file=sys.stderr)
            return (False, 0, True)
        return (invert, contrast, display_on)

    def panel_gram(self) -> bytes:
        """The display controller's own display RAM: the screen as it is shown.

        Preferred over raw() when the caller does not know where *this* firmware
        keeps its buffers. Builds that share an ancestor still differ in their
        display logic, and the multi-system release keeps its image somewhere else
        entirely -- but every one of them pushes pixels through the same controller.
        """
        hexed = self._client.command("qom-get", path=PANEL_PATH, property="gram")
        return bytes.fromhex(hexed)

    def panel_pixels(self):
        """The screen as pixels, from the controller's own memory.

        No hardware mirroring is applied. The driver programs 0xA1 (segment reverse)
        and 0xC0, but the columns arrive in the order the glass needs, so mirroring
        on top of the data flips the picture: against the guest's own framebuffer at
        the same instant, 8153 of 8192 pixels agree with no mirror and 6557 with
        one. The flags stay reported, not acted on.

        Raises if the panel model is absent, which is how the caller knows to fall
        back to guest RAM -- see uvk5_stream.FramePump.
        """
        gram = self.panel_gram()
        if len(gram) != TOTAL_ROWS * LCD_WIDTH:
            raise ValueError("panel GRAM is %d bytes, expected %d"
                             % (len(gram), TOTAL_ROWS * LCD_WIDTH))
        return self._apply_panel(unpack(gram[:STATUS_BYTES], gram[STATUS_BYTES:]))

    def panel_png(self, scale: int = 4) -> bytes:
        """The panel's own memory, encoded as a PNG."""
        return encode_png(self.panel_pixels(), scale)
    def _apply_panel(self, pixels):
        """Apply the panel settings that change the picture (inversion only)."""
        invert, _contrast, display_on = self.panel_state()
        if invert:
            # 0xA7: the panel inverts the whole image, which is a visible, fully
            # determined effect -- so the render follows it. Contrast is analogue
            # and cannot be rendered; display-on is deliberately *not* acted on
            # here: whether a software reset (0xE2) clears the display-on latch is
            # not certain, and blanking the screen on a guess would be worse than
            # reporting the flag and leaving the image alone.
            pixels = [[1 - value for value in row] for row in pixels]
        return pixels
    def png(self, scale: int = 4) -> bytes:
        status, frame = self.raw()
        return encode_png(self._apply_panel(unpack(status, frame)), scale)
