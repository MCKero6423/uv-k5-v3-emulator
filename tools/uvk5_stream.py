#!/usr/bin/env python3
"""Background frame pump for the web UI.

One thread reads the LCD at a fixed rate into a shared buffer, and every HTTP
client serves from that buffer. Previously each /stream iteration did its own QMP
reads, so load scaled with the number of clients and a slow reader could stall the
grab loop.

Encoding happens only when the framebuffer bytes actually change -- the LCD is
static most of the time, so idle CPU stays near zero. `generation` lets a client
tell "no new frame" from "same frame again" without comparing bytes itself.

`rebind` exists for power cycling: the emulator can come and go under the server,
and rebinding to None blanks the screen rather than leaving a stale frame that
looks live.
"""
import threading
import time

from uvk5_lcd import FrameGrabber, encode_png, unpack


class FramePump:
    def __init__(self, client, frame_addr: int, status_addr: int,
                 fps: int = 15, scale: int = 4, spool_dir: str = "/dev/shm"):
        self._frame_addr = frame_addr
        self._status_addr = status_addr
        self._spool_dir = spool_dir
        self._interval = 1.0 / fps
        self._scale = scale
        self._lock = threading.Lock()
        self._grabber = (FrameGrabber(client, frame_addr, status_addr, spool_dir)
                         if client is not None else None)
        self._png = None
        self._raw = None
        self._generation = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    def rebind(self, client):
        """Point at a new QMP client, or None when the emulator is off."""
        with self._lock:
            self._grabber = (
                FrameGrabber(client, self._frame_addr, self._status_addr,
                             self._spool_dir)
                if client is not None else None)
            if client is None:
                self._png = None        # dark screen, not a stale frame
                self._raw = None
            self._generation += 1

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                grabber = self._grabber
            if grabber is not None:
                try:
                    status, frame = grabber.raw()
                    current = (status, frame)
                    with self._lock:
                        # Re-check: a rebind may have landed mid-read, and its
                        # blanking must not be undone by this stale frame.
                        if self._grabber is grabber and current != self._raw:
                            self._raw = current
                            self._png = encode_png(unpack(status, frame),
                                                   self._scale)
                            self._generation += 1
                except Exception:
                    # A dead emulator must not kill the pump: power may come
                    # back, and latest() keeps serving the last good frame.
                    pass
            slack = self._interval - (time.monotonic() - started)
            if slack > 0:
                self._stop.wait(slack)

    def latest(self):
        with self._lock:
            return self._png

    def generation(self) -> int:
        with self._lock:
            return self._generation
