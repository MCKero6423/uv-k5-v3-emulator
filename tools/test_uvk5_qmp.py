#!/usr/bin/env python3
"""Unit tests for the QMP client. Uses a fake server, so no emulator needed."""
import json
import os
import socket
import tempfile
import threading
import unittest

from uvk5_qmp import QmpClient


def fake_server(path, script):
    """Minimal QMP server: greets, then replies to each command from `script`."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.sendall(json.dumps({"QMP": {"version": {}}}).encode() + b"\n")
        buf = b""
        for reply in script:
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            _, buf = buf.split(b"\n", 1)
            conn.sendall(json.dumps(reply).encode() + b"\n")
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv


class TestQmpClient(unittest.TestCase):
    def test_negotiates_and_returns_command_result(self):
        path = os.path.join(tempfile.mkdtemp(), "qmp.sock")
        # reply 1 = qmp_capabilities, reply 2 = our command
        fake_server(path, [{"return": {}}, {"return": {"status": "running"}}])

        client = QmpClient(path)
        self.addCleanup(client.close)
        self.assertEqual(client.command("query-status"), {"status": "running"})

    def test_raises_on_qmp_error(self):
        path = os.path.join(tempfile.mkdtemp(), "qmp.sock")
        fake_server(path, [{"return": {}},
                           {"error": {"class": "GenericError", "desc": "nope"}}])
        client = QmpClient(path)
        self.addCleanup(client.close)
        with self.assertRaises(RuntimeError) as ctx:
            client.command("bogus")
        self.assertIn("nope", str(ctx.exception))

    def test_skips_interleaved_events(self):
        """Events arrive unsolicited and must not be mistaken for a result.

        The fake server answers one command with an event followed by the real
        return, so a client that stopped at the first message would hand back the
        event instead.
        """
        path = os.path.join(tempfile.mkdtemp(), "qmp.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            conn.sendall(json.dumps({"QMP": {"version": {}}}).encode() + b"\n")
            buf = b""

            def next_command():
                nonlocal buf
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                line, buf = buf.split(b"\n", 1)
                return json.loads(line)

            next_command()                                  # qmp_capabilities
            conn.sendall(json.dumps({"return": {}}).encode() + b"\n")
            next_command()                                  # query-status
            # An event first, then the actual return.
            conn.sendall(json.dumps({"event": "RESUME"}).encode() + b"\n")
            conn.sendall(json.dumps({"return": {"status": "running"}}).encode() + b"\n")

        threading.Thread(target=run, daemon=True).start()

        client = QmpClient(path)
        self.addCleanup(client.close)
        self.assertEqual(client.command("query-status"), {"status": "running"})


if __name__ == "__main__":
    unittest.main()
