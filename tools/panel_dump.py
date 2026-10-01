#!/usr/bin/env python3
"""Render the display controller's own memory, for any firmware, without a browser.

The web page already does this; this is the same thing from a shell, which is what you
want when the question is "does *this* build render correctly" and the answer has to be
compared rather than glanced at. It prints ASCII at one character per LCD pixel, so two
builds can be diffed, and can also write a scaled PNG.

    tools/panel_dump.py --qmp 127.0.0.1:4444                   # ASCII, as the panel shows it
    tools/panel_dump.py --qmp 127.0.0.1:4444 --mapping mirror-cols
    tools/panel_dump.py --qmp 127.0.0.1:4444 --png work/screen.png

The --mapping option exists because which mapping is right is a property of the *driver*,
not of the controller. Two builds can program identical panel registers -- 0xA1 segment
reverse, 0xC0, 0xA6 -- and still arrive in different orders, because one compensates in
software and the other does not. Measured on the 5.9.0.CN build against its own
framebuffer: 8188 of 8192 pixels agree with the panel data untouched, 6954 with the
columns mirrored.
"""
import argparse
import sys

import uvk5_lcd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qmp", default="127.0.0.1:4444",
                    help="the emulator's QMP endpoint (host:port or unix:path)")
    ap.add_argument("--mapping", default="identity",
                    choices=["identity", "mirror-cols", "mirror-rows", "both"])
    ap.add_argument("--png", help="write a scaled PNG here instead of ASCII")
    ap.add_argument("--scale", type=int, default=4)
    args = ap.parse_args(argv)

    from uvk5_qmp import QmpClient
    # The frame addresses are unused on this path: the controller has its own memory.
    grab = uvk5_lcd.FrameGrabber(QmpClient(args.qmp, timeout=20), 0, 0)
    pixels = grab.panel_pixels()
    if args.mapping in ("mirror-cols", "both"):
        pixels = [list(reversed(row)) for row in pixels]
    if args.mapping in ("mirror-rows", "both"):
        pixels = list(reversed(pixels))

    if args.png:
        with open(args.png, "wb") as fh:
            fh.write(uvk5_lcd.encode_png(pixels, args.scale))
        print("wrote %s (%d x %d)" % (args.png, len(pixels[0]), len(pixels)))
        return 0

    print("panel %d x %d, %d pixels lit, mapping %s"
          % (len(pixels[0]), len(pixels), sum(sum(r) for r in pixels), args.mapping))
    for row in pixels:
        print("".join("#" if value else "." for value in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
