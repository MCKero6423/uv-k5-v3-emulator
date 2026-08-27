#!/usr/bin/env bash
# Press keys, then screenshot -- without any GDB breakpoints in between.
#
# Breakpoints halt the guest, so a key held across a breakpoint session is never
# processed by the main loop. This holds the key, lets the machine run freely,
# releases, and only then reads the framebuffer.
set -uo pipefail

TOOLS="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-/root/vm_screen.png}"
HOLD="${HOLD:-3}"
SETTLE="${SETTLE:-3}"

hold_key() {
    python3 - "$1" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/tmp/uvk5-qmp.sock")
buf = b""


def rd():
    global buf
    while b"\n" not in buf:
        buf += s.recv(4096)
    line, buf = buf.split(b"\n", 1)
    return json.loads(line)


rd()
for p in ({"execute": "qmp_capabilities"},
          {"execute": "qom-set",
           "arguments": {"path": "/machine/keypad", "property": "press",
                         "value": sys.argv[1]}}):
    s.sendall(json.dumps(p).encode() + b"\n")
    while True:
        m = rd()
        if "return" in m or "error" in m:
            break
PY
}

for key in "$@"; do
    echo "press $key"
    hold_key "$key"
    sleep "$HOLD"
    hold_key ""
    sleep "$SETTLE"
done

rm -f /tmp/_screen_dump.bin
python3 "$TOOLS/screenshot.py" \
    --frame-addr 0x200013DC --status-addr 0x2000175C \
    --port 1234 --out "$OUT" --scale 4 2>&1 | grep pixels
