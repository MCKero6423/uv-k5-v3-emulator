#!/usr/bin/env bash
# What does KEYBOARD_Poll return, and what does the app do with it?
#
# The scan is already proven to read the right row (IDR bit 15 low for MENU), so
# the remaining question is downstream: does Poll return the key code, and does
# the debounce in app.c accept it?
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"

python3 - "$KEY" <<'PY'
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
print(f"holding {sys.argv[1]}")
PY

SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

{
    echo "set confirm off"
    echo "set pagination off"
    echo "target remote :1234"
    # Return value in r0. KEY_MENU is 5 in KEY_Code_t; KEY_INVALID is 255.
    echo "break *0x08004c58"
    echo "commands"
    echo "silent"
    echo "printf \"Poll returns %d\\n\", \$r0"
    echo "continue"
    echo "end"
    for _ in $(seq 8); do echo "continue"; done
    echo "detach"
    echo "quit"
} >"$SCRIPT"

timeout 90 gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null | grep 'Poll returns' | head -8
