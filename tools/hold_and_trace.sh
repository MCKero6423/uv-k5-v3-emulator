#!/usr/bin/env bash
# Hold a key without releasing it, then look at the row levels the keypad drives.
#
# key.py presses and releases, so sampling afterwards always shows the released
# state. This holds the key for the whole observation window instead.
set -uo pipefail

KEY="${1:-MENU}"
LOG=/tmp/uvk5-trace.log

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
for payload in (
    {"execute": "qmp_capabilities"},
    {"execute": "qom-set", "arguments": {"path": "/machine/keypad",
                                        "property": "press", "value": key}},
    {"execute": "qom-get", "arguments": {"path": "/machine/keypad",
                                        "property": "press"}},
):
    s.sendall(json.dumps(payload).encode() + b"\n")
    while True:
        m = rd()
        if "return" in m:
            last = m["return"]
            break
        if "error" in m:
            raise SystemExit("QMP error: " + m["error"].get("desc", "?"))
print(f"holding {key!r}, property reads back {last!r}")
PY

# Watch what the keypad drives while the key stays held.
before=$(wc -l < "$LOG")
sleep 3
tail -n +"$before" "$LOG" | grep 'keypad row' | sort -u | head -8

echo
echo "distinct row levels seen while held:"
tail -n +"$before" "$LOG" | grep -oE 'row[0-9] -> [01]' | sort -u
