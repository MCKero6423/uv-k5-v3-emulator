#!/usr/bin/env bash
# Watch what KEYBOARD_Poll actually reads while a key is held.
#
# Prints the column and row state at each entry to the scan. This separates two
# failure modes that look identical from outside: the rows never going low when
# the firmware looks, versus the rows going low but the debounce rejecting them.
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"
SAMPLES="${2:-10}"

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
    # 0x08004bf0 is the `ldr r3, [r5, #16]` that reads IDR inside the debounce
    # loop -- after a column has been pulled low. Breaking at function entry
    # instead shows every column still high, which tells you nothing.
    echo "break *0x08004bf0"
    echo "commands"
    echo "silent"
    echo "printf \"scan ODR=%04x IDR=%04x\\n\", *(unsigned*)0x50000414, *(unsigned*)0x50000410"
    echo "continue"
    echo "end"
    for _ in $(seq "$SAMPLES"); do echo "continue"; done
    echo "detach"
    echo "quit"
} >"$SCRIPT"

timeout 90 gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null | grep '^scan' | head -"$SAMPLES"
