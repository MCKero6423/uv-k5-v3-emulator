#!/usr/bin/env python3
"""List QOM child nodes under a path, to find where a device actually lives.

Usage: qom_ls.py [/machine]
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
        self._read()                       # greeting
        self.command("qmp_capabilities")

    def _read(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("QMP connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name: str, **args) -> dict:
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
    root = sys.argv[1] if len(sys.argv) > 1 else "/machine"
    for item in Qmp(SOCKET).command("qom-list", path=root):
        kind = item.get("type", "")
        marker = "dir" if kind.startswith("child<") else "   "
        print(f"{marker}  {item['name']:24s} {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
