#!/usr/bin/env python3
"""Holding PTT must put the radio into transmit, and draw the mic level bar.

PTT was the one input the model never had. It is not a matrix key -- GPIO_IsPttPressed
reads PB10 directly (driver/gpio.h:31, active low) -- so it needed its own line rather
than a column/row intersection.

With it, the transmit audio bar becomes reachable. app/app.c:1700 draws it only while
gCurrentFunction == FUNCTION_TRANSMIT and gSetting_mic_bar is set; that setting is
Data[7] bit 4 at flash 0xA0A8 (settings.c:423), and blank flash reads 0xFF, so it is on
by default. The level itself comes from REG_64 via BK4819_GetVoiceAmplitudeOut.

Checked here:
  1. the radio is not transmitting to begin with
  2. holding PTT reaches FUNCTION_TRANSMIT
  3. the screen changes while transmitting
  4. releasing PTT leaves transmit again

Point 4 matters as much as the rest: a PTT that sticks would leave the emulated radio
keyed forever, and every later test would run against a transmitting radio.
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
BOOT_SECONDS = 24

FUNCTION_TRANSMIT = 1


class Qmp:
    def __init__(self, endpoint):
        # A socket or an endpoint: QEMU's QMP takes one client, so the caller that
        # waited for it hands its connection in.
        if hasattr(endpoint, "recv"):
            self.sock = endpoint
        else:
            self.sock = uvk5_socket.connect(endpoint, timeout=30)
        self.sock.settimeout(25)
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

    def set_ptt(self, held):
        return self.cmd("qom-set", path="/machine/keypad",
                        property="ptt", value=held)

    def ink(self, tmp):
        """Lit pixels in the framebuffer.

        memsave, not pmemsave: the latter takes a physical address and silently
        returns zeros here, which looks like a blank screen with no error.
        """
        out = pathlib.Path(tmp) / "f.bin"
        self.cmd("memsave", val=FRAME_ADDR, size=FRAME_BYTES, filename=str(out))
        return sum(bin(b).count("1") for b in out.read_bytes())

    def read_u8(self, addr):
        """Not available over QMP; callers use gdb for firmware globals."""
        raise NotImplementedError


def function_value(elf, port):
    """Read gCurrentFunction over gdb.

    Stopping the guest is acceptable here: the question is which state it settled in,
    not anything timing-dependent. Key injection would be a different matter.
    """
    out = subprocess.run(
        [str(uvk5_testenv.gdb()), "-batch",
         "-ex", "set confirm off", "-ex", "set pagination off",
         "-ex", f"target remote :{port}",
         "-ex", 'printf "FN=%d\\n", *(unsigned char*)&gCurrentFunction',
         "-ex", "detach", "-ex", "quit", str(elf)],
        capture_output=True, text=True, timeout=60)
    for line in out.stdout.splitlines():
        if line.startswith("FN="):
            return int(line[3:])
    return -1


def main():
    if uvk5_testenv.gdb() is None:
        return uvk5_testenv.skip("gdb-multiarch is missing; this test reads firmware "
                                 "globals over a gdb attach, which has not been ported to "
                                 "the QMP memsave route the page uses")
    for tool, what in ((QEMU, "QEMU"), (ELF, "firmware"), (PRISTINE, "pristine flash image")):
        if tool is None or not tool.exists():
            return uvk5_testenv.skip("%s is missing (%s); see the README Quick start"
                                     % (what, tool or "not found"))

    port = 1261
    with tempfile.TemporaryDirectory() as tmp:
        img = pathlib.Path(tmp) / "flash.img"
        img.write_bytes(gzip.decompress(PRISTINE.read_bytes()))
        sock = uvk5_socket.server_endpoint("qmp", directory=str(tmp))

        proc = subprocess.Popen(
            [str(QEMU), "-M", f"uv-k5-v3,flash-image={img}",
             "-nographic", "-monitor", "none",
             "-qmp", sock,
             "-kernel", str(ELF), "-gdb", f"tcp::{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Qmp() below connects, and uvk5_socket retries until QEMU's QMP answers, so
            # there is no socket path to wait for -- on Windows there would not be one.
            time.sleep(BOOT_SECONDS)
            time.sleep(BOOT_SECONDS)

            qmp = Qmp(str(sock))
            failures = 0

            idle_fn = function_value(ELF, port)
            idle_ink = qmp.ink(tmp)
            print(f"idle:          fn={idle_fn}, {idle_ink} lit pixels")
            if idle_fn == FUNCTION_TRANSMIT:
                print("FAIL  already transmitting before PTT was touched")
                failures += 1
            else:
                print("PASS  not transmitting to begin with")

            qmp.set_ptt(True)
            time.sleep(3)
            tx_fn = function_value(ELF, port)
            tx_ink = qmp.ink(tmp)
            print(f"PTT held:      fn={tx_fn}, {tx_ink} lit pixels")

            if tx_fn == FUNCTION_TRANSMIT:
                print("PASS  PTT put the radio into transmit")
            else:
                print(f"FAIL  expected fn={FUNCTION_TRANSMIT}, got {tx_fn}")
                failures += 1

            if tx_ink != idle_ink:
                print(f"PASS  the display changed ({idle_ink} -> {tx_ink})")
            else:
                print("FAIL  the display did not change while transmitting")
                failures += 1

            qmp.set_ptt(False)
            time.sleep(3)
            rel_fn = function_value(ELF, port)
            print(f"PTT released:  fn={rel_fn}")
            if rel_fn != FUNCTION_TRANSMIT:
                print("PASS  releasing PTT left transmit")
            else:
                print("FAIL  still transmitting after release; PTT is stuck")
                failures += 1

            if failures:
                return 1
            print("\nPTT keys the radio and releases cleanly")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
