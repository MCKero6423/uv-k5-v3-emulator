#!/usr/bin/env python3
"""Unit tests for the web UI. Stubs the QMP client, so no emulator needed."""
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

    def test_sends_down_and_up_not_just_tap(self):
        """Real press duration must come from the browser.

        The firmware distinguishes a short press from a held key at 400 ms, so
        the front end has to send the two edges separately rather than asking the
        server for a fixed-length tap.
        """
        self.assertIn("send(key, 'down')", self.body)
        self.assertIn("send(key, 'up')", self.body)

    def test_binds_pointer_and_keyboard_input(self):
        self.assertIn("pointerdown", self.body)
        self.assertIn("keydown", self.body)

    def test_releases_on_blur_so_keys_cannot_stick(self):
        self.assertIn("blur", self.body)

    def test_does_not_offer_ptt(self):
        self.assertNotIn('data-key="PTT"', self.body)


if __name__ == "__main__":
    unittest.main()
