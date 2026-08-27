#!/usr/bin/env python3
"""Press keys on the emulated radio through its QMP socket.

The keypad model exposes a "press" property: writing a key name holds that key,
writing an empty string releases it. The firmware debounces over several 10 ms
polls, so a press has to be held for a while to register -- see HOLD_MS.

Usage:
    key.py MENU              # one short press
    key.py MENU UP UP EXIT   # a sequence
    key.py --long F          # long press
    key.py --list            # show key names
"""

import argparse
import json
import socket
import sys
import time

QMP_SOCKET = "/tmp/uvk5-qmp.sock"
KEYPAD_PATH = "/machine/keypad"

# App/app/app.c debounces with key_debounce_10ms = 2 and treats
# key_repeat_delay_10ms = 40 as a long press. Guest time runs fast under
# emulation, so these are generous rather than exact.
# Guest time runs fast under emulation (SysTick reads are accelerated so busy-wait
# delays converge), so a press has to be held far longer in wall-clock terms than
# on real hardware for the firmware's debounce to complete. Measured: 400 ms was
# too short to register at all.
HOLD_MS = 2500
LONG_HOLD_MS = 6000
GAP_MS = 1200

KEYS = [
    "MENU", "UP", "DOWN", "EXIT", "F", "STAR",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "SIDE1", "SIDE2",
]


class Qmp:
    """Minimal QMP client: connect, negotiate, send commands."""

    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise SystemExit(
                f"cannot reach the emulator at {path}: {exc}\n"
                "Start it with sim/tools/run.sh first."
            ) from exc
        self.buf = b""
        self._read_json()                      # greeting
        self.command("qmp_capabilities")

    def _read_json(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("emulator closed the QMP connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name: str, **args) -> dict:
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        self.sock.sendall(json.dumps(payload).encode() + b"\n")

        while True:
            msg = self._read_json()
            if "error" in msg:
                raise SystemExit(f"QMP error: {msg['error'].get('desc', msg['error'])}")
            if "return" in msg:
                return msg["return"]
            # Events (RESET, STOP, ...) arrive interleaved; keep reading.

    def set_key(self, value: str) -> None:
        self.command("qom-set", path=KEYPAD_PATH, property="press", value=value)


def press(qmp: Qmp, key: str, hold_ms: int) -> None:
    qmp.set_key(key)
    time.sleep(hold_ms / 1000)
    qmp.set_key("")
    time.sleep(GAP_MS / 1000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="key names to press in order")
    ap.add_argument("--long", action="store_true", help="hold each key longer")
    ap.add_argument("--hold", type=int, help="hold time in ms, overrides --long")
    ap.add_argument("--list", action="store_true", help="list key names and exit")
    ap.add_argument("--socket", default=QMP_SOCKET)
    args = ap.parse_args()

    if args.list:
        print(" ".join(KEYS))
        return 0
    if not args.keys:
        ap.error("no keys given (try --list)")

    unknown = [k for k in args.keys if k.upper() not in KEYS]
    if unknown:
        raise SystemExit(f"unknown key(s): {', '.join(unknown)}\nKnown: {' '.join(KEYS)}")

    hold = args.hold if args.hold else (LONG_HOLD_MS if args.long else HOLD_MS)
    qmp = Qmp(args.socket)

    for key in args.keys:
        press(qmp, key.upper(), hold)
        print(f"pressed {key.upper()} ({hold} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
