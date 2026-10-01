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

# STATUS_BYTES is not decoration: _grab() splits the controller's memory into the
# status line and the frame with it. It was missing from this import for the whole
# life of the panel path, so the panel branch raised NameError on every frame, the
# bare except below swallowed it, and every picture the page ever drew came from the
# guest-RAM fallback instead -- which needs *that build's* buffer addresses. With the
# CN addresses and a CN firmware that looked right; with any other firmware the screen
# was plausible and offset, which is exactly how it was reported.
from uvk5_lcd import STATUS_BYTES, FrameGrabber, default_spool_dir, encode_png, unpack


class FramePump:
    def __init__(self, client, frame_addr: int, status_addr: int,
                 fps: int = 15, scale: int = 4, spool_dir: str = None,
                 on_fallback=None):
        # Called once with the reason the first time a frame has to come from guest RAM.
        # It used to be swallowed whole, which is how a NameError in the panel branch
        # went unnoticed for as long as the fallback kept producing plausible pictures.
        self._on_fallback = on_fallback
        self._frame_addr = frame_addr
        self._status_addr = status_addr
        # Resolved by FrameGrabber: /dev/shm on Linux, the temp directory on
        # Windows, where /dev/shm does not exist.
        self._spool_dir = spool_dir or default_spool_dir()
        self._interval = 1.0 / fps
        self._scale = scale
        self._lock = threading.Lock()
        self._grabber = (FrameGrabber(client, frame_addr, status_addr, spool_dir)
                         if client is not None else None)
        self._png = None
        self._raw = None
        self._generation = 0
        # Which memory the pixels came from. The panel is preferred and is the only
        # source that is firmware-independent; the guest-RAM fallback needs that
        # build's own buffer addresses, so getting it wrong shows up as a picture
        # that is plausible but offset. Reporting it is what turns "the screen looks
        # wrong" into "it came from the fallback, and here is why".
        self._source = None
        self._note = None
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

    def set_buffers(self, frame_addr: int, status_addr: int):
        """Point the guest-RAM fallback at this firmware's buffers.

        The addresses move between builds, so they are discovered from the firmware
        itself (tools/uvk5_buffers.py) rather than hardcoded. Only the fallback uses
        them: the panel path needs none.
        """
        with self._lock:
            self._frame_addr = frame_addr
            self._status_addr = status_addr
            grabber = self._grabber
            if grabber is not None:
                self._grabber = FrameGrabber(grabber._client, frame_addr, status_addr,
                                             self._spool_dir)

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                grabber = self._grabber
            if grabber is not None:
                try:
                    status, frame, pixels = self._grab(grabber)
                    current = (status, frame)
                    with self._lock:
                        # Re-check: a rebind may have landed mid-read, and its
                        # blanking must not be undone by this stale frame.
                        if self._grabber is grabber and current != self._raw:
                            self._raw = current
                            self._png = encode_png(pixels, self._scale)
                            self._generation += 1
                except Exception:
                    # A dead emulator must not kill the pump: power may come
                    # back, and latest() keeps serving the last good frame.
                    pass
                    # A dead emulator must not kill the pump: power may come
                    # back, and latest() keeps serving the last good frame.
                    pass
            slack = self._interval - (time.monotonic() - started)
            if slack > 0:
                self._stop.wait(slack)


    def source(self):
        """(which memory the last frame came from, why the fallback happened)."""
        with self._lock:
            return self._source, self._note

    def _grab(self, grabber):
        """One frame: (status, frame, pixels), from the panel if it is there.

        Every firmware pushes its pixels through the display controller, so the
        controller's memory is the screen no matter where that build keeps its own
        buffers -- and builds sharing an ancestor still differ in their display
        logic, which is why guessing guest addresses does not generalise.

        Guest RAM remains the fallback for an emulator built without the panel
        model, and is what the tests stub.
        """
        try:
            pixels = grabber.panel_pixels()
            gram = grabber.panel_gram()
            self._source = "panel"
            self._note = None
            return gram[:STATUS_BYTES], gram[STATUS_BYTES:], pixels
        except Exception as exc:
            # Remember the first reason rather than overwriting it every frame: the
            # cause does not change while the emulator runs, and the first one is
            # the informative one.
            if self._source != "framebuffer":
                self._note = "%s: %s" % (type(exc).__name__, exc)
                if self._on_fallback is not None:
                    try:
                        self._on_fallback(self._note)
                    except Exception:
                        pass
            self._source = "framebuffer"
            status, frame = grabber.raw()
            return status, frame, unpack(status, frame)
    def latest(self):
        with self._lock:
            return self._png

    def generation(self) -> int:
        with self._lock:
            return self._generation
