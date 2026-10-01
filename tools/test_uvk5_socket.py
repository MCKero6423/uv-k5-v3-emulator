#!/usr/bin/env python3
"""Unit tests for the endpoint helper. No emulator needed."""
import socket
import unittest

import uvk5_socket


class TestEndpoints(unittest.TestCase):
    def test_the_endpoint_is_a_listening_tcp_form(self):
        "QEMU is the one listening, so the returned endpoint says so."
        srv, endpoint = uvk5_socket.listen("probe")
        self.addCleanup(srv.close)
        self.assertTrue(endpoint.startswith("unix:") or endpoint.startswith("tcp:"))

    def test_every_accepted_endpoint_form_connects(self):
        """The forms that reach connect() must all work.

        A bare "host:port" did not once: the parser read it as a scheme, left the host
        empty, and hung until the deadline -- so the page could not power the emulator on
        while a QEMU started by hand answered instantly.

        A fresh listener per form, because a backlog of one means the second client waits
        for an accept() nothing is calling -- which looks exactly like a broken parser.
        """
        for make in (lambda port: "127.0.0.1:%d" % port,
                     lambda port: "tcp:127.0.0.1:%d" % port,
                     lambda port: "tcp:127.0.0.1:%d,server=on,wait=off" % port):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            port = srv.getsockname()[1]
            self.addCleanup(srv.close)
            endpoint = make(port)
            with self.subTest(endpoint=endpoint):
                client = uvk5_socket.connect(endpoint, timeout=5)
                client.close()

    def test_the_two_directions_are_not_confused(self):
        srv_listen, endpoint = uvk5_socket.listen("x")
        self.addCleanup(srv_listen.close)
        self.assertNotIn("server=on", endpoint,
                         "listen() means QEMU connects out; asking it to serve is a deadlock")
        served = uvk5_socket.server_endpoint("x")
        self.assertIn("server=on", served)


if __name__ == "__main__":
    unittest.main()
