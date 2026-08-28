#!/usr/bin/env python3
"""A bounded log buffer for the web UI.

Collects supervisor events, QEMU stderr, and firmware serial output (which the
machine model prints as "SERIAL <line>") so the browser has something to show.

In memory and bounded on purpose: this is a debugging aid inside a long-running
server, so an unbounded buffer would be a slow leak. Anyone wanting a permanent
record can redirect the server's own stderr to a file.

Every entry carries a monotonic `seq`, so a polling client can ask for "anything
after N" and get each line exactly once even when older entries have been evicted.
"""
import collections
import threading
import time


class LogBuffer:
    def __init__(self, capacity: int = 500):
        self._entries = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, source: str, text: str, ip: str = None):
        """Record one line. `ip` identifies the client that caused it.

        The buffer is shared by every viewer, so without an attributed IP a log of
        keypresses from two people is unreadable. Entries with no client behind
        them -- firmware serial, QEMU stderr -- carry None.
        """
        with self._lock:
            self._seq += 1
            self._entries.append({
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "ip": ip,
                "source": source,
                "text": text,
            })

    def cursor(self) -> int:
        with self._lock:
            return self._seq

    def entries(self, since: int = 0):
        with self._lock:
            return [e for e in self._entries if e["seq"] > since]

    def pump_stream(self, stream, default_source: str = "qemu"):
        """Read a byte stream to EOF, one entry per line.

        Lines the machine model tags with "SERIAL " are firmware output and are
        recorded under their own source, so the UI can tell them apart from QEMU's
        own chatter.

        Decoding is lenient: serial bytes can be garbage before the firmware has
        configured the port, and losing the whole stream to one bad byte would be
        worse than showing a replacement character.
        """
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            if line.startswith("SERIAL "):
                self.add("serial", line[len("SERIAL "):])
            else:
                self.add(default_source, line)
