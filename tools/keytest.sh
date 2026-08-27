#!/usr/bin/env bash
# Verify the press property round-trips: set a key, read it back, release it.
#
# A mismatch here means the QMP path or the property is wrong. A match means the
# model has the key held, and any failure to reach the firmware is downstream --
# in the matrix wiring or the debounce.
set -uo pipefail

TOOLS="$HOME/uvk5-port/sim/tools"
KEY="${1:-MENU}"

python3 - "$KEY" <<'PY'
import json, socket, sys, time

KEY = sys.argv[1]
SOCK = "/tmp/uvk5-qmp.sock"


class Qmp:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(path)
        self.buf = b""
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            self.buf += self.s.recv(4096)
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, name, **args):
        p = {"execute": name}
        if args:
            p["arguments"] = args
        self.s.sendall(json.dumps(p).encode() + b"\n")
        while True:
            m = self._read()
            if "return" in m:
                return m["return"]
            if "error" in m:
                raise SystemExit("QMP error: " + m["error"].get("desc", "?"))


q = Qmp(SOCK)
q.cmd("qom-set", path="/machine/keypad", property="press", value=KEY)
held = q.cmd("qom-get", path="/machine/keypad", property="press")
print(f"set {KEY!r} -> reads back {held!r}  {'OK' if held == KEY else 'MISMATCH'}")
time.sleep(0.5)
q.cmd("qom-set", path="/machine/keypad", property="press", value="")
print("released ->", repr(q.cmd("qom-get", path="/machine/keypad", property="press")))
PY
