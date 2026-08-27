#!/usr/bin/env bash
# Prove whether the keypad sees column changes, by driving a column by hand.
#
# Holds a key, writes GPIOB's BSRR to pull column 1 (pin 6) low, then reads IDR.
# If row0 goes low, the matrix is wired correctly and the earlier samples simply
# landed between scans. If it stays high, the column signal is not reaching the
# keypad model.
#
# BSRR: low half sets a pin, high half resets it (py32f071xB.h).
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"
BASE=0x50000400
BSRR=$((BASE + 0x18))
IDR=$((BASE + 0x10))

# Hold the key first.
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
    # Freeze the guest so the firmware's own scan cannot move the columns.
    echo "interrupt"
    echo "printf \"before  \""
    echo "x/1xw $IDR"
    # Pull pin 6 (column 1) low: write bit 6 into the reset half of BSRR.
    echo "set *(unsigned int *)$BSRR = 0x00400000"
    echo "printf \"col1 low\""
    echo "x/1xw $IDR"
    # Release it again.
    echo "set *(unsigned int *)$BSRR = 0x00000040"
    echo "printf \"released\""
    echo "x/1xw $IDR"
    echo "detach"
    echo "quit"
} >"$SCRIPT"

gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null | grep -E "before|col1 low|released|0x5000"
