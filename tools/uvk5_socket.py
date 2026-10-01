#!/usr/bin/env python3
"""Sockets for talking to QEMU, on platforms that may not have unix sockets.

The tools and tests used to hardcode socket.AF_UNIX, which a Windows QEMU cannot
create -- so the emulator and the page worked there while the *verification* path did
not, which is the half that matters. Everything here returns an endpoint string in the
form QEMU expects, so a caller never has to know which kind it got:

    srv, endpoint = uvk5_socket.listen("qmp")      # QEMU connects to this
    qemu_args = ["-qmp", endpoint]

That endpoint is unix:/tmp/uvk5-XXXX/qmp.sock where unix sockets exist, and
tcp:127.0.0.1:<port>,server=on,wait=off otherwise. QmpClient and the supervisor already
accept both forms; the listener side is what was missing.
"""
from __future__ import annotations

import os
import socket
import tempfile
import time

HAVE_UNIX = hasattr(socket, "AF_UNIX")


def can_use_unix():
    """True when a unix socket can actually be bound, not merely that the name exists."""
    if not HAVE_UNIX:
        return False
    probe_dir = tempfile.mkdtemp(prefix="uvk5-probe-")
    path = os.path.join(probe_dir, "probe.sock")
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.bind(path)
        finally:
            probe.close()
        return True
    except OSError:
        return False
    finally:
        for cleanup in (lambda: os.unlink(path), lambda: os.rmdir(probe_dir)):
            try:
                cleanup()
            except OSError:
                pass


def listen(name: str = "qmp", directory: str | None = None):
    """A listening socket plus the endpoint for QEMU to connect *out* to.

    Use server_endpoint() instead when QEMU should be the one listening.

    Returns (socket, endpoint). The caller closes the socket and removes the directory
    it was given, if any.
    """
    keep = directory or tempfile.mkdtemp(prefix="uvk5-%s-" % name)
    if can_use_unix():
        path = os.path.join(keep, name + ".sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        return srv, "unix:" + path
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, "tcp:127.0.0.1:%d" % srv.getsockname()[1]


def server_endpoint(name: str = "qmp", directory: str | None = None) -> str:
    """An endpoint for QEMU to LISTEN on -- the caller then connects to it.

    This is the direction the emulator tests use, and the opposite of listen():
    with server=on QEMU binds the port itself, so a listener held by the test makes
    QEMU fail to start and the test then connects to its own socket and waits for a
    greeting that can never come. That mistake cost a debugging round here.
    """
    if can_use_unix():
        keep = directory or tempfile.mkdtemp(prefix="uvk5-%s-" % name)
        return "unix:" + os.path.join(keep, name + ".sock") + ",server=on,wait=off"
    return "tcp:127.0.0.1:%d,server=on,wait=off" % free_port()


def free_port() -> int:
    """A port nothing is listening on, for a caller that wants to name one itself."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def connect(endpoint: str, timeout: float = 20.0):
    """Connect to whatever listen() returned, waiting for it to appear."""
    kind, _, rest = endpoint.partition(":")
    if kind not in ("unix", "tcp"):
        # A bare "host:port", which is what the supervisor, the web UI and the README
        # have always passed. Read as a scheme, its "host" is the address itself and
        # the port is empty, so the connect goes to an empty host and hangs until the
        # deadline -- which reads as "the emulator never started" while the guest is
        # running happily. That shipped once and cost a page that would not power on.
        kind, rest = "tcp", endpoint
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if kind == "unix":
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(rest.partition(",")[0])

                return sock
            host, _, port = rest.partition(",")[0].rpartition(":")
            return socket.create_connection((host, int(port)), timeout=2.0)
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    raise OSError("could not connect to %s: %s" % (endpoint, last))


if __name__ == "__main__":
    srv, endpoint = listen("demo")
    print("unix sockets available:", can_use_unix())
    print("listening on", endpoint)
    srv.close()
