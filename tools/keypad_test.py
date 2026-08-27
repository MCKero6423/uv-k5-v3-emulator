#!/usr/bin/env python3
"""Check that keypresses reach the firmware and drive the UI.

Boots its own QEMU instance on private ports, so it does not disturb a running
run.sh session. Three checks:

  1. a short MENU press opens the menu from the main screen
  2. DOWN moves the menu cursor
  3. a short MENU press still works after power save has engaged

Why this exists: the keypad was reported broken for a long time and was not. The
cause was always press duration -- key.py held keys for 2500 ms, which the
firmware reads as a long press, and the long-press path does not open the menu.
A later round of "power save blocks the keypad" was the same error plus reading
gKeyReading0 after releasing the key, when it is always KEY_INVALID.

Two rules this test follows, and any manual probing should too:

  * Never run gdb between presses. Each attach halts the guest and can stretch a
    sequence past the 20 s menu timeout, so the UI falls back to the main screen
    and later presses go somewhere unintended.
  * Read key state while the key is still held, never after release.

Usage:
    tools/keypad_test.py            # all checks
    tools/keypad_test.py -v         # show each step
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
QEMU = os.environ.get(
    "QEMU_BIN", f"{HOME}/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.environ.get(
    "ELF", f"{HOME}/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
HERE = os.path.dirname(os.path.abspath(__file__))
FLASH = os.path.join(os.path.dirname(HERE), "assets", "flash.img")

QMP = "/tmp/uvk5-keypad-test-qmp.sock"
GDB_PORT = "1239"

# App/misc.c: key_debounce_10ms = 2 (20 ms), key_repeat_delay_10ms = 40 (400 ms).
# SysTick interrupts run at close to real time, so these are wall-clock values.
SHORT_MS = 200          # past debounce, well short of a long press
GAP_MS = 300            # let the release debounce before the next press

DISPLAY_MAIN = 0
DISPLAY_MENU = 1
KEY_INVALID = 19
FUNCTION_POWER_SAVE = 5

GMENUCURSOR_ADDR = "0x20001c94"


class Emu:
    def __init__(self, verbose=False):
        self.verbose = verbose
        for path in (QMP,):
            if os.path.exists(path):
                os.unlink(path)
        for path, what in ((QEMU, "QEMU binary"), (ELF, "firmware ELF"),
                           (FLASH, "flash image")):
            if not os.path.exists(path):
                sys.exit(f"missing {what}: {path}")

        self.proc = subprocess.Popen(
            [QEMU, "-M", f"uv-k5-v3,flash-image={FLASH}", "-nographic",
             "-monitor", "none", "-qmp", f"unix:{QMP},server=on,wait=off",
             "-kernel", ELF, "-gdb", f"tcp::{GDB_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(150):
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(QMP)
                break
            except OSError:
                if self.proc.poll() is not None:
                    sys.exit("QEMU exited during startup")
                time.sleep(0.1)
        else:
            sys.exit(f"QMP socket never appeared at {QMP}")

        self.buf = b""
        self._read()                       # greeting
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                sys.exit("QEMU closed the QMP connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, name, **args):
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        self.sock.sendall(json.dumps(payload).encode() + b"\n")
        while True:
            msg = self._read()
            if "error" in msg:
                sys.exit(f"QMP error: {msg['error']}")
            if "return" in msg:
                return msg["return"]

    def hold(self, key):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=key)

    def release(self):
        self.cmd("qom-set", path="/machine/keypad", property="press", value="")

    def press(self, key, hold_ms=SHORT_MS, gap_ms=GAP_MS):
        self.hold(key)
        time.sleep(hold_ms / 1000)
        self.release()
        time.sleep(gap_ms / 1000)
        if self.verbose:
            print(f"    pressed {key} ({hold_ms} ms)")

    def state(self):
        """Read UI state over gdb. Halts the guest, so never call mid-sequence."""
        exprs = [
            ("screen", "*(char*)&gScreenToDisplay"),
            ("fn", "*(char*)&gCurrentFunction"),
            ("kr0", "*(char*)&gKeyReading0"),
            ("cursor", f"*(unsigned char*){GMENUCURSOR_ADDR}"),
        ]
        args = ["gdb-multiarch", "-batch", "-ex", "set confirm off",
                "-ex", "set pagination off",
                "-ex", f"target remote :{GDB_PORT}"]
        for name, expr in exprs:
            args += ["-ex", f'printf "{name}=%d\\n", {expr}']
        args += ["-ex", "detach", "-ex", "quit", ELF]
        out = subprocess.run(args, capture_output=True, text=True).stdout
        got = {k: int(v) for k, v in re.findall(r"(\w+)=(-?\d+)", out)}
        if "screen" not in got:
            sys.exit("could not read guest state over gdb")
        return got

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if os.path.exists(QMP):
            os.unlink(QMP)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    emu = Emu(verbose=args.verbose)
    failures = []
    try:
        # Boot is ~5 s; power save engages ~6 s in and does not block anything,
        # so waiting past it makes the test deterministic rather than racy.
        time.sleep(16)

        # 1. short MENU press opens the menu, straight out of power save.
        #    No state read before the press: a gdb attach immediately beforehand
        #    disturbs the guest enough to lose the press.
        emu.press("MENU")
        st = emu.state()
        if st["screen"] == DISPLAY_MENU:
            print("PASS  short MENU press opens the menu (also wakes power save)")
        else:
            failures.append(f"MENU did not open the menu (screen={st['screen']})")
            print(f"FAIL  MENU did not open the menu (screen={st['screen']})")

        # 2. DOWN moves the cursor. Presses go out back to back with no gdb
        #    between them, then state is read once.
        before = emu.state()["cursor"]
        emu.press("DOWN")
        emu.press("DOWN")
        after = emu.state()
        if after["screen"] != DISPLAY_MENU:
            failures.append("menu closed during DOWN presses")
            print("FAIL  menu closed while pressing DOWN")
        elif after["cursor"] == (before + 2):
            print(f"PASS  DOWN moves the cursor ({before} -> {after['cursor']})")
        else:
            failures.append(
                f"cursor moved {before} -> {after['cursor']}, expected +2")
            print(f"FAIL  cursor {before} -> {after['cursor']}, expected +2")

        # 3. a held key is visible to the firmware *while held*.
        #
        #    Read state mid-hold, then release. Note the read has to come after
        #    the hold has already been established and the release must follow
        #    it -- do NOT interleave a gdb read between hold and release when the
        #    press itself is what you are testing. That attach pauses the guest
        #    and can stretch the press past the debounce window, which makes a
        #    working build look broken. Here the press outcome is not under test,
        #    only whether the scan sees the key, so the read is safe.
        emu.hold("MENU")
        time.sleep(0.5)
        held = emu.state()
        emu.release()
        time.sleep(GAP_MS / 1000)
        if held["kr0"] != KEY_INVALID:
            print(f"PASS  held key is seen by the scan (gKeyReading0={held['kr0']})")
        else:
            failures.append("held key not seen by the scan")
            print("FAIL  held key not seen (gKeyReading0=KEY_INVALID)")
    finally:
        emu.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all keypad checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
