#!/usr/bin/env python3
"""Unit tests for the web UI. Stubs the QMP client, so no emulator needed."""
import time
import unittest

import webui


class StubClient:
    """Records commands; writes the files memsave would write."""

    def __init__(self):
        self.sent = []

    def command(self, name, **args):
        self.sent.append((name, args))
        if name == "query-status":
            return {"status": "running", "running": True}
        if name == "memsave":
            with open(args["filename"], "wb") as fh:
                fh.write(bytes(args["size"]))
            return {}
        if name == "pmemsave":
            raise AssertionError("pmemsave reads physical addresses; use memsave")
        return {}

    def presses(self):
        """The sequence of values written to the keypad press property."""
        return [a["value"] for n, a in self.sent if n == "qom-set"]


def make_app():
    client = StubClient()
    app = webui.create_app(client, frame_addr=0x1000, status_addr=0x2000)
    app.config.update(TESTING=True)
    return client, app.test_client()


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.client, self.http = make_app()

    def test_status_reports_running(self):
        resp = self.http.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "running")

    def test_index_serves_html_with_the_screen_and_keypad(self):
        resp = self.http.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("/stream", body)
        self.assertIn("MENU", body)


class TestKeyEndpoint(unittest.TestCase):
    def setUp(self):
        self.client, self.http = make_app()

    def test_down_holds_the_key(self):
        resp = self.http.post("/api/key", json={"key": "MENU", "action": "down"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.presses(), ["MENU"])

    def test_up_releases(self):
        self.http.post("/api/key", json={"key": "MENU", "action": "up"})
        self.assertEqual(self.client.presses(), [""])

    def test_tap_holds_then_releases(self):
        resp = self.http.post("/api/key", json={"key": "UP", "action": "tap"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.presses(), ["UP", ""])

    def test_tap_duration_is_a_short_press(self):
        """Must land above the 20 ms debounce and below the 400 ms hold."""
        self.assertGreater(webui.TAP_MS, 20)
        self.assertLess(webui.TAP_MS, 400)

    def test_writes_the_keypad_press_property(self):
        self.http.post("/api/key", json={"key": "MENU", "action": "down"})
        name, args = self.client.sent[-1]
        self.assertEqual(name, "qom-set")
        self.assertEqual(args["path"], "/machine/keypad")
        self.assertEqual(args["property"], "press")

    def test_rejects_unknown_key(self):
        resp = self.http.post("/api/key", json={"key": "PTT", "action": "tap"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.presses(), [])

    def test_rejects_unknown_action(self):
        resp = self.http.post("/api/key", json={"key": "UP", "action": "wiggle"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.presses(), [])

    def test_accepts_lowercase_key(self):
        resp = self.http.post("/api/key", json={"key": "menu", "action": "down"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.presses(), ["MENU"])

    def test_release_all_clears_every_key(self):
        self.http.post("/api/key", json={"key": "MENU", "action": "down"})
        resp = self.http.post("/api/release-all")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.presses()[-1], "")


class TestStream(unittest.TestCase):
    def setUp(self):
        self.client, self.http = make_app()

    def test_stream_is_multipart_and_yields_a_png_part(self):
        resp = self.http.get("/stream?limit=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", resp.headers["Content-Type"])
        body = resp.get_data()
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"\x89PNG\r\n\x1a\n", body)

    def test_single_frame_endpoint_returns_a_png(self):
        resp = self.http.get("/frame.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"], "image/png")
        self.assertTrue(resp.get_data().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_stream_reads_frames_with_memsave(self):
        self.http.get("/stream?limit=1")
        self.assertIn("memsave", [n for n, _ in self.client.sent])


class TestFrontEnd(unittest.TestCase):
    def setUp(self):
        _, http = make_app()
        self.body = http.get("/").get_data(as_text=True)

    def test_every_model_key_has_a_button(self):
        for key in webui.KEYS:
            self.assertIn(f'data-key="{key}"', self.body)

    def test_binds_pointer_and_keyboard_input(self):
        self.assertIn("pointerdown", self.body)
        self.assertIn("keydown", self.body)

    def test_releases_on_blur_so_keys_cannot_stick(self):
        self.assertIn("blur", self.body)

    def test_does_not_offer_ptt(self):
        self.assertNotIn('data-key="PTT"', self.body)


class TestHoldMs(unittest.TestCase):
    """Press duration must be produced by the server, not by request timing.

    At 400 ms RTT the gap between a `down` request and an `up` request is itself
    ~400 ms, which the firmware reads as a held key (key_repeat_delay_10ms = 40).
    Measured against the real server: an intended tap arrived as 407 ms. Holding
    server-side is what makes a short press possible over a slow link.
    """

    def setUp(self):
        self.client, self.http = make_app()

    def test_hold_ms_presses_and_releases(self):
        resp = self.http.post("/api/key", json={"key": "MENU", "hold_ms": 120})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.presses(), ["MENU", ""])

    def test_hold_ms_is_honoured_server_side(self):
        start = time.monotonic()
        self.http.post("/api/key", json={"key": "MENU", "hold_ms": 150})
        elapsed = (time.monotonic() - start) * 1000
        self.assertGreaterEqual(elapsed, 140)
        self.assertLess(elapsed, 400)

    def test_hold_ms_defaults_to_a_short_press(self):
        resp = self.http.post("/api/key", json={"key": "UP"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["hold_ms"], webui.TAP_MS)

    def test_hold_ms_is_clamped(self):
        resp = self.http.post("/api/key", json={"key": "UP", "hold_ms": 99999})
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(resp.get_json()["hold_ms"], webui.MAX_HOLD_MS)

    def test_hold_ms_rejects_nonsense(self):
        resp = self.http.post("/api/key", json={"key": "UP", "hold_ms": "soon"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.client.presses(), [])

    def test_hold_ms_rejects_negative(self):
        resp = self.http.post("/api/key", json={"key": "UP", "hold_ms": -5})
        self.assertEqual(resp.status_code, 400)

    def test_long_hold_is_preserved_not_clamped_to_a_tap(self):
        """A deliberate long press must stay long, or hold events break."""
        resp = self.http.post("/api/key", json={"key": "MENU", "hold_ms": 900})
        self.assertEqual(resp.get_json()["hold_ms"], 900)


class TestFrontEndHoldMs(unittest.TestCase):
    """The page must send one request per key, carrying a measured duration."""

    def setUp(self):
        _, http = make_app()
        self.body = http.get("/").get_data(as_text=True)

    def test_sends_hold_ms(self):
        self.assertIn("hold_ms", self.body)

    def test_measures_press_duration_in_the_browser(self):
        self.assertIn("performance.now()", self.body)

    def test_does_not_send_separate_down_and_up_for_taps(self):
        """Two requests per key double the latency and break at 400 ms RTT."""
        self.assertNotIn("send(key, 'down')", self.body)
        self.assertNotIn("send(key, 'up')", self.body)

    def test_enforces_a_minimum_hold(self):
        """A very fast click still has to clear the 20 ms debounce."""
        self.assertIn("MIN_HOLD_MS", self.body)


if __name__ == "__main__":
    unittest.main()
