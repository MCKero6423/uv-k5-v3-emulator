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

    def test_does_not_send_separate_down_and_up_for_taps(self):
        """Two requests per key double the latency and break at 400 ms RTT."""
        self.assertNotIn("send(key, 'down')", self.body)
        self.assertNotIn("send(key, 'up')", self.body)

    def test_injects_the_minimum_hold(self):
        """The floor is shared with the server so both agree on it."""
        self.assertIn(f"MIN_HOLD_MS = {webui.MIN_HOLD_MS}", self.body)


class TestStreamUsesPump(unittest.TestCase):
    """Serving frames must not cost QMP reads: the pump owns the grabbing."""

    def _app(self):
        client = StubClient()
        app = webui.create_app(client, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        return client, app, app.test_client()

    def _wait(self, app, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if app.config["PUMP"].latest() is not None:
                return True
            time.sleep(0.02)
        return False

    def test_exposes_the_pump(self):
        _, app, _ = self._app()
        self.assertIn("PUMP", app.config)

    def test_serving_frames_costs_no_qmp_reads(self):
        client, app, http = self._app()
        self.assertTrue(self._wait(app), "pump produced no frame")

        before = len([n for n, _ in client.sent if n == "memsave"])
        http.get("/stream?limit=1")
        http.get("/frame.png")
        after = len([n for n, _ in client.sent if n == "memsave"])
        # The pump keeps grabbing in the background, so allow a little drift;
        # what must not happen is a read per request.
        self.assertLess(after - before, 8,
                        "serving a frame triggered fresh QMP reads")

    def test_frame_png_still_returns_a_png(self):
        _, app, http = self._app()
        self.assertTrue(self._wait(app))
        resp = http.get("/frame.png")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_data().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_frame_png_returns_503_when_there_is_no_frame(self):
        """No frame is a state, not a crash: the emulator may be powered off."""
        app = webui.create_app(None, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        self.assertEqual(app.test_client().get("/frame.png").status_code, 503)


class FakeSupervisor:
    """Minimal supervisor double: records calls, tracks powered state."""

    def __init__(self, client=None, owns=True):
        self._client = client
        self._owns = owns
        self.calls = []

    def is_running(self):
        return self._client is not None

    def owns_process(self):
        return self._owns

    def client(self):
        return self._client

    def power_on(self):
        self.calls.append("power_on")
        if self._client is None:
            self._client = StubClient()
        return True

    def power_off(self):
        self.calls.append("power_off")
        self._client = None
        return True

    def reset(self):
        self.calls.append("reset")
        if self._client is not None:
            self._client.command("system_reset")
        return True

    def pause(self):
        self.calls.append("pause")
        if self._client is None:
            return False
        self._client.command("stop")
        return True

    def resume(self):
        self.calls.append("resume")
        if self._client is None:
            return False
        self._client.command("cont")
        return True


def make_supervised(owns=True):
    client = StubClient()
    sup = FakeSupervisor(client, owns=owns)
    app = webui.create_app(client, frame_addr=0x1000, status_addr=0x2000,
                           supervisor=sup)
    app.config.update(TESTING=True)
    return client, sup, app.test_client()


class TestPowerEndpoints(unittest.TestCase):
    def test_reset_issues_system_reset(self):
        client, sup, http = make_supervised()
        resp = http.post("/api/power/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reset", sup.calls)
        self.assertIn("system_reset", [n for n, _ in client.sent])

    def test_pause_and_resume(self):
        client, sup, http = make_supervised()
        self.assertEqual(http.post("/api/power/pause").status_code, 200)
        self.assertEqual(http.post("/api/power/resume").status_code, 200)
        names = [n for n, _ in client.sent]
        self.assertIn("stop", names)
        self.assertIn("cont", names)

    def test_off_then_on(self):
        _, sup, http = make_supervised()
        self.assertEqual(http.post("/api/power/off").status_code, 200)
        self.assertFalse(http.get("/api/status").get_json()["powered"])
        self.assertEqual(http.post("/api/power/on").status_code, 200)
        self.assertTrue(http.get("/api/status").get_json()["powered"])
        self.assertEqual(sup.calls, ["power_off", "power_on"])

    def test_unknown_action_is_rejected(self):
        _, sup, http = make_supervised()
        resp = http.post("/api/power/explode")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(sup.calls, [])

    def test_off_is_refused_for_an_adopted_emulator(self):
        """We did not start that process, so killing it is not ours to do."""
        _, sup, http = make_supervised(owns=False)
        resp = http.post("/api/power/off")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(sup.calls, [])

    def test_reset_is_allowed_for_an_adopted_emulator(self):
        """system_reset does not kill anything, so it is fine either way."""
        _, sup, http = make_supervised(owns=False)
        self.assertEqual(http.post("/api/power/reset").status_code, 200)
        self.assertIn("reset", sup.calls)

    def test_power_without_a_supervisor_is_409_not_500(self):
        _, http = make_app()
        resp = http.post("/api/power/on")
        self.assertEqual(resp.status_code, 409)


class TestStatusReportsPower(unittest.TestCase):
    def test_status_includes_powered(self):
        _, http = make_app()
        self.assertIn("powered", http.get("/api/status").get_json())

    def test_status_when_powered_off_does_not_error(self):
        """The page must load with the emulator off, not 500."""
        app = webui.create_app(None, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        http = app.test_client()
        self.assertEqual(http.get("/").status_code, 200)
        resp = http.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["powered"])

    def test_status_reports_unreachable_when_qmp_raises(self):
        class Broken:
            def command(self, name, **args):
                raise RuntimeError("socket gone")

        app = webui.create_app(Broken(), frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        body = app.test_client().get("/api/status").get_json()
        self.assertFalse(body["powered"])
        self.assertIn("unreachable", body["status"])


class TestKeyWhenPoweredOff(unittest.TestCase):
    def test_key_is_refused_with_no_emulator(self):
        """Pressing a key on a dark screen is a 409, not a crash."""
        app = webui.create_app(None, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        resp = app.test_client().post("/api/key", json={"key": "MENU"})
        self.assertEqual(resp.status_code, 409)


class TestPowerBar(unittest.TestCase):
    def setUp(self):
        _, http = make_app()
        self.body = http.get("/").get_data(as_text=True)

    def test_has_power_buttons(self):
        for action in ("on", "off", "reset"):
            self.assertIn(f'data-power="{action}"', self.body)

    def test_power_bar_is_above_the_screen(self):
        """The user asked for it on top."""
        self.assertLess(self.body.index('data-power="on"'),
                        self.body.index('id="screen"'))

    def test_power_off_asks_for_confirmation(self):
        """Off kills the emulator; a stray click should not do that silently."""
        self.assertIn("confirm(", self.body)

    def test_shows_the_powered_state(self):
        self.assertIn("powerstate", self.body)

    def test_dims_the_screen_when_off(self):
        """A dark screen is the signal that the machine is off."""
        self.assertIn("screen-off", self.body)


class TestStartsPoweredOff(unittest.TestCase):
    """The emulator must not be running until the user asks for it."""

    def test_app_with_no_client_serves_a_dark_screen(self):
        app = webui.create_app(None, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        http = app.test_client()

        self.assertEqual(http.get("/").status_code, 200)
        self.assertFalse(http.get("/api/status").get_json()["powered"])
        # No frame yet, and that is a state rather than an error.
        self.assertEqual(http.get("/frame.png").status_code, 503)

    def test_page_offers_an_on_button_while_off(self):
        app = webui.create_app(None, frame_addr=0x1000, status_addr=0x2000)
        app.config.update(TESTING=True)
        body = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('data-power="on"', body)

    def test_main_has_an_attach_flag_not_an_own_flag(self):
        """Owning the process is the default; attaching is the opt-in."""
        import inspect
        src = inspect.getsource(webui.main)
        self.assertIn("--attach", src)
        self.assertNotIn("--own-emulator", src)

    def test_main_does_not_power_on_at_startup(self):
        """Arriving at a dark screen is the point; do not boot it for them."""
        import inspect
        src = inspect.getsource(webui.main)
        # power_on may only appear under the attach branch, never unconditionally.
        self.assertNotIn("supervisor.power_on()", src)


class TestMeasuredSend(unittest.TestCase):
    """The browser measures the real press and sends it once, on release.

    This replaces an optimistic scheme that fired a speculative tap at
    pointerdown and a second held press if the button was still down. That was
    152 ms faster but it *guessed*, and when the guess was wrong the firmware
    received both presses and acted on both: a normal click in the menu moved
    gMenuCursor by 9 and opened the submenu. Measuring is slower and exact, and
    it makes hold-to-repeat work, since the firmware is held for as long as the
    user actually holds.
    """

    def setUp(self):
        _, http = make_app()
        self.body = http.get("/").get_data(as_text=True)

    def test_measures_press_duration_in_the_browser(self):
        self.assertIn("performance.now()", self.body)

    def test_sends_on_release_not_on_press(self):
        down_fn = self.body.split("function down(key)")[1].split("function up(")[0]
        self.assertNotIn("sendKey(", down_fn,
                         "down() must not send: the duration is not known yet")
        up_fn = self.body.split("function up(key)")[1].split("\n}}")[0]
        self.assertIn("sendKey(", up_fn, "up() is where the press is sent")

    def test_sends_the_measured_duration(self):
        self.assertIn("hold_ms", self.body)

    def test_does_not_speculate_with_a_second_press(self):
        """No timer may fire a second press behind the user's back."""
        self.assertNotIn("LONG_PRESS_AFTER_MS", self.body)
        self.assertNotIn("longTimers", self.body)

    def test_enforces_a_minimum_hold(self):
        """A very fast click must still clear the debounce floor."""
        self.assertIn("MIN_HOLD_MS", self.body)
        self.assertGreaterEqual(webui.MIN_HOLD_MS, 30)

    def test_does_not_send_separate_down_and_up_for_taps(self):
        """Two requests would put the round trip inside the press duration."""
        self.assertNotIn("send(key, 'down')", self.body)
        self.assertNotIn("send(key, 'up')", self.body)


class TestLogsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client, self.http = make_app()

    def test_logs_endpoint_returns_entries(self):
        resp = self.http.get("/api/logs")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("entries", body)
        self.assertIn("cursor", body)

    def test_logs_accept_a_since_cursor(self):
        resp = self.http.get("/api/logs?since=0")
        self.assertEqual(resp.status_code, 200)

    def test_logs_exposes_the_buffer(self):
        _, sup, http = make_supervised()
        app_log = http.application.config.get("LOG")
        self.assertIsNotNone(app_log)


class TestLogPane(unittest.TestCase):
    def setUp(self):
        _, http = make_app()
        self.body = http.get("/").get_data(as_text=True)

    def test_page_has_a_log_pane(self):
        self.assertIn('id="logpane"', self.body)
        self.assertIn("/api/logs", self.body)

    def test_pane_has_a_fixed_height(self):
        """The container must not grow with content."""
        self.assertIn("#logtext", self.body)
        self.assertIn("height:", self.body)

    def test_pane_scrolls_rather_than_expanding(self):
        self.assertIn("overflow-y:auto", self.body)

    def test_pane_is_capped_in_line_count(self):
        """Even a scrollable pane needs a cap, or the DOM grows forever."""
        self.assertIn("MAX_LOG_LINES", self.body)

    def test_autoscroll_yields_to_manual_scrolling(self):
        """Scrolling up to read must not be yanked back by the next line."""
        self.assertIn("scrollHeight", self.body)
        self.assertIn("clientHeight", self.body)


class TestKeyLogging(unittest.TestCase):
    """Keys must appear in the log, or the UI cannot be debugged from the browser."""

    def test_key_press_is_logged(self):
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "MENU", "hold_ms": 60})
        texts = [e["text"] for e in log.entries() if e["source"] == "key"]
        self.assertTrue(any("MENU" in t for t in texts), texts)

    def test_log_records_the_hold_duration(self):
        """Distinguishing a tap from a hold is the whole point of the record."""
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "MENU", "hold_ms": 900})
        texts = [e["text"] for e in log.entries() if e["source"] == "key"]
        self.assertTrue(any("900" in t for t in texts), texts)

    def test_log_marks_a_long_press_as_held(self):
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "MENU", "hold_ms": 900})
        texts = [e["text"].lower() for e in log.entries() if e["source"] == "key"]
        self.assertTrue(any("held" in t for t in texts), texts)

    def test_rejected_key_is_logged_too(self):
        """A refused key must be visible, or it looks like nothing happened."""
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "PTT"})
        texts = [e["text"] for e in log.entries() if e["source"] == "key"]
        self.assertTrue(any("PTT" in t for t in texts), texts)


class TestIdleKeepalive(unittest.TestCase):
    """A static screen must still produce frames, just slowly."""

    def test_keepalive_interval_is_defined(self):
        self.assertTrue(hasattr(webui, "IDLE_FRAME_INTERVAL_S"))
        self.assertGreater(webui.IDLE_FRAME_INTERVAL_S, 0)

    def test_keepalive_is_slower_than_the_live_rate(self):
        """Slower on idle, but never stopped."""
        self.assertGreater(webui.IDLE_FRAME_INTERVAL_S, 1.0 / webui.TARGET_FPS)


class TestClientIpInLogs(unittest.TestCase):
    """Entries carry the client IP, so a shared log says who did what."""

    def test_key_entry_records_the_client_ip(self):
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "MENU", "hold_ms": 60},
                  environ_overrides={"REMOTE_ADDR": "172.21.91.137"})
        entries = [e for e in log.entries() if e["source"] == "key"]
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["ip"], "172.21.91.137")

    def test_power_entry_records_the_client_ip(self):
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/power/reset",
                  environ_overrides={"REMOTE_ADDR": "172.21.91.137"})
        entries = [e for e in log.entries() if e["source"] == "power"]
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["ip"], "172.21.91.137")

    def test_ipv6_is_recorded(self):
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "UP", "hold_ms": 60},
                  environ_overrides={"REMOTE_ADDR": "fd3c:3f9b:6424:2::99"})
        entries = [e for e in log.entries() if e["source"] == "key"]
        self.assertEqual(entries[-1]["ip"], "fd3c:3f9b:6424:2::99")

    def test_x_forwarded_for_is_preferred_behind_a_proxy(self):
        """nginx reverse-proxies this, so REMOTE_ADDR is always 127.0.0.1."""
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "UP", "hold_ms": 60},
                  environ_overrides={"REMOTE_ADDR": "127.0.0.1",
                                     "HTTP_X_FORWARDED_FOR": "172.21.91.137"})
        entries = [e for e in log.entries() if e["source"] == "key"]
        self.assertEqual(entries[-1]["ip"], "172.21.91.137")

    def test_only_the_first_hop_of_x_forwarded_for_is_used(self):
        """The rest of the chain is attacker-controlled and must be ignored."""
        client, sup, http = make_supervised()
        log = http.application.config["LOG"]
        http.post("/api/key", json={"key": "UP", "hold_ms": 60},
                  environ_overrides={
                      "REMOTE_ADDR": "127.0.0.1",
                      "HTTP_X_FORWARDED_FOR": "172.21.91.137, 10.0.0.1"})
        entries = [e for e in log.entries() if e["source"] == "key"]
        self.assertEqual(entries[-1]["ip"], "172.21.91.137")

    def test_entries_without_a_request_have_no_ip(self):
        """Firmware serial and qemu output come from no client at all."""
        from uvk5_logs import LogBuffer
        log = LogBuffer()
        log.add("serial", "boot banner")
        self.assertIsNone(log.entries()[-1]["ip"])

    def test_page_renders_the_ip_between_time_and_source(self):
        _, http = make_app()
        body = http.get("/").get_data(as_text=True)
        # e.time + ip + source, in that order
        line = [l for l in body.splitlines() if "e.source" in l and "e.time" in l]
        self.assertTrue(line, "log line template not found")
        tmpl = line[0]
        self.assertLess(tmpl.index("e.time"), tmpl.index("ip"))
        self.assertLess(tmpl.index("ip"), tmpl.index("e.source"))


if __name__ == "__main__":
    unittest.main()
