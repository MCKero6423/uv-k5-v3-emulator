#!/usr/bin/env python3
"""millis() must advance, because 17 call sites build timeouts on it.

Found by auditing what the emulator genuinely reproduces rather than what merely runs.
TIM2 was covered by the catch-all stub, which returns the last value written -- so

    uint32_t millis(void) { return LL_TIM_GetCounter(TIM2); }

returned 0 forever and no elapsed-time check could ever fire. A silent wrong answer,
not a hang, which is the harder kind to spot.

Checked here:
  1. the counter is non-zero once the firmware has been running a while
  2. it increases between two samples
  3. it increases by roughly the elapsed wall time, not some arbitrary amount

Point 3 is what separates a working counter from one that merely changes: a counter
ticking at the wrong rate would satisfy 1 and 2 and still break every timeout.
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
TIM2_CNT = 0x40000024      # TIM2 base + CNT offset
GAP = 5.0


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


def read_counter(port):
    """TIM2's CNT, via gdb.

    Read through the CPU so the peripheral's read handler runs -- the count is derived
    on access, not stored, so dumping memory another way would miss it.
    """
    out = subprocess.run(
        ["gdb-multiarch", "-batch",
         "-ex", "set confirm off", "-ex", "set pagination off",
         "-ex", f"target remote :{port}",
         "-ex", f'printf "CNT=%u\\n", *(unsigned int*){TIM2_CNT}',
         "-ex", "detach", "-ex", "quit", str(ELF)],
        capture_output=True, text=True, timeout=90)
    for line in out.stdout.splitlines():
        if line.startswith("CNT="):
            return int(line[4:])
    return None


def main():
    for tool in (QEMU, ELF, PRISTINE):
        if not tool.exists():
            print(f"SKIP  missing {tool}")
            return 0

    port = 1263
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

            failures = 0

            first = read_counter(port)
            print(f"millis() sample 1: {first}")
            if first is None:
                print("FAIL  could not read TIM2")
                return 1
            if first == 0:
                print("FAIL  the counter is still 0; TIM2 is not running")
                failures += 1
            else:
                print("PASS  the counter is running")

            time.sleep(GAP)
            second = read_counter(port)
            print(f"millis() sample 2: {second}  (after {GAP:.0f}s)")

            delta = second - first
            if delta > 0:
                print(f"PASS  it advanced by {delta} ms")
            else:
                print(f"FAIL  it did not advance ({first} -> {second})")
                failures += 1

            # Generous bounds: the guest is paused twice by gdb, and the emulator does
            # not track wall time exactly. The point is to catch a counter running at
            # completely the wrong rate, not to measure precision.
            low, high = GAP * 1000 * 0.3, GAP * 1000 * 3.0
            if low <= delta <= high:
                print(f"PASS  the rate is plausible "
                      f"({delta} ms for {GAP:.0f}s of wall time)")
            else:
                print(f"FAIL  the rate is wrong: {delta} ms elapsed over "
                      f"{GAP:.0f}s, expected roughly {int(GAP * 1000)}")
                failures += 1

            if failures:
                return 1
            print("\nmillis() advances at a usable rate")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
