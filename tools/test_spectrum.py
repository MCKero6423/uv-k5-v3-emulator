#!/usr/bin/env python3
"""RSSI must depend on where the radio is tuned, not be a constant.

Why this matters more than the number on the meter. RSSI used to be a fixed value
comfortably above squelch, which gave the S-meter something to draw but meant the band
was uniformly and permanently occupied. Scanning, squelch, and every "is this channel
busy" decision therefore faced a situation that never varied, so none of that logic was
actually being exercised -- the tests passed without testing anything.

Scope, stated plainly: the *shape* is real physics -- power falls off away from a
carrier, with a noise floor underneath -- and the station list is invented. This
reproduces "the firmware copes with a band that has signals in some places and not
others". It does not reproduce any real radio environment, and a dBm figure from here
is not a claim about the world.

Checked here:
  1. tuning to a station gives a strong reading
  2. tuning well away from every station drops to the noise floor
  3. the difference is large enough for squelch to distinguish them
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
BK_PATH = "/machine/bk4819"

# A station in the model's table, and a frequency far from all of them.
ON_STATION_HZ10 = 40000000     # 400.000 MHz
OFF_STATION_HZ10 = 41000000    # 410.000 MHz, several MHz clear of anything


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

    def reg(self, num):
        return self.cmd("qom-get", path=BK_PATH,
                        property=f"reg{num:02x}").get("return")

    def key(self, name, hold=0.15):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=name)
        time.sleep(hold)
        self.cmd("qom-set", path="/machine/keypad", property="press", value="")

    def type_frequency(self, mhz_digits):
        """Enter a frequency on the keypad, as a user would.

        Deliberately not poking REG_38/REG_39 directly: that would test the model
        against itself. Going through the firmware means the tuning path is exercised
        too.
        """
        for ch in mhz_digits:
            self.key(ch, hold=0.12)
            time.sleep(0.25)


def rssi_after_tuning(qmp, digits, settle=4):
    qmp.type_frequency(digits)
    time.sleep(settle)
    # Engage monitor so the receiver is actually running and polling.
    qmp.key("SIDE1")
    time.sleep(3)
    tuned = (qmp.reg(0x39) << 16) | qmp.reg(0x38)
    return qmp.reg(0x67), tuned


def main():
    for tool in (QEMU, ELF, PRISTINE):
        if not tool.exists():
            print(f"SKIP  missing {tool}")
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        img = pathlib.Path(tmp) / "flash.img"
        img.write_bytes(gzip.decompress(PRISTINE.read_bytes()))
        sock = pathlib.Path(tmp) / "qmp.sock"

        proc = subprocess.Popen(
            [str(QEMU), "-M", f"uv-k5-v3,flash-image={img}",
             "-nographic", "-monitor", "none",
             "-qmp", f"unix:{sock},server=on,wait=off",
             "-kernel", str(ELF)],
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

            # The radio boots tuned to 400.000, which is a station in the table.
            on_rssi = qmp.reg(0x67)
            tuned = (qmp.reg(0x39) << 16) | qmp.reg(0x38)
            print(f"tuned {tuned / 100000:.5f} MHz (a station): RSSI 0x{on_rssi:04X}")

            if tuned != ON_STATION_HZ10:
                print(f"note  expected {ON_STATION_HZ10 / 100000:.5f} MHz at boot; "
                      "the comparison below is still valid")

            # Tune away by typing a new frequency: 410.000 MHz.
            off_rssi, off_tuned = rssi_after_tuning(qmp, "410000")
            print(f"tuned {off_tuned / 100000:.5f} MHz (empty):   "
                  f"RSSI 0x{off_rssi:04X}")

            if off_tuned == tuned:
                print("FAIL  the frequency did not change; cannot compare")
                return 1

            if on_rssi > off_rssi:
                print(f"PASS  RSSI depends on tuning "
                      f"(0x{on_rssi:04X} on station, 0x{off_rssi:04X} off)")
            else:
                print(f"FAIL  RSSI did not drop away from the station "
                      f"(0x{on_rssi:04X} -> 0x{off_rssi:04X})")
                failures += 1

            # REG_67 is 0.25 dB/step, so 0x80 is 32 dB -- far more than any squelch
            # hysteresis, i.e. the two cases are unambiguously distinguishable.
            gap = on_rssi - off_rssi
            if gap >= 0x80:
                print(f"PASS  the gap is {gap * 0.25:.0f} dB, enough for squelch "
                      "to tell them apart")
            else:
                print(f"FAIL  the gap is only {gap * 0.25:.0f} dB; squelch could not "
                      "reliably distinguish a busy channel from an empty one")
                failures += 1

            if failures:
                return 1
            print("\nthe band has signals in some places and not others")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
