#!/usr/bin/env python3
"""A scan must keep stepping, even though the receiver always reports a signal.

This guards a regression the BK4819 model could easily cause. bk4819_eval_receiver
reports RSSI comfortably above any sane squelch threshold, and a scan halts when it
finds a busy channel -- so a band that is permanently busy could stop the scan dead on
its first step. It does not, and this keeps it that way.

The check deliberately does not count distinct frames. RSSI varies on every poll, so
the meter and its dBm readout change constantly and would give a perfect "all frames
differ" score on a completely stationary radio. Instead it compares the framebuffer
rows holding the large frequency digits: those only change if the radio retunes.

The framebuffer is 128x64 as 8 pages of 128 bytes, page p covering rows 8p..8p+7. The
frequency digits for the upper VFO occupy pages 1-2.
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

FRAME_ADDR = 0x200013DC
FRAME_BYTES = 1024
PAGE = 128
FREQ_PAGES = slice(PAGE * 1, PAGE * 3)

BOOT_SECONDS = 24
SAMPLES = 6
SAMPLE_GAP = 1.5


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

    def frame(self, tmp):
        """memsave, not pmemsave: the latter takes a physical address and silently
        returns zeros here, which looks like a blank screen with no error."""
        out = pathlib.Path(tmp) / "f.bin"
        self.cmd("memsave", val=FRAME_ADDR, size=FRAME_BYTES, filename=str(out))
        return out.read_bytes()

    def key(self, name, hold=0.12):
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

            # Long-press STAR starts a scan.
            qmp.key("STAR", hold=0.8)
            time.sleep(1.5)

            frames = []
            for _ in range(SAMPLES):
                frames.append(qmp.frame(tmp))
                time.sleep(SAMPLE_GAP)

            tunings = {f[FREQ_PAGES] for f in frames}
            print(f"frequency display: {len(tunings)} distinct over "
                  f"{SAMPLES} samples")

            qmp.key("EXIT")

            if len(tunings) > 1:
                print("PASS  the scan steps through frequencies")
                print("\na busy band does not stall the scan")
                return 0
            print("FAIL  the frequency never changed; the scan is stuck, which is "
                  "what an always-busy receiver would cause")
            return 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
