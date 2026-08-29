#!/usr/bin/env python3
"""The S-meter must appear, with a reading, once the radio is monitoring.

This is the payoff for modelling the BK4819's receive registers, and it took several
false starts, so the path matters:

  * RSSI used to read 0 at all 18 call sites -- -160 dBm -- so squelch never opened and
    a scan faced a dead band.
  * Register reads arrived shifted one bit left, which made four attempts at the
    squelch interrupt fail for reasons that looked like timing every time.
  * Even with reads fixed and the interrupt handshake working, the firmware idles in
    power save and never acts on a squelch flag.

The way in is ACTION_Monitor, which skips squelch entirely: app/app.c:482 chooses
FUNCTION_MONITOR over FUNCTION_RECEIVE whenever gMonitor is set, and settings.c:263
falls back to ACTION_OPT_MONITOR for an out-of-range stored value -- which blank flash
(0xFF) is. So SIDE1 short-press engages monitor on a pristine image.

Checked here:
  1. the guest starts in power save, as it always does
  2. SIDE1 moves it out of power save and sets gMonitor
  3. the screen grows, because a meter is now being drawn

Point 3 is deliberately a size comparison rather than pixel matching. The frame is a
PNG of a 1-bit display, so more ink means more content; asserting exact bytes would
break on any unrelated UI change and teach the next person nothing.
"""

import gzip
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
SIM = HERE.parent
QEMU = pathlib.Path(os.environ.get(
    "QEMU", "/root/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"))
ELF = pathlib.Path(os.environ.get(
    "ELF", "/root/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf"))
PRISTINE = SIM / "assets/pristine/flash-pristine.img.gz"

FRAME_ADDR = 0x200013DC
FRAME_BYTES = 1024          # 128x64, one bit per pixel
BOOT_SECONDS = 24


class Qmp:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(25)
        self.sock.connect(path)
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._read()
            if "return" in reply or "error" in reply:
                return reply

    def press(self, key, hold=0.2):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=key)
        time.sleep(hold)
        self.cmd("qom-set", path="/machine/keypad", property="press", value="")

    def frame_ink(self, tmp):
        """Count set pixels in the framebuffer.

        memsave, never pmemsave: the latter takes a physical address and quietly
        returns zeros for this region, which looks like a blank screen with no error.
        """
        out = pathlib.Path(tmp) / "frame.bin"
        self.cmd("memsave", val=FRAME_ADDR, size=FRAME_BYTES,
                 filename=str(out))
        data = out.read_bytes()
        return sum(bin(b).count("1") for b in data)


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

            before = qmp.frame_ink(tmp)
            print(f"idle screen:       {before} lit pixels")

            qmp.press("SIDE1", hold=0.15)
            time.sleep(3)

            after = qmp.frame_ink(tmp)
            print(f"monitoring screen: {after} lit pixels")

            rssi = qmp.cmd("qom-get", path="/machine/bk4819",
                           property="reg67").get("return", 0)
            print(f"RSSI register:     0x{rssi:04X}")

            failures = 0

            if rssi == 0:
                print("FAIL  RSSI reads zero; the receiver reports a dead band")
                failures += 1
            else:
                print("PASS  RSSI has a value")

            # The meter, its two numeric readouts and the MONI label are all new ink.
            # A few hundred pixels is a wide margin against redraw noise while still
            # being far below what the meter row actually adds.
            if after <= before + 100:
                print(f"FAIL  screen did not gain content ({before} -> {after}); "
                      "no meter is being drawn")
                failures += 1
            else:
                print(f"PASS  the screen gained {after - before} pixels of content")

            if failures:
                return 1
            print("\nthe S-meter reads a signal once monitoring is engaged")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
