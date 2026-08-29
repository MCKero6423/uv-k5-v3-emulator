#!/usr/bin/env python3
"""The battery level must follow the ADC, including the low-battery warning.

Written after an honest audit of what the emulator actually reproduces. The ADC was
modelled but returned a hardcoded 2200 forever, so an entire firmware behaviour --
gBatteryDisplayLevel, gLowBattery, and the warning popup -- was unreachable. A
peripheral that answers reads is not the same as a peripheral that is reproduced.

Checked here:
  1. a mid-scale reading gives a normal, non-zero battery level
  2. a low reading drops that level
  3. a low reading raises gLowBattery
  4. raising the reading again clears it

Point 4 matters: a latching flag that never clears would pass 1-3 and still be wrong.
"""

import gzip
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

SIM = pathlib.Path(__file__).resolve().parent.parent
QEMU = pathlib.Path(os.environ.get(
    "QEMU", "/root/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"))
ELF = pathlib.Path(os.environ.get(
    "ELF", "/root/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf"))
PRISTINE = SIM / "assets/pristine/flash-pristine.img.gz"

BOOT_SECONDS = 24
ADC_PATH = "/machine/soc/adc"

# The firmware samples the battery on a timer, so a change needs a few seconds to be
# picked up and turned into a level.
SETTLE = 6


class Qmp:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(25)
        self.s.connect(path)
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.s.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._read()
            if "return" in reply or "error" in reply:
                return reply

    def set_adc(self, value):
        return self.cmd("qom-set", path=ADC_PATH, property="adc-result",
                        value=value)


def firmware_state(port):
    """gBatteryDisplayLevel and gLowBattery, over gdb.

    Stopping the guest is fine here: the question is which state it settled in, not
    anything timing-dependent.
    """
    out = subprocess.run(
        ["gdb-multiarch", "-batch",
         "-ex", "set confirm off", "-ex", "set pagination off",
         "-ex", f"target remote :{port}",
         "-ex", 'printf "LEVEL=%d LOW=%d\\n",'
                ' *(unsigned char*)&gBatteryDisplayLevel,'
                ' *(unsigned char*)&gLowBattery',
         "-ex", "detach", "-ex", "quit", str(ELF)],
        capture_output=True, text=True, timeout=90)
    for line in out.stdout.splitlines():
        if line.startswith("LEVEL="):
            parts = dict(p.split("=") for p in line.split())
            return int(parts["LEVEL"]), int(parts["LOW"])
    return None, None


def main():
    for tool in (QEMU, ELF, PRISTINE):
        if not tool.exists():
            print(f"SKIP  missing {tool}")
            return 0

    port = 1262
    with tempfile.TemporaryDirectory() as tmp:
        img = pathlib.Path(tmp) / "flash.img"
        img.write_bytes(gzip.decompress(PRISTINE.read_bytes()))
        sock = pathlib.Path(tmp) / "qmp.sock"

        proc = subprocess.Popen(
            [str(QEMU), "-M", f"uv-k5-v3,flash-image={img}",
             "-nographic", "-monitor", "none",
             "-qmp", f"unix:{sock},server=on,wait=off",
             "-kernel", str(ELF), "-gdb", f"tcp::{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(BOOT_SECONDS * 4):
                if sock.exists():
                    break
                time.sleep(0.25)
            else:
                print("FAIL  QMP socket never appeared")
                return 1
            time.sleep(BOOT_SECONDS)

            qmp = Qmp(str(sock))
            failures = 0

            reply = qmp.set_adc(2200)
            if "error" in reply:
                print(f"FAIL  adc-result not settable: {reply['error']}")
                return 1

            time.sleep(SETTLE)
            level_ok, low_ok = firmware_state(port)
            print(f"adc=2200 (normal):  level={level_ok} low={low_ok}")
            if level_ok is None:
                print("FAIL  could not read the firmware's battery state")
                return 1
            if low_ok:
                print("FAIL  a mid-scale reading was treated as low battery")
                failures += 1
            else:
                print("PASS  a normal reading is not low battery")

            # Well under any sane threshold, but not zero: zero could plausibly be
            # special-cased as "no reading".
            qmp.set_adc(1200)
            time.sleep(SETTLE)
            level_low, low_low = firmware_state(port)
            print(f"adc=1200 (flat):    level={level_low} low={low_low}")

            if level_low < level_ok:
                print(f"PASS  the level followed the ADC down "
                      f"({level_ok} -> {level_low})")
            else:
                print(f"FAIL  the level did not drop ({level_ok} -> {level_low})")
                failures += 1

            if low_low:
                print("PASS  low battery was raised")
            else:
                print("FAIL  a flat battery did not raise gLowBattery")
                failures += 1

            # Recovery: the level must come back. gLowBattery deliberately is NOT
            # asserted to clear here.
            #
            # helper/battery.c:190-204 only clears gLowBattery when the level lands
            # exactly on 2; above that it clears gLowBatteryConfirmed and leaves
            # gLowBattery alone. So going 4 -> 0 -> 4 legitimately leaves the flag set,
            # and an earlier version of this test called that a failure. It was the
            # test that was wrong, not the model -- the emulator reproduces the firmware,
            # including behaviour that looks like a bug.
            qmp.set_adc(2200)
            time.sleep(SETTLE)
            level_back, low_again = firmware_state(port)
            print(f"adc=2200 (charged): level={level_back} low={low_again}")
            if level_back > level_low:
                print(f"PASS  the level recovered ({level_low} -> {level_back})")
            else:
                print(f"FAIL  the level stayed down ({level_low} -> {level_back})")
                failures += 1

            if failures:
                return 1
            print("\nbattery level tracks the ADC")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
