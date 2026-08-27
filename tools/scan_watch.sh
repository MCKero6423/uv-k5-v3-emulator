#!/usr/bin/env bash
# Watch the firmware's own keypad scan while a key is held.
#
# Breaks inside KEYBOARD_Poll right after read_rows(), and prints the column
# index being scanned together with the row bits actually sampled. This
# distinguishes "the scan never runs" from "the scan runs but the rows never
# go low".
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"
HITS="${2:-12}"

python3 - "$KEY" <<'PY'
import json, socket, sys
key = sys.argv[1]
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
          {"execute": "qom-set", "arguments": {"path": "/machine/keypad",
                                               "property": "press", "value": key}}):
    s.sendall(json.dumps(p).encode() + b"\n")
    while True:
        m = rd()
        if "return" in m or "error" in m:
            break
print(f"holding {key}")
PY

SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

{
    echo "set confirm off"
    echo "set pagination off"
    echo "target remote :1234"
    echo "break keyboard.c:231"
    echo "commands"
    echo "silent"
    echo "printf \"col j=%u reg2=%#06x odr=%#06x\\n\", j, reg2, *(unsigned int *)0x50000414"
    echo "continue"
    echo "end"
    for _ in $(seq "$HITS"); do echo "continue"; done
    echo "detach"
    echo "quit"
} >"$SCRIPT"

timeout 120 gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>&1 | grep -E "col j=|Breakpoint|No symbol|Function"
