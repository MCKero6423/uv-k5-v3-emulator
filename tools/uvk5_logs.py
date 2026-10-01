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

# Bytes that are safe to show as text. Everything else in a serial line means the
# wire is carrying binary, not output meant to be read.
_TEXT_BYTES = frozenset(range(0x20, 0x7f)) | {0x09}


def describe_line(raw: bytes) -> str:
    """A line of serial, or a summary when the bytes are not text.

    Serial carries two very different things over one wire: the firmware's readable
    output, and the CPS programming protocol, which is binary. Decoding the second
    as text filled the pane with control characters and buried the first, so a line
    that is mostly non-printable becomes its size plus a hex prefix instead.
    """
    body = raw.rstrip(b"\r\n")
    if not body:
        return ""
    unprintable = sum(1 for b in body if b not in _TEXT_BYTES)
    if unprintable * 4 <= len(body):
        return body.decode("utf-8", "replace")
    head = body[:24].hex(" ")
    tail = "" if len(body) <= 24 else f" … +{len(body) - 24} bytes"
    return f"<binary {len(body)} bytes> {head}{tail}"


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
        worse than showing it. A line that is mostly binary is summarised rather
        than decoded -- see describe_line().
        """
        for raw in iter(stream.readline, b""):
            # Strip the model's tag before looking at the bytes: on a binary line the
            # hex summary would otherwise hide the prefix and the line would lose its
            # "serial" attribution.
            source = default_source
            if raw.startswith(b"SERIAL "):
                source = "serial"
                raw = raw[len(b"SERIAL "):]
            line = describe_line(raw)
            if line:
                self.add(source, line)
