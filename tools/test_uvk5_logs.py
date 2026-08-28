#!/usr/bin/env python3
"""Unit tests for the log ring buffer. No emulator needed."""
import io
import threading
import unittest

from uvk5_logs import LogBuffer


class TestLogBuffer(unittest.TestCase):
    def test_records_and_returns_entries(self):
        log = LogBuffer(capacity=10)
        log.add("power", "on")
        entries = log.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "power")
        self.assertEqual(entries[0]["text"], "on")
        self.assertIn("time", entries[0])
        self.assertIn("seq", entries[0])

    def test_is_bounded(self):
        """Unbounded growth inside a long-running server is a slow leak."""
        log = LogBuffer(capacity=5)
        for i in range(20):
            log.add("test", f"line {i}")
        entries = log.entries()
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[-1]["text"], "line 19")
        self.assertEqual(entries[0]["text"], "line 15")

    def test_since_returns_only_newer_entries(self):
        log = LogBuffer(capacity=10)
        log.add("a", "first")
        cursor = log.cursor()
        log.add("b", "second")
        fresh = log.entries(since=cursor)
        self.assertEqual([e["text"] for e in fresh], ["second"])

    def test_since_beyond_the_end_returns_nothing(self):
        log = LogBuffer(capacity=10)
        log.add("a", "first")
        self.assertEqual(log.entries(since=log.cursor()), [])

    def test_cursor_survives_eviction(self):
        """A client polling with `since` must not be sent the same line twice
        just because older entries were dropped."""
        log = LogBuffer(capacity=3)
        for i in range(3):
            log.add("t", f"{i}")
        cursor = log.cursor()
        for i in range(3, 6):
            log.add("t", f"{i}")
        fresh = log.entries(since=cursor)
        self.assertEqual([e["text"] for e in fresh], ["3", "4", "5"])

    def test_pump_stream_splits_lines_and_tags_serial(self):
        log = LogBuffer(capacity=20)
        stream = io.BytesIO(b"SERIAL boot ok\nplain qemu message\n")
        log.pump_stream(stream, default_source="qemu")
        got = [(e["source"], e["text"]) for e in log.entries()]
        self.assertIn(("serial", "boot ok"), got)
        self.assertIn(("qemu", "plain qemu message"), got)

    def test_pump_stream_skips_blank_lines(self):
        log = LogBuffer(capacity=20)
        log.pump_stream(io.BytesIO(b"one\n\n\ntwo\n"), default_source="qemu")
        self.assertEqual([e["text"] for e in log.entries()], ["one", "two"])

    def test_pump_stream_survives_undecodable_bytes(self):
        """Serial output can be garbage before the firmware initialises."""
        log = LogBuffer(capacity=20)
        log.pump_stream(io.BytesIO(b"\xff\xfe bad\ngood\n"), default_source="qemu")
        self.assertEqual(len(log.entries()), 2)

    def test_add_is_thread_safe(self):
        log = LogBuffer(capacity=500)

        def worker(n):
            for i in range(100):
                log.add("t", f"{n}-{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(log.entries()), 400)
        # sequence numbers must be unique, or `since` would skip or repeat lines
        seqs = [e["seq"] for e in log.entries()]
        self.assertEqual(len(seqs), len(set(seqs)))


if __name__ == "__main__":
    unittest.main()
