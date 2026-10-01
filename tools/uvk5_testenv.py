#!/usr/bin/env python3
"""Where the emulator tests find a QEMU and a firmware, and what to say when they cannot.

The emulator tests used to name one developer's build directory:

    QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
    ELF  = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")

which is fine on the machine they were written on and useless anywhere else -- and a
missing file used to be a hard failure, so a checkout without that build could never
see a green run and nobody could tell "broken" from "not set up here".

Resolution order, for both:

    qemu()      env QEMU, env UVK5_QEMU, qemu-system-arm on PATH
    firmware()  env ELF, env UVK5_FIRMWARE, assets/firmware/*, work/*

firmware() prefers assets/firmware, which tools/fetch_firmware.py fills from the upstream
project's release archive; that is what makes these tests reproducible rather than
anchored to one private build.
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIRMWARE_DIR = os.path.join(ROOT, "assets", "firmware")


def qemu():
    """Path to a qemu-system-arm, or None."""
    for var in ("QEMU", "UVK5_QEMU"):
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return pathlib.Path(value)
    found = shutil.which("qemu-system-arm")
    return pathlib.Path(found) if found else None


def gdb():
    """Path to a gdb that speaks ARM, or None.

    A few tests read firmware globals over a gdb attach. That is the one part of the
    suite a Windows box cannot do at all, and it used to be a hard failure rather than
    a skip -- so those tests reported a regression where there was only a missing tool.
    """
    for var in ("GDB", "UVK5_GDB"):
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return pathlib.Path(value)
    # Not plain gdb: on most hosts that is the *host* architecture's gdb, which attaches
    # to the guest, returns nothing useful, and produces a confusing parse failure
    # instead of "no debugger". The two names below can actually read an ARM target.
    for name in ("gdb-multiarch", "arm-none-eabi-gdb"):
        found = shutil.which(name)
        if found:
            return pathlib.Path(found)
    return None


def firmware():
    """Path to a firmware image to run, or None."""
    for var in ("ELF", "UVK5_FIRMWARE", "UVK5_MULTIBOOT_IMAGE"):
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return pathlib.Path(value)
    patterns = [os.path.join(FIRMWARE_DIR, "*.bin"),
                os.path.join(FIRMWARE_DIR, "*.elf"),
                os.path.join(ROOT, "work", "*.bin"),
                os.path.join(ROOT, "work", "*.elf")]
    for pattern in patterns:
        for hit in sorted(glob.glob(pattern)):
            return pathlib.Path(hit)
    return None


def missing(items):
    """First thing in @items that is absent, described with how to fix it.

    @items is a list of (path, what, how). Returns None when everything is present.
    """
    for path, what, how in items:
        if not path or not os.path.exists(path):
            return "%s is missing (%s); %s" % (what, path or "not found", how)
    return None


def skip(message):
    """Print a skip line and return the exit code a test should use.

    A missing prerequisite is not a failing test. Saying so loudly beats exiting
    non-zero, which is indistinguishable from a regression and is why a Windows or
    fresh checkout could never show a green run.
    """
    print("SKIP: %s" % message)
    return 0
