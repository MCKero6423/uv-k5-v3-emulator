#!/usr/bin/env python3
"""What the device says it is running, and whether that is what was uploaded.

The page knew only what it had *asked* to boot: the image it was given, or the one
uploaded through it. That is not the same question. With the multi-system release, an
external slot plus a valid state marker makes the factory bootloader reflash the
internal flash from that slot on every power-on -- so the uploaded image is overwritten
before it ever runs, the page keeps saying "f4hwn.fusion.bin", and the radio is running
something else entirely.

The firmware answers the question itself: it prints a banner on USART1 in its first
seconds ("UV-K5 Firmware, EGZUMER+F4HWN v6.0.0.CN"), which the machine tags SERIAL and
the page already collects. Reading that back is the difference between what we asked for
and what is running.

The second half matters too: a banner *should* often differ from a file name -- a file
called f4hwn.fusion.bin can legitimately report v6.0.0.CN -- so a difference is only
suspicious when the running version is not in the uploaded image at all. That check is
`image_mentions`, and it is what makes the hint honest rather than a false alarm.
"""
import re

BANNER = re.compile(r"UV-K5 Firmware,\s*(.+?)\s*$")


def latest(lines):
    """The most recent firmware banner in an iterable of log lines, or None."""
    found = None
    for line in lines:
        match = BANNER.search(str(line))
        if match:
            found = match.group(1).strip()
    return found


def image_mentions(path, banner) -> bool:
    """Does this firmware file contain the string the device reported?

    A version string lives in the image, so an image whose banner this is will contain
    it. If it does not, the device is running something else -- which is the case that
    needs explaining (a slot restored by the bootloader, most often).
    """
    if not path or not banner:
        return True          # nothing to check against; do not accuse anything
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return True          # unreadable is not evidence of a mismatch
    return banner.encode("ascii", "ignore") in blob
