#!/usr/bin/env python3
"""The audio amplifier must turn on when the firmware decides to make sound.

Scope, stated plainly, because "add a speaker and a microphone" is the obvious request
and the honest answer is that there is nothing to add. On the real radio neither passes
through the MCU:

  * receive audio is demodulated inside the BK4819 and leaves it as analogue on its AF
    output pin, going straight to the amplifier
  * transmit audio goes from the microphone into the chip's own ADC

The firmware's whole involvement is PA8 (amplifier enable), REG_47 (which AF source the
chip routes) and REG_64 (a level it displays). No audio samples exist anywhere in the
MCU's address space, so a device model has nothing to capture or play, and browser audio
permissions have nothing to carry. Synthesising sound would be inventing data the
firmware never produced.

What is real is the firmware's *intent*, and PA8 states it exactly. Checked here:

  1. the amplifier is off while idle in power save
  2. engaging monitor (SIDE1) turns it on -- squelch forced open means listening
  3. it is still on afterwards, i.e. this is a state and not a blip
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

    def speaker_on(self):
        return self.cmd("qom-get", path="/machine/audio",
                        property="speaker-on").get("return")

    def key(self, name, hold=0.15):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=name)
        time.sleep(hold)
        self.cmd("qom-set", path="/machine/keypad", property="press", value="")


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

            idle = qmp.speaker_on()
            print(f"idle:            speaker-on={idle}")
            if idle:
                print("FAIL  the amplifier is on while the radio sits in power save")
                failures += 1
            else:
                print("PASS  the amplifier is off when there is nothing to hear")

            qmp.key("SIDE1")
            time.sleep(3)
            listening = qmp.speaker_on()
            print(f"monitoring:      speaker-on={listening}")
            if listening:
                print("PASS  engaging monitor turned the amplifier on")
            else:
                print("FAIL  monitor is engaged but the amplifier stayed off")
                failures += 1

            time.sleep(2)
            still = qmp.speaker_on()
            print(f"still listening: speaker-on={still}")
            if still:
                print("PASS  it stays on; this is a state, not a blip")
            else:
                print("FAIL  the amplifier dropped again straight away")
                failures += 1

            if failures:
                return 1
            print("\nthe firmware's audio intent is observable")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
