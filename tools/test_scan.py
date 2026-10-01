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

import uvk5_socket
import uvk5_testenv

SIM = pathlib.Path(__file__).resolve().parent.parent
QEMU = uvk5_testenv.qemu()
ELF = uvk5_testenv.firmware()
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
        self.s = uvk5_socket.connect(path, timeout=25)
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
    for tool, what in ((QEMU, "QEMU"), (ELF, "firmware"), (PRISTINE, "pristine flash image")):
        if tool is None or not tool.exists():
            return uvk5_testenv.skip("%s is missing (%s); see the README Quick start"
                                     % (what, tool or "not found"))

    with tempfile.TemporaryDirectory() as tmp:
        img = pathlib.Path(tmp) / "flash.img"
        img.write_bytes(gzip.decompress(PRISTINE.read_bytes()))
        sock = uvk5_socket.server_endpoint("qmp", directory=str(tmp))

        proc = subprocess.Popen(
            [str(QEMU), "-M", f"uv-k5-v3,flash-image={img}",
             "-nographic", "-monitor", "none",
             "-qmp", sock,
             "-kernel", str(ELF)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Qmp() below connects, and uvk5_socket retries until QEMU's QMP answers, so
            # there is no socket path to wait for -- on Windows there would not be one.
            time.sleep(BOOT_SECONDS)
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
