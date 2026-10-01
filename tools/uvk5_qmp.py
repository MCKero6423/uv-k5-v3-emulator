#!/usr/bin/env python3
"""Minimal QMP client for the UV-K5 emulator.

Deliberately not shared with tools/key.py: that script is standalone on purpose,
so it runs with nothing but the stdlib and no sys.path juggling.

Only one client can hold the QMP socket at a time -- the emulator is started with
server=on,wait=off, which accepts a single connection. A long-lived server owns
it and callers serialise through the lock.
"""
import json
import socket
import threading


def connect(endpoint: str, timeout: float):
    """Open the QMP connection.

    Accepts everything QEMU accepts: a bare unix path, host:port, or the full forms
    tcp:host:port[,options] and unix:path -- which is what uvk5_socket.listen() hands
    back, so the listening and connecting sides cannot drift apart. One parser, in
    uvk5_socket, because two of them is how the Windows path broke: this one only
    understood host:port, so a tcp:... endpoint fell through to the unix branch and
    failed with a missing AF_UNIX.
    """
    import uvk5_socket

    return uvk5_socket.connect(endpoint, timeout=timeout)


class QmpClient:
    def __init__(self, path: str, timeout: float = 5.0):
        self._lock = threading.Lock()
        try:
            self._sock = connect(path, timeout)
        except OSError as exc:
            raise RuntimeError(
                f"cannot reach the emulator at {path}: {exc}\n"
                "Start it with tools/run.sh first (on Windows pass --qmp "
                "127.0.0.1:4444 and start QEMU with -qmp tcp:...). Note the QMP "
                "socket takes a single client, so tools/key.py cannot be "
                "connected at the same time."
            ) from exc
        self._buf = b""
        self._read_json()                       # greeting
        self.command("qmp_capabilities")

    def _read_json(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP connection closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def command(self, name: str, **args):
        payload = {"execute": name}
        if args:
            payload["arguments"] = args
        with self._lock:
            self._sock.sendall(json.dumps(payload).encode() + b"\n")
            while True:
                msg = self._read_json()
                if "error" in msg:
                    raise RuntimeError(
                        f"QMP {name} failed: "
                        f"{msg['error'].get('desc', msg['error'])}")
                if "return" in msg:
                    return msg["return"]
                # Events (STOP, RESET, RESUME, ...) interleave with replies;
                # keep reading until the matching return arrives.

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
