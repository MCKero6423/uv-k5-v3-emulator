#!/usr/bin/env python3
"""Web remote control for the UV-K5 emulator.

Serves the LCD as a live stream and maps on-screen buttons to the keypad model,
so the emulated radio can be driven from a browser.

Start the emulator first (tools/run.sh), then:

    python3 tools/webui.py --frame-addr 0x200013DC --status-addr 0x2000175C

Then open http://127.0.0.1:8080/

Two things worth knowing:

  * The QMP socket takes a single client, so tools/key.py cannot talk to the same
    emulator while this server is running.
  * There is no authentication. It binds loopback and anyone who reaches the port
    has full control of the emulated radio. Do not expose it.
"""
import argparse
import json
import time

from flask import Flask, Response, jsonify, request

from uvk5_keys import KEYS, is_valid, normalise
from uvk5_stream import FramePump

KEYPAD_PATH = "/machine/keypad"

# Firmware thresholds, from App/misc.c:
#   key_debounce_10ms     = 2  -> 20 ms to register a press
#   key_repeat_delay_10ms = 40 -> 400 ms counts as HELD, a different event
#
# The browser sends the duration it measured and the server holds the key for
# exactly that long. It must not be reproduced by sending `down` and `up` as two
# requests: over a slow link the round trip between them *becomes* the press
# duration. Measured against this server at 400 ms RTT, an intended tap arrived as
# a 407 ms hold, so every short press was dispatched as a held key and handlers
# like MAIN_Key_MENU did nothing. Jitter either side of the threshold is what made
# it look intermittent rather than simply broken.
TAP_MS = 200

# Below the 20 ms debounce nothing registers at all, so even a very fast click has
# to ask for at least this long.
MIN_HOLD_MS = 60

# A hold longer than this is a stuck key or a typo, not intent.
MAX_HOLD_MS = 5000

BOUNDARY = "uvk5frame"
TARGET_FPS = 15

# Physical layout of the UV-K5 keypad, for the on-screen grid.
KEY_GRID = [
    ["MENU", "UP",   "DOWN", "EXIT"],
    ["1",    "2",    "3",    "STAR"],
    ["4",    "5",    "6",    "0"],
    ["7",    "8",    "9",    "F"],
]
SIDE_KEYS = ["SIDE1", "SIDE2"]

# Keyboard shortcuts -> radio keys.
KEY_BINDINGS = {
    "ArrowUp": "UP", "ArrowDown": "DOWN",
    "Enter": "MENU", "Escape": "EXIT",
    "KeyF": "F", "KeyM": "MENU",
    "BracketLeft": "SIDE1", "BracketRight": "SIDE2",
    "Digit0": "0", "Digit1": "1", "Digit2": "2", "Digit3": "3", "Digit4": "4",
    "Digit5": "5", "Digit6": "6", "Digit7": "7", "Digit8": "8", "Digit9": "9",
}


POWER_ACTIONS = ("on", "off", "reset", "pause", "resume")


def create_app(client, frame_addr: int, status_addr: int, scale: int = 4,
               supervisor=None):
    app = Flask(__name__)

    # One background grabber for every client. client may be None: the emulator
    # can be powered off, and the page still has to load.
    pump = FramePump(client, frame_addr, status_addr, fps=TARGET_FPS, scale=scale)
    pump.start()
    app.config["PUMP"] = pump
    app.config["SUPERVISOR"] = supervisor

    def active_client():
        """The live QMP client, or None when the emulator is off.

        The supervisor is authoritative once present: it replaces the client on
        every power cycle, so the one captured at create_app time goes stale.
        """
        if supervisor is not None:
            return supervisor.client()
        return client

    def set_press(value: str):
        target = active_client()
        if target is None:
            raise LookupError("emulator is off")
        target.command("qom-set", path=KEYPAD_PATH, property="press", value=value)

    @app.get("/")
    def index():
        return Response(render_index(scale), mimetype="text/html")

    @app.get("/api/status")
    def api_status():
        target = active_client()
        if target is None:
            return jsonify(powered=False, status="off")
        try:
            info = target.command("query-status")
        except Exception as exc:
            # The emulator can die under us; that is a state to report, not a 500.
            return jsonify(powered=False, status="unreachable", error=str(exc))
        return jsonify(powered=True, **info)

    @app.post("/api/power/<action>")
    def api_power(action):
        action = (action or "").strip().lower()
        if action not in POWER_ACTIONS:
            return jsonify(error=f"unknown action {action!r}",
                           valid=list(POWER_ACTIONS)), 400
        if supervisor is None:
            return jsonify(
                error="power control needs a supervisor; this server was "
                      "started without one"), 409
        # Refuse to kill a process we did not start. Reset is fine either way,
        # since system_reset does not end anything.
        if action == "off" and not supervisor.owns_process():
            return jsonify(
                error="this server attached to an emulator it did not start, "
                      "so it will not stop it. Restart without --attach to "
                      "manage the process here."), 409

        {"on": supervisor.power_on,
         "off": supervisor.power_off,
         "reset": supervisor.reset,
         "pause": supervisor.pause,
         "resume": supervisor.resume}[action]()

        # Point the pump at whatever client is live now. rebind(None) blanks the
        # screen, so power off actually goes dark instead of freezing on the last
        # frame.
        pump.rebind(supervisor.client())
        return jsonify(ok=True, action=action, powered=supervisor.is_running())

    @app.post("/api/key")
    def api_key():
        body = request.get_json(silent=True) or {}
        key = normalise(body.get("key", ""))
        action = (body.get("action") or "tap").strip().lower()

        if not is_valid(key):
            return jsonify(error=f"unknown key {body.get('key')!r}",
                           valid=list(KEYS)), 400
        if action not in ("down", "up", "tap"):
            return jsonify(error=f"unknown action {action!r}",
                           valid=["down", "up", "tap"]), 400

        hold_raw = body.get("hold_ms")
        if hold_raw is None:
            hold_ms = TAP_MS
        else:
            try:
                hold_ms = int(hold_raw)
            except (TypeError, ValueError):
                return jsonify(
                    error=f"hold_ms must be a number, got {hold_raw!r}"), 400
            if hold_ms < 0:
                return jsonify(error="hold_ms must not be negative"), 400
            hold_ms = min(hold_ms, MAX_HOLD_MS)

        try:
            if action == "down":
                set_press(key)
            elif action == "up":
                set_press("")
            else:
                # Hold here, locally. See the note on TAP_MS: doing this as two
                # requests puts the network round trip inside the press duration.
                set_press(key)
                time.sleep(hold_ms / 1000)
                set_press("")
        except LookupError:
            return jsonify(error="emulator is off; press On first"), 409
        return jsonify(ok=True, key=key, action=action, hold_ms=hold_ms)

    @app.post("/api/release-all")
    def api_release_all():
        """Safety valve: an empty press clears every key in the model."""
        try:
            set_press("")
        except LookupError:
            return jsonify(error="emulator is off"), 409
        return jsonify(ok=True)

    def wait_for_frame(timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            png = pump.latest()
            if png is not None:
                return png
            time.sleep(0.02)
        return None

    @app.get("/frame.png")
    def frame_png():
        png = wait_for_frame()
        if png is None:
            return jsonify(error="no frame available; is the emulator on?"), 503
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/stream")
    def stream():
        # limit exists for tests; unset means stream until the client leaves.
        limit = request.args.get("limit", type=int)
        interval = 1.0 / TARGET_FPS

        def frames():
            sent, seen = 0, -1
            while limit is None or sent < limit:
                png = pump.latest()
                generation = pump.generation()
                # Send only when the pump reports a new frame. A slow client
                # therefore falls behind in frames, never in QMP reads.
                if png is not None and (generation != seen or limit is not None):
                    seen = generation
                    yield (b"--" + BOUNDARY.encode() + b"\r\n"
                           b"Content-Type: image/png\r\n"
                           b"Content-Length: " + str(len(png)).encode()
                           + b"\r\n\r\n" + png + b"\r\n")
                    sent += 1
                else:
                    time.sleep(interval)

        return Response(frames(),
                        mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                        headers={"Cache-Control": "no-store",
                                 "X-Accel-Buffering": "no"})

    return app


def render_index(scale: int) -> str:
    grid = "\n".join(
        "<div class='row'>" + "".join(
            f'<button class="key" data-key="{k}">{"*" if k == "STAR" else k}</button>'
            for k in row) + "</div>"
        for row in KEY_GRID
    )
    sides = "".join(
        f'<button class="key side" data-key="{k}">{k}</button>' for k in SIDE_KEYS
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UV-K5 remote</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#15171a; color:#c9d1d9;
         font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .radio {{ display:flex; flex-direction:column; align-items:center; gap:14px;
           padding:20px; background:#1e2126; border:1px solid #2d333b;
           border-radius:14px; }}
  /* pixelated keeps the 128x64 LCD crisp when scaled up */
  #screen {{ display:block; image-rendering:pixelated; background:#c8d6b9;
            border:3px solid #0d1117; border-radius:4px; }}
  .body {{ display:flex; gap:14px; align-items:flex-start; }}
  .sides {{ display:flex; flex-direction:column; gap:8px; }}
  .pad {{ display:flex; flex-direction:column; gap:8px; }}
  .row {{ display:flex; gap:8px; }}
  .key {{ width:62px; height:44px; font:inherit; font-weight:600; color:#c9d1d9;
         background:#2b3138; border:1px solid #3a424b; border-radius:8px;
         cursor:pointer; user-select:none; -webkit-user-select:none;
         touch-action:manipulation; }}
  .key:hover {{ background:#343b44; }}
  .key.active {{ background:#4b8bf5; border-color:#4b8bf5; color:#fff; }}
  .side {{ width:76px; }}
  .hint {{ color:#6e7681; font-size:12px; text-align:center; max-width:430px; }}
  #status {{ font-size:12px; color:#6e7681; }}
</style>
</head><body>
<div class="radio">
  <img id="screen" src="/stream" alt="radio LCD"
       width="{128 * scale}" height="{64 * scale}">
  <div class="body">
    <div class="sides">{sides}</div>
    <div class="pad">{grid}</div>
  </div>
  <div id="status">connecting...</div>
  <p class="hint">How long you hold a key is measured here and sent as a number,
  so a slow link cannot turn a tap into a long press. Over 400 ms the firmware
  treats it as held, which is a different event. Arrows move, Enter is MENU,
  Esc is EXIT, digits map straight through. No PTT button -- the keypad model
  has no PTT line.</p>
</div>
<script>
const BINDINGS = {json.dumps(KEY_BINDINGS)};
const MIN_HOLD_MS = {MIN_HOLD_MS};
const pressedAt = new Map();

async function sendKey(key, holdMs) {{
  try {{
    await fetch('/api/key', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{key: key, hold_ms: Math.round(holdMs)}})
    }});
  }} catch (err) {{
    document.getElementById('status').textContent = 'send failed: ' + err;
  }}
}}

function mark(key, on) {{
  document.querySelectorAll('[data-key="' + key + '"]')
    .forEach(el => el.classList.toggle('active', on));
}}

// One request per key, carrying the duration as a number. Sending 'down' and
// 'up' as two requests would put the network round trip inside the press: at
// 400 ms RTT every tap arrived as a ~407 ms hold, which the firmware dispatches
// as a held key and MAIN_Key_MENU ignores.
function down(key) {{
  if (pressedAt.has(key)) return;
  pressedAt.set(key, performance.now());
  mark(key, true);
}}
function up(key) {{
  const started = pressedAt.get(key);
  if (started === undefined) return;
  pressedAt.delete(key);
  mark(key, false);
  sendKey(key, Math.max(performance.now() - started, MIN_HOLD_MS));
}}

document.querySelectorAll('.key').forEach(btn => {{
  const key = btn.dataset.key;
  btn.addEventListener('pointerdown', ev => {{ ev.preventDefault(); down(key); }});
  btn.addEventListener('pointerup', ev => {{ ev.preventDefault(); up(key); }});
  btn.addEventListener('pointerleave', () => up(key));
  btn.addEventListener('pointercancel', () => up(key));
  btn.addEventListener('contextmenu', ev => ev.preventDefault());
}});

addEventListener('keydown', ev => {{
  const key = BINDINGS[ev.code];
  if (!key || ev.repeat) return;
  ev.preventDefault();
  down(key);
}});
addEventListener('keyup', ev => {{
  const key = BINDINGS[ev.code];
  if (!key) return;
  ev.preventDefault();
  up(key);
}});
// Release on blur, so losing focus mid-press cannot leave a key stuck down.
addEventListener('blur', () => {{
  [...pressedAt.keys()].forEach(up);
  fetch('/api/release-all', {{method: 'POST'}}).catch(() => {{}});
}});

async function poll() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('status').textContent =
      'guest: ' + (s.status || 'unknown');
  }} catch (err) {{
    document.getElementById('status').textContent = 'emulator unreachable';
  }}
}}
poll();
setInterval(poll, 3000);
</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qmp", default="/tmp/uvk5-qmp.sock")
    ap.add_argument("--frame-addr", type=lambda v: int(v, 0), required=True,
                    help="address of gFrameBuffer (moves between builds)")
    ap.add_argument("--status-addr", type=lambda v: int(v, 0), required=True,
                    help="address of gStatusLine")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--scale", type=int, default=4)
    args = ap.parse_args()

    from uvk5_qmp import QmpClient
    client = QmpClient(args.qmp)
    app = create_app(client, args.frame_addr, args.status_addr, args.scale)
    print(f"serving on http://{args.host}:{args.port}/  "
          f"(no authentication; loopback only unless you changed --host)")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
