#!/usr/bin/env bash
# Pull column 1 (PB6) low by hand and read back BOTH ODR and IDR.
#
# Reading ODR proves whether the MMIO write reached the GPIO model at all;
# reading IDR proves whether the keypad drove the row line back. The earlier
# probe only read IDR, which cannot tell those two apart.
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
KEY="${1:-MENU}"
BASE=0x50000400
IDR=$((BASE + 0x10))
ODR=$((BASE + 0x14))
BSRR=$((BASE + 0x18))
BRR=$((BASE + 0x28))

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
                                               "property": "press", "value": key}},
          {"execute": "qom-get", "arguments": {"path": "/machine/keypad",
                                               "property": "press"}}):
    s.sendall(json.dumps(p).encode() + b"\n")
    while True:
        m = rd()
        if "return" in m:
            if p["execute"] == "qom-get":
                print("keypad reports held key:", m["return"])
            break
        if "error" in m:
            print("QMP error:", m["error"]); break
PY

SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

{
    echo "set confirm off"
    echo "set pagination off"
    echo "target remote :1234"
    echo "interrupt"
    echo "printf \"idle    ODR \""
    echo "x/1xw $ODR"
    echo "printf \"idle    IDR \""
    echo "x/1xw $IDR"
    # Pull PB6 low through BRR (offset 0x28) -- the same register the driver uses.
    echo "set *(unsigned int *)$BRR = 0x40"
    echo "printf \"brr low ODR \""
    echo "x/1xw $ODR"
    echo "printf \"brr low IDR \""
    echo "x/1xw $IDR"
    # And through BSRR's reset half, the other path in the model.
    echo "set *(unsigned int *)$BSRR = 0x00400000"
    echo "printf \"bsrr    ODR \""
    echo "x/1xw $ODR"
    echo "printf \"bsrr    IDR \""
    echo "x/1xw $IDR"
    echo "set *(unsigned int *)$BSRR = 0x00000040"
    echo "detach"
    echo "quit"
} >"$SCRIPT"

timeout 120 gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>&1 | grep -E "idle|brr low|bsrr|0x5000"
