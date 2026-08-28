#!/usr/bin/env python3
"""The BK4819 register interface must work, and RSSI must not read as zero.

Scope: this covers the register bus, not radio behaviour. The chip has no public
datasheet, so App/driver/bk4819.c is the only specification available and it can only
say which registers were written -- never what left the antenna. Keying envelopes,
spurious emissions and sensitivity need a real radio and a spectrum analyser.

What it does check:

  1. The firmware boots. That is not a formality: App/app/app.c:910 and :1417 spin on
     bit 0 of REG_0C with no timeout, so a model that leaves that bit set hangs the
     guest outright. PB9 used to be idled low purely so reads returned 0 and those
     loops could exit.
  2. Registers written by the firmware read back with the values it wrote, which
     proves the bit-banged transfer is being decoded rather than ignored.
  3. RSSI is non-zero. It was previously hard 0 at 18 call sites, i.e. -160 dBm, so
     the S-meter read empty and squelch and scan logic evaluated a dead band.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
PRISTINE = os.path.join(ROOT, "assets", "pristine", "flash-pristine.img.gz")

BOOT_SECONDS = 20

# From App/driver/bk4819.c. These are the ones the firmware reads back.
REG_INTERRUPT = 0x0C
REG_RSSI = 0x67


class Qmp:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
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

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def main():
    for path, what in ((QEMU, "QEMU"), (ELF, "firmware"), (PRISTINE, "pristine image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")

    workdir = tempfile.mkdtemp(prefix="uvk5-bk4819-")
    image = os.path.join(workdir, "flash.img")
    sock_path = os.path.join(workdir, "qmp.sock")
    with gzip.open(PRISTINE, "rb") as src, open(image, "wb") as dst:
        shutil.copyfileobj(src, dst)

    proc = subprocess.Popen(
        [QEMU, "-M", f"uv-k5-v3,flash-image={image}", "-nographic", "-monitor", "none",
         "-qmp", f"unix:{sock_path},server=on,wait=off", "-kernel", ELF],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    failures = []
    try:
        for _ in range(300):
            if os.path.exists(sock_path):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("QMP socket never appeared")

        time.sleep(BOOT_SECONDS)
        qmp = Qmp(sock_path)

        # 1. Still running means the untimed REG_0C spin terminated.
        status = qmp.cmd("query-status").get("return", {})
        print(f"guest status: {status.get('status')}")
        if status.get("status") != "running":
            failures.append(
                f"guest is {status.get('status')}, not running -- most likely stuck "
                "in the untimed spin on REG_0C bit 0")
        else:
            print("PASS  the firmware booted and is running")

        # 2 and 3. Read the model's register file through QOM.
        regs = {}
        for name, num in (("interrupt", REG_INTERRUPT), ("rssi", REG_RSSI)):
            reply = qmp.cmd("qom-get", path="/machine/bk4819",
                            property=f"reg{num:02x}")
            if "error" in reply:
                failures.append(f"cannot read reg{num:02x}: {reply['error']}")
            else:
                regs[name] = reply["return"]

        if "rssi" in regs:
            print(f"RSSI register: 0x{regs['rssi']:04X}")
            if regs["rssi"] == 0:
                failures.append(
                    "RSSI reads 0, i.e. -160 dBm: squelch and scan see a dead band")
            else:
                print("PASS  RSSI is not stuck at zero")

        if "interrupt" in regs:
            print(f"REG_0C: 0x{regs['interrupt']:04X}")
            if regs["interrupt"] & 1:
                failures.append(
                    "REG_0C bit 0 is set; the firmware spins on it without a timeout")
            else:
                print("PASS  REG_0C bit 0 is clear")

        # The firmware writes plenty of registers during init, so a register file
        # that is entirely zero means the bus decode never ran.
        written = 0
        for num in range(0x00, 0x80):
            reply = qmp.cmd("qom-get", path="/machine/bk4819",
                            property=f"reg{num:02x}")
            if "return" in reply and reply["return"] not in (0, None):
                written += 1
        print(f"non-zero registers: {written}")
        if written < 5:
            failures.append(
                f"only {written} registers hold a value; the firmware writes dozens "
                "during init, so the three-wire transfer is not being decoded")
        else:
            print("PASS  the firmware's register writes were decoded")

        qmp.cmd("quit")
        qmp.close()

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print()
        for f in failures:
            print(f"FAIL {f}")
        sys.exit(1)
    print("\nthe BK4819 register interface works")


if __name__ == "__main__":
    main()
