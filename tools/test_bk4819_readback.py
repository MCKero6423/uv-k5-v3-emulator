#!/usr/bin/env python3
"""A register read must deliver the value the register holds.

DRAFT -- not yet a guard, and deliberately not registered in tools/run_tests.sh.

It passes on the working model (238 reads, every one agreeing), but it does *not* fail when
the historical fix is removed: with skip_falling left unset the reassembled word is unchanged,
so this observation point is not the one the guest samples at. Until that is understood,
tools/test_bk4819_readback.sh remains the guard for this bug and this file is the working
draft of its portable replacement.

This is the guard for a bug that was invisible for a long time: reads arrived shifted
one place left, so seeding REG_0C with 0x1248 delivered 0x2490 to the firmware. Writes
were always fine and the registers the firmware polls hardest were legitimately zero,
and reading zero and getting zero looks like success.

It used to be tools/test_bk4819_readback.sh, which seeded a register by *patching
qemu/py32f071.c*, rebuilt QEMU, and read the value out of the running guest with
gdb-multiarch. Three dependencies -- a source tree, a compiler and an ARM gdb -- for a
guard whose whole job is to protect whoever is editing the model right now. On a machine
without an ARM gdb it skipped, which is to say the guard did not exist there.

It now watches the bits the model presents to the guest (UVK5_BK4819_PROBE) and asserts
that the sixteen bits a read delivers are the register's own value. No rebuild, no gdb,
no source patch: plain Python, so it runs wherever the emulator does.
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import uvk5_testenv                                    # noqa: E402

# The firmware reads REG_0C about 1000 times in the first seconds, REG_31 and REG_48
# hundreds of times, so a sample is guaranteed without seeding anything.
MIN_SAMPLES = 200


def main():
    qemu = uvk5_testenv.qemu()
    firmware = uvk5_testenv.firmware()
    missing = uvk5_testenv.missing([
        (qemu, "QEMU", "set QEMU=/path/to/qemu-system-arm, or put it on PATH"),
        (firmware, "firmware", "run tools/fetch_firmware.py, or set ELF=..."),
    ])
    if missing:
        return uvk5_testenv.skip(missing)

    workdir = tempfile.mkdtemp(prefix="uvk5-readback-")
    probe = os.path.join(workdir, "reads.log")
    env = dict(os.environ)
    env["UVK5_BK4819_PROBE"] = probe
    env.setdefault("UVK5_FLASH_IMAGE", os.path.join(HERE, "..", "assets", "flash.img"))
    env["PATH"] = "F:/msys64/mingw64/bin;" + env.get("PATH", "")

    # No QMP needed: the probe writes to a file and the firmware asks the chip on its own.
    proc = subprocess.Popen([str(qemu), "-M", "uv-k5-v3", "-nographic", "-monitor", "none",
                             "-serial", "null", "-kernel", str(firmware)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1.0)
            if os.path.exists(probe) and len(open(probe, encoding="utf-8").read().splitlines()) >= MIN_SAMPLES:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    reads = []
    if os.path.exists(probe):
        with open(probe, encoding="utf-8") as fh:
            reads = [line.split() for line in fh if line.startswith("READ ")]
    if len(reads) < MIN_SAMPLES:
        print("FAIL  only %d reads captured (wanted %d): did the firmware boot?"
              % (len(reads), MIN_SAMPLES))
        return 1

    bad = []
    for fields in reads:
        values = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
        sent, reg = values.get("sent"), values.get("reg")
        if sent is not None and reg is not None and sent != reg:
            bad.append((values.get("cmd"), sent, reg))

    print("  %d register reads, %d delivered something other than the register's value"
          % (len(reads), len(bad)))
    if not bad:
        print("")
        print("register reads are bit-aligned")
        return 0

    print("")
    for cmd, sent, reg in bad[:5]:
        direction = ""
        if sent == "%04x" % ((int(reg, 16) << 1) & 0xffff):
            direction = " (shifted one place left; the command byte's trailing falling edge is eating bit 15)"
        elif sent == "%04x" % (int(reg, 16) >> 1):
            direction = " (shifted one place right; a data bit is being presented twice)"
        print("FAIL  REG_%s: register holds %s, guest received %s%s" % (cmd, reg, sent, direction))
    return 1


if __name__ == "__main__":
    sys.exit(main())
