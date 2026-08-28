#!/usr/bin/env python3
"""Flash writes must survive a power cycle.

The SPI NOR model keeps the image in a g_malloc buffer and only ever reads the
backing file, so everything the firmware saves -- settings, edited frequencies,
channel data -- disappears when the QEMU process exits. On real hardware that is a
physical part which holds its contents with the power off.

The check is deliberately blunt: boot, let the firmware run long enough to write
its settings, power off, and compare the image on disk. The firmware writes to
EEPROM during boot on its own (SETTINGS_InitEEPROM and the power-on save path), so
no UI navigation and no guest-side poking is needed.

Driving the guest from gdb was tried and abandoned: `call` into firmware functions
hangs, because the main loop is running and the called function waits on hardware
the debugger has effectively frozen. Observing the file is both simpler and closer
to the behaviour the user actually sees.

Boots its own emulator on a private socket and works on a copy of the image, so it
cannot disturb a running session or damage assets/flash.img.

Run: python3 tools/test_flash_persist.py
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
SOURCE_IMAGE = os.path.join(SIM, "assets", "flash.img")

QMP = "/tmp/uvk5-persist-test.sock"

# Flash offsets the firmware demonstrably writes during a boot, measured rather than
# guessed. The mapping is in App/driver/eeprom_compat.c: these are *flash* addresses,
# not the EEPROM addresses the settings code uses, and an earlier version of this test
# watched EEPROM offsets by mistake and reported "same" for everything.
#
# These were 0x008100 and 0x00A100 while page-program wrapping was missing from the
# model: writes ran past the page boundary and landed a page high. With wrapping in
# place the firmware's writes align to the sector, as they do on real hardware.
WATCH = [
    ("mr/vfo attrs", 0x008000, 0x20),   # 1024 MR + 7 VFO attributes, 2 bytes each
    ("settings", 0x00A000, 0x20),       # the settings block
]

# Must stay untouched: this is where per-band VFO frequencies live. The missing page
# wrap let a 512-byte burst at 0x008F00 spill into it, zeroing stored frequencies so
# every typed frequency reverted to BX4819_band1_lower (18 MHz).
MUST_NOT_CHANGE = [("vfo frequencies", 0x009000, 0xD6)]


class Qmp:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(25)
        self.sock.connect(path)
        self.buf = b""
        self._readline()
        self.command("qmp_capabilities")

    def _readline(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._readline()
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise RuntimeError(reply["error"])

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def boot(image):
    if os.path.exists(QMP):
        os.unlink(QMP)
    proc = subprocess.Popen(
        [QEMU, "-M", f"uv-k5-v3,flash-image={image}", "-nographic",
         "-monitor", "none", "-qmp", f"unix:{QMP},server=on,wait=off",
         "-kernel", ELF],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 25
    while time.time() < deadline:
        if os.path.exists(QMP):
            return proc, Qmp(QMP)
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("QMP socket never appeared")


def shutdown(proc, qmp):
    """Quit through QMP, which is exactly what the web UI's power off does."""
    try:
        qmp.command("quit")
    except Exception:
        pass                                  # the socket usually drops first
    qmp.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if os.path.exists(QMP):
        os.unlink(QMP)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def snapshot(path):
    data = open(path, "rb").read()
    regions = WATCH + MUST_NOT_CHANGE
    return {name: data[off:off + length] for name, off, length in regions}


def main():
    for path, what in ((QEMU, "QEMU"), (ELF, "firmware"),
                       (SOURCE_IMAGE, "flash image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")

    workdir = tempfile.mkdtemp(prefix="uvk5-persist-")
    image = os.path.join(workdir, "flash.img")

    # Start from the pristine image, not assets/flash.img. The live image may have
    # been written by an earlier session, and then a boot has nothing left to save
    # -- which shows up as "the image is byte-identical", a confusing failure that
    # looks like persistence is broken when it is the fixture that is dirty.
    pristine_gz = os.path.join(os.path.dirname(SOURCE_IMAGE),
                               "pristine", "flash-pristine.img.gz")
    if os.path.exists(pristine_gz):
        import gzip
        with gzip.open(pristine_gz, "rb") as src, open(image, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copy(SOURCE_IMAGE, image)
    failures = []

    try:
        before_sha = sha(image)
        before = snapshot(image)
        print(f"image sha before boot: {before_sha[:16]}")
        for name, _, _ in WATCH:
            print(f"  {name:9s} {before[name][:12].hex()}")

        print("\nbooting, letting the firmware settle, then powering off")
        proc, qmp = boot(image)
        try:
            time.sleep(20)                    # main loop plus a settings write
            status = qmp.command("query-status")
            print(f"  guest: {status.get('status')}")
        finally:
            shutdown(proc, qmp)

        after_sha = sha(image)
        after = snapshot(image)
        print(f"\nimage sha after power off: {after_sha[:16]}")
        for name, _, _ in WATCH:
            mark = "same" if after[name] == before[name] else "CHANGED"
            print(f"  {name:9s} {after[name][:12].hex()}  {mark}")

        if after_sha == before_sha:
            failures.append(
                "the image on disk is byte-identical after a boot and clean "
                "power off, so nothing the firmware wrote to flash was saved")
        else:
            print("\nPASS  the firmware's writes reached the file")

        # Being specific matters: a changed hash alone could be almost anything.
        # These are the regions holding channel and settings data.
        unchanged = [n for n, _, _ in WATCH if after[n] == before[n]]
        if unchanged:
            failures.append(
                f"the image changed but {', '.join(unchanged)} did not, so the "
                "regions holding settings and channel data were not written")
        else:
            print("PASS  the settings and channel regions were written")

        # The frequency area must be left alone. A page-program burst that fails to
        # wrap spills into it and zeroes stored frequencies, which is what made a
        # typed frequency always revert to 18 MHz.
        spilled = [n for n, _, _ in MUST_NOT_CHANGE if after[n] != before[n]]
        if spilled:
            failures.append(
                f"{', '.join(spilled)} was overwritten -- a write spilled past a "
                "page boundary into the VFO frequency area")
        else:
            print("PASS  the VFO frequency area was not overwritten")

        # A second boot must see what the first one left behind.
        print("\nbooting again from the same file")
        proc, qmp = boot(image)
        try:
            time.sleep(20)
        finally:
            shutdown(proc, qmp)

        third = snapshot(image)
        if third != after:
            changed = [n for n, _, _ in WATCH if third[n] != after[n]]
            print(f"  note: {', '.join(changed)} changed again on the second boot")
        print("  (a stable image across reboots means state is being carried over)")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("flash writes persist across a power cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
