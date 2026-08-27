#!/usr/bin/env python3
"""Check whether a held key actually pulls a GPIOB row line low.

Holds a key over QMP, then reads GPIOB's input data register through the GDB
stub. The row pins are 15..12; with a key held and its column pulled low, the
matching row bit must read 0.

This isolates two failure modes that look identical from the firmware's side:
the keypad model not registering the press, and the row lines not reaching the
GPIO port.

Usage: gpio_watch.py [KEY]
"""

import json
import re
import socket
import subprocess
import sys
import tempfile
import time

QMP_SOCKET = "/tmp/uvk5-qmp.sock"
GPIOB_BASE = 0x50000400
GPIO_IDR = 0x10
GPIO_ODR = 0x14
ELF = "/root/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf"


class Qmp:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("QMP closed")
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
            if "return" in msg:
                return msg["return"]
            if "error" in msg:
                raise SystemExit("QMP error: " + msg["error"].get("desc", "?"))

    def press(self, key):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=key)


def read_words(addresses):
    """Reads several 32-bit words through the GDB stub in one session."""
    lines = ["set confirm off", "set pagination off", "target remote :1234"]
    lines += [f"x/1xw {a:#x}" for a in addresses]
    lines += ["detach", "quit", ""]

    with tempfile.NamedTemporaryFile("w", suffix=".gdb", delete=False) as fh:
        fh.write("\n".join(lines))
        script = fh.name

    out = subprocess.run(["gdb-multiarch", "-batch", "-x", script, ELF],
                         capture_output=True, text=True, timeout=60).stdout
    # gdb prints "0x50000410 <optional symbol>:\t0xffff". Match the address at
    # line start and the first hex value after the colon; an earlier pattern that
    # required no colon before the value silently matched nothing and every read
    # came back as zero.
    values = {}
    for match in re.finditer(r"^(0x[0-9a-fA-F]+)[^:\n]*:\s*(0x[0-9a-fA-F]+)", out, re.M):
        values[int(match.group(1), 16)] = int(match.group(2), 16)
    return values


def describe(idr, odr):
    rows = [(15 - r, r) for r in range(4)]
    cols = [(6 - (c - 1), c) for c in range(1, 5)]
    row_txt = " ".join(f"row{r}(p{p})={'LOW' if not (idr >> p) & 1 else 'high'}"
                       for p, r in rows)
    col_txt = " ".join(f"col{c}(p{p})={'LOW' if not (odr >> p) & 1 else 'high'}"
                       for p, c in cols)
    return row_txt, col_txt


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "MENU"
    qmp = Qmp(QMP_SOCKET)

    qmp.press("")
    time.sleep(0.2)
    base = read_words([GPIOB_BASE + GPIO_IDR, GPIOB_BASE + GPIO_ODR])
    idr0 = base.get(GPIOB_BASE + GPIO_IDR, 0)
    odr0 = base.get(GPIOB_BASE + GPIO_ODR, 0)

    qmp.press(key)
    time.sleep(0.2)
    held = read_words([GPIOB_BASE + GPIO_IDR, GPIOB_BASE + GPIO_ODR])
    idr1 = held.get(GPIOB_BASE + GPIO_IDR, 0)
    odr1 = held.get(GPIOB_BASE + GPIO_ODR, 0)
    qmp.press("")

    print(f"released: IDR={idr0:#06x} ODR={odr0:#06x}")
    print(f"  {describe(idr0, odr0)[0]}")
    print(f"held {key}: IDR={idr1:#06x} ODR={odr1:#06x}")
    print(f"  {describe(idr1, odr1)[0]}")
    print(f"  {describe(idr1, odr1)[1]}")

    if idr0 == idr1:
        print("\nno change in IDR: the row lines are not reaching the GPIO port, "
              "or the scan had every column high at sample time")
    else:
        print(f"\nIDR changed (bits {idr0 ^ idr1:#06x}) -- the matrix is wired through")
    return 0


if __name__ == "__main__":
    sys.exit(main())
