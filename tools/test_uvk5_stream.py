#!/usr/bin/env python3
"""Unit tests for the background frame pump. No emulator needed."""
import tempfile
import threading
import time
import unittest

from uvk5_lcd import LCD_HEIGHT, LCD_WIDTH, STATUS_BYTES, TOTAL_ROWS, unpack
from uvk5_stream import FramePump


class CountingClient:
    """Counts memsave calls, so QMP load can be asserted."""

    def __init__(self):
        self.reads = 0
        self.lock = threading.Lock()

    def command(self, name, **args):
        if name == "pmemsave":
            raise AssertionError("pmemsave reads physical addresses; use memsave")
        if name != "memsave":
            raise AssertionError(f"unexpected {name}")
        with self.lock:
            self.reads += 1
        with open(args["filename"], "wb") as fh:
            fh.write(bytes(args["size"]))
        return {}


def wait_for_frame(pump, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pump.latest() is not None:
            return True
        time.sleep(0.01)
    return False


class TestFramePump(unittest.TestCase):
    def _pump(self, client=None, fps=50):
        pump = FramePump(client if client is not None else CountingClient(),
                         0x1000, 0x2000, fps=fps, spool_dir=tempfile.mkdtemp())
        self.addCleanup(pump.stop)
        return pump

    def test_latest_returns_a_png_once_started(self):
        pump = self._pump()
        pump.start()
        self.assertTrue(wait_for_frame(pump), "no frame produced")
        self.assertTrue(pump.latest().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_latest_is_none_before_start(self):
        pump = self._pump()
        self.assertIsNone(pump.latest())

    def test_read_rate_is_independent_of_reader_count(self):
        """QMP load must not scale with clients: that is the whole point."""
        client = CountingClient()
        pump = self._pump(client, fps=20)
        pump.start()
        self.assertTrue(wait_for_frame(pump))

        time.sleep(0.5)
        with client.lock:
            first = client.reads
        # Hammer latest() the way many readers would. It must cause no reads.
        for _ in range(2000):
            pump.latest()
        with client.lock:
            after_hammer = client.reads
        self.assertLess(after_hammer - first, 30,
                        "serving frames must not trigger fresh QMP reads")

    def test_stop_halts_reading(self):
        client = CountingClient()
        pump = self._pump(client)
        pump.start()
        self.assertTrue(wait_for_frame(pump))
        pump.stop()
        with client.lock:
            settled = client.reads
        time.sleep(0.3)
        with client.lock:
            self.assertEqual(client.reads, settled)

    def test_generation_does_not_advance_on_an_unchanged_frame(self):
        """Encoding only on change is what keeps idle CPU near zero."""
        pump = self._pump()
        pump.start()
        self.assertTrue(wait_for_frame(pump))
        time.sleep(0.2)
        gen = pump.generation()
        time.sleep(0.3)
        # The stub always returns the same zero frame, so nothing changed.
        self.assertEqual(pump.generation(), gen)

    def test_start_is_idempotent(self):
        pump = self._pump()
        pump.start()
        pump.start()
        self.assertTrue(wait_for_frame(pump))

    def test_survives_a_client_that_raises(self):
        """A dead emulator must not kill the pump; power may come back."""
        class Broken:
            def command(self, name, **args):
                raise RuntimeError("emulator gone")

        pump = self._pump(Broken())
        pump.start()
        time.sleep(0.2)
        self.assertIsNone(pump.latest())      # nothing to show, but still alive
        pump.stop()

    def test_rebind_to_none_blanks_the_screen(self):
        """Power off must go dark, not keep showing a stale frame."""
        pump = self._pump()
        pump.start()
        self.assertTrue(wait_for_frame(pump))
        pump.rebind(None)
        self.assertIsNone(pump.latest())

    def test_rebind_to_a_client_resumes(self):
        pump = self._pump()
        pump.start()
        self.assertTrue(wait_for_frame(pump))
        pump.rebind(None)
        self.assertIsNone(pump.latest())
        pump.rebind(CountingClient())
        self.assertTrue(wait_for_frame(pump), "did not resume after rebind")


class FakePanel:
    """A grabber whose panel works, and whose guest RAM must never be touched.

    read() raises, so any test that silently takes the fallback fails loudly instead of
    comparing two wrong pictures -- which is how the bug below stayed invisible: the
    fake client used by the tests above answers memsave and nothing else, so the panel
    branch failed there too, the bare except swallowed it, and every one of them was
    really exercising the fallback.
    """

    def __init__(self):
        self.gram = bytes((i * 7) & 0xFF for i in range(TOTAL_ROWS * LCD_WIDTH))

    def panel_gram(self):
        return self.gram

    def panel_pixels(self):
        return unpack(self.gram[:STATUS_BYTES], self.gram[STATUS_BYTES:])

    def raw(self):
        raise AssertionError("guest RAM was read while the panel was available")


class TestFrameSource(unittest.TestCase):
    """Which memory the page draws is not a detail -- it decides whose screen is right.

    The panel is the only firmware-independent source: every build pushes pixels through
    the same controller, while guest RAM is correct only for the build whose buffer
    addresses were passed in. So when the panel is reachable it must be used.

    uvk5_stream.py used STATUS_BYTES without importing it. The panel branch therefore
    raised NameError on every frame, a bare except swallowed it, and every screen the
    page drew came from guest RAM at one firmware's addresses -- right-looking for that
    firmware, plausible and offset for any other. These two tests fail on that code.
    """

    def test_the_panel_is_used_when_it_is_there(self):
        pump = FramePump(None, 0, 0)
        status, frame, pixels = pump._grab(FakePanel())
        self.assertEqual(pump.source()[0], "panel",
                         "the panel is reachable, so guest RAM must not be used")
        self.assertIsNone(pump.source()[1])
        self.assertEqual(len(status), STATUS_BYTES)
        self.assertEqual(len(frame), TOTAL_ROWS * LCD_WIDTH - STATUS_BYTES)
        # unpack() returns one entry per LCD line, not per page: eight pages of eight.
        self.assertEqual(len(pixels), LCD_HEIGHT)
        self.assertEqual(len(pixels[0]), LCD_WIDTH)

    def test_a_broken_panel_falls_back_and_says_why(self):
        notes = []
        pump = FramePump(None, 0, 0, on_fallback=notes.append)
        broken = FakePanel()
        broken.panel_gram = lambda: (_ for _ in ()).throw(RuntimeError("no panel model"))
        broken.raw = lambda: (b"\x00" * STATUS_BYTES,
                              b"\x00" * (TOTAL_ROWS * LCD_WIDTH - STATUS_BYTES))
        pump._grab(broken)
        self.assertEqual(pump.source()[0], "framebuffer")
        self.assertIn("no panel model", pump.source()[1])
        self.assertEqual(len(notes), 1, "the fallback is announced once, with its reason")


if __name__ == "__main__":
    unittest.main()
