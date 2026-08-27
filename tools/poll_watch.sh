#!/usr/bin/env bash
# Break on KEYBOARD_Poll and single-step the scan, printing GPIOB ODR/IDR.
#
# Note: GDB *writes* to MMIO do not reach device models -- cpu_memory_rw_debug
# routes writes through address_space_write_rom, which only touches RAM/ROM. So
# this only observes; the firmware does the driving.
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"
STEPS="${2:-400}"

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
    echo "break *0x08004bd4"          # KEYBOARD_Poll
    echo "continue"
    echo "printf \"entered KEYBOARD_Poll\\n\""
    echo "delete"
    for _ in $(seq "$STEPS"); do
        echo "stepi"
        echo "printf \"pc=%#010x odr=%#06x idr=%#06x\\n\", \$pc, *(unsigned int *)0x50000414, *(unsigned int *)0x50000410"
    done
    echo "detach"
    echo "quit"
} >"$SCRIPT"

timeout 180 gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>&1 | grep -E "entered|pc=" | uniq -f2
