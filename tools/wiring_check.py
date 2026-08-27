#!/usr/bin/env python3
"""Verify the keypad GPIO wiring in the running machine.

qdev out-GPIOs are QOM link properties, so the board's wiring is directly
observable: /machine/soc/b "pin-out[6]" should point at a keypad "col" input,
and /machine/keypad "row[0]" should point at a GPIOB "pin-in" input. A link that
reads back empty means the connection was never made.
"""

import json
import socket

SOCKET = "/tmp/uvk5-qmp.sock"


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
                return {"__error__": msg["error"].get("desc", "?")}


def get(q, path, prop):
    r = q.cmd("qom-get", path=path, property=prop)
    if isinstance(r, dict) and "__error__" in r:
        return "ERR: " + r["__error__"]
    return r


def main():
    q = Qmp(SOCKET)

    print("== /machine children ==")
    for it in q.cmd("qom-list", path="/machine"):
        print(f"   {it['name']:20s} {it.get('type','')}")

    print("\n== GPIOB column outputs (pins 6..3 = cols 1..4) ==")
    for c in range(1, 5):
        pin = 6 - (c - 1)
        print(f"   pin-out[{pin}] -> {get(q, '/machine/soc/b', f'pin-out[{pin}]')}")

    print("\n== keypad row outputs (rows 0..3 -> pins 15..12) ==")
    for r in range(4):
        print(f"   row[{r}] -> {get(q, '/machine/keypad', f'row[{r}]')}")

    print("\n== keypad col input objects (targets of the wiring above) ==")
    for it in q.cmd("qom-list", path="/machine/keypad"):
        if it["name"].startswith(("col[", "row[")):
            print(f"   {it['name']:12s} {it.get('type','')}")

    print("\n== GPIOB pin-in objects for the row pins ==")
    for it in q.cmd("qom-list", path="/machine/soc/b"):
        if it["name"].startswith("pin-in["):
            n = int(it["name"][7:-1])
            if n >= 12:
                print(f"   {it['name']:12s} {it.get('type','')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
