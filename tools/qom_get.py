#!/usr/bin/env python3
"""Read a QOM property from the running emulator.

Usage: qom_get.py <path> <property>
       qom_get.py /machine/keypad press
"""

import json
import socket
import sys

SOCKET = "/tmp/uvk5-qmp.sock"


class Qmp:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self._read()
        self.command("qmp_capabilities")

    def _read(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("QMP connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name: str, **args):
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


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    value = Qmp(SOCKET).command("qom-get", path=sys.argv[1], property=sys.argv[2])
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
