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
import os
import time

from flask import Flask, Response, jsonify, request

from uvk5_keys import KEYS, is_valid, normalise
from uvk5_logs import LogBuffer
from uvk5_stream import FramePump

KEYPAD_PATH = "/machine/keypad"

# Firmware thresholds, from App/misc.c:
#   key_debounce_10ms     = 2  -> 20 ms to register a press
#   key_repeat_delay_10ms = 40 -> 400 ms counts as HELD, a different event
#
# This is a property of the firmware, not a tunable.
#
# Past this point the firmware also auto-repeats, every key_repeat_10ms = 80 ms.
# So a 500 ms press moving a menu cursor several steps is correct, not a bug: it is
# what a real radio does when you hold the button that long. Measured: 500 ms moved
# the cursor 3 steps, 800 ms moved 7, 1500 ms moved 15 -- all consistent with
# (duration - 400) / 80. Do not try to suppress it in the UI; the fix for an
# accidental repeat is a shorter press, not a filter that hides held events.
FIRMWARE_HELD_MS = 400

# The browser sends the duration it wants and the server holds the key for exactly
# that long. It must not be reproduced by sending `down` and `up` as two requests:
# over a slow link the round trip between them *becomes* the press duration.
# Measured against this server at 400 ms RTT, an intended tap arrived as a 407 ms
# hold, so every short press was dispatched as a held key and handlers like
# MAIN_Key_MENU did nothing. Jitter either side of the threshold is what made it
# look intermittent rather than simply broken.
#
# Default when a request omits hold_ms, i.e. for scripts and curl. The browser
# always sends a measured duration, so this does not apply to normal use. Kept
# short because the request does not return until the hold finishes, making the
# value latency the caller pays directly. See MIN_HOLD_MS for why 60 ms.
TAP_MS = 60

# A hold longer than this is a stuck key or a typo, not intent.
MAX_HOLD_MS = 5000

# Floor for a measured press. A very fast click can measure under the debounce
# window, where the firmware would not register it at all.
#
# Measured, and the sample size mattered: at 12 trials per value, 20 ms registered
# only 5/12 while 30 ms was 12/12. The nominal 20 ms debounce is not enough on its
# own because KEYBOARD_Poll samples each column 8 times wanting 2 matching reads.
# 60 ms is double the proven floor. An earlier 4-trial sweep called 30 ms reliable
# and would have shipped a flaky value.
MIN_HOLD_MS = 60

# Lines kept in the browser's log pane. The pane is a fixed-height scroll box, so
# older lines move up out of view; this caps the DOM behind it, which would
# otherwise grow all session even though only a screenful is visible.
MAX_LOG_LINES = 500

BOUNDARY = "uvk5frame"
TARGET_FPS = 15

# Resend the current frame at least this often even when nothing changed.
#
# Sending only on change saves bandwidth but makes a static screen
# indistinguishable from a dead connection, and a client that joined mid-idle
# would sit blank until something moved. Slow rather than stopped.
IDLE_FRAME_INTERVAL_S = 2.0

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
               supervisor=None, log=None):
    app = Flask(__name__)

    if log is None:
        log = LogBuffer()
    app.config["LOG"] = log

    # One background grabber for every client. client may be None: the emulator
    # can be powered off, and the page still has to load.
    pump = FramePump(client, frame_addr, status_addr, fps=TARGET_FPS, scale=scale)
    pump.start()
    app.config["PUMP"] = pump
    app.config["SUPERVISOR"] = supervisor

    def client_ip():
        """The address of whoever made this request.

        Behind the nginx reverse proxy REMOTE_ADDR is always 127.0.0.1, so the
        first hop of X-Forwarded-For is what identifies the real client. Only the
        first entry is trusted: the rest of the chain can be set by the caller.
        """
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return request.remote_addr

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

    def set_ptt(held: bool):
        """Hold or release PTT.

        Separate from set_press because PTT is not a matrix key: the firmware reads
        PB10 directly, so the model exposes it as its own boolean rather than a name
        in the key table.
        """
        target = active_client()
        if target is None:
            raise LookupError("emulator is off")
        target.command("qom-set", path=KEYPAD_PATH, property="ptt", value=held)

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

    @app.get("/api/logs")
    def api_logs():
        since = request.args.get("since", type=int, default=0)
        return jsonify(entries=log.entries(since=since), cursor=log.cursor())

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

        # Attribute the action here: the supervisor has no request context, and on
        # a shared log "who powered it off" is the useful part.
        log.add("power", f"{action} requested", ip=client_ip())

        try:
            {"on": supervisor.power_on,
             "off": supervisor.power_off,
             "reset": supervisor.reset,
             "pause": supervisor.pause,
             "resume": supervisor.resume}[action]()
        except Exception as exc:
            # Starting the emulator can genuinely fail -- a stale QMP socket, a
            # missing binary, a port already taken. Report it as a failed action
            # rather than a 500 with a traceback the browser cannot show.
            log.add("power", f"{action} failed: {exc}", ip=client_ip())
            pump.rebind(supervisor.client())
            return jsonify(error=f"{action} failed: {exc}",
                           powered=supervisor.is_running()), 503

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
            # Log refusals too: a silently dropped key is indistinguishable from
            # a dead button in the browser.
            log.add("key", f"{body.get('key')!r} rejected: not a key on this model",
                    ip=client_ip())
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
                log.add("key", f"{key} down", ip=client_ip())
                set_press(key)
            elif action == "up":
                log.add("key", f"{key} up", ip=client_ip())
                set_press("")
            else:
                # Label by what the FIRMWARE will conclude, so the log says what
                # the radio saw. That boundary is 400 ms (key_repeat_delay_10ms),
                # not the UI's hold threshold -- conflating the two is what caused
                # the tap+held double send in the first place.
                kind = "held" if hold_ms >= FIRMWARE_HELD_MS else "tap"
                log.add("key", f"{key} {kind} {hold_ms}ms", ip=client_ip())
                # Hold here, locally. See the note on TAP_MS: doing this as two
                # requests puts the network round trip inside the press duration.
                set_press(key)
                time.sleep(hold_ms / 1000)
                set_press("")
        except LookupError:
            log.add("key", f"{key} ignored: emulator is off", ip=client_ip())
            return jsonify(error="emulator is off; press On first"), 409
        return jsonify(ok=True, key=key, action=action, hold_ms=hold_ms)

    @app.post("/api/ptt")
    def api_ptt():
        """Hold or release PTT.

        Explicit down/up rather than a timed tap: transmitting is a state the operator
        chooses to stay in, and a fixed duration would be wrong for it. The trade-off
        is that a client which never sends the release leaves the radio keyed, so
        /api/release-all clears this too.
        """
        body = request.get_json(silent=True) or {}
        held = body.get("held")
        if not isinstance(held, bool):
            return jsonify(error="held must be true or false"), 400
        try:
            set_ptt(held)
        except LookupError:
            log.add("key", "PTT ignored: emulator is off", ip=client_ip())
            return jsonify(error="emulator is off; press On first"), 409
        log.add("key", f"PTT {'down' if held else 'up'}", ip=client_ip())
        return jsonify(ok=True, ptt=held)

    @app.post("/api/release-all")
    def api_release_all():
        """Safety valve: clears every key in the model, PTT included.

        PTT needs releasing explicitly -- it is not part of the key matrix, so an empty
        press does not touch it, and a client that vanished mid-transmission would
        otherwise leave the radio keyed indefinitely.
        """
        try:
            set_press("")
            set_ptt(False)
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
            sent, seen, last_sent_at = 0, -1, 0.0
            while limit is None or sent < limit:
                png = pump.latest()
                generation = pump.generation()
                now = time.monotonic()
                # Send on change, and otherwise at the idle keepalive rate. Change
                # detection alone leaves a static screen looking like a dead
                # connection, and a client joining mid-idle would stay blank.
                stale = now - last_sent_at >= IDLE_FRAME_INTERVAL_S
                if png is not None and (generation != seen or stale
                                        or limit is not None):
                    seen = generation
                    last_sent_at = now
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
    # PTT is its own button, not a data-key one: it latches on press and releases on
    # let-go rather than sending a measured tap, because transmitting is a state.
    sides += '<button class="key side ptt" id="ptt">PTT</button>'
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
  /* The border lives on .screenwrap so it stays put when the frame is hidden. */
  #screen {{ display:block; image-rendering:pixelated; background:#c8d6b9; }}
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
  /* Red while keyed, so it is obvious the radio is transmitting. */
  .key.ptt.active {{ background:#da3633; border-color:#da3633; }}
  .hint {{ color:#6e7681; font-size:12px; text-align:center; max-width:430px; }}
  #status {{ font-size:12px; color:#6e7681; }}
  .powerbar {{ display:flex; gap:8px; align-items:center; align-self:stretch; }}
  .pwr {{ padding:6px 14px; font:inherit; color:#c9d1d9; background:#2b3138;
         border:1px solid #3a424b; border-radius:6px; cursor:pointer; }}
  .pwr:hover:not(:disabled) {{ background:#343b44; }}
  .pwr:disabled {{ opacity:0.5; cursor:default; }}
  #powerstate {{ font-size:12px; color:#6e7681; margin-left:auto; }}
  #powerstate.on {{ color:#3fb950; }}
  /*
   * Powered off is a dark panel, drawn by the wrapper so the frame itself can be
   * hidden. An earlier attempt put a dark background on the <img> alone, which
   * changed nothing visible: the image kept painting the last frame over it, so
   * Off looked like it had not worked.
   */
  .screenwrap {{ background:#0b0d10; border:3px solid #0d1117; border-radius:4px;
                line-height:0; }}
  .screenwrap.screen-off #screen {{ visibility:hidden; }}
  #logpane {{ align-self:stretch; }}
  #logpane summary {{ font-size:12px; color:#6e7681; cursor:pointer;
                     user-select:none; }}
  /*
   * Fixed height, so the box never grows with content: older lines move up out
   * of view and you scroll back to read them. min-height matches height so a
   * nearly empty pane does not jump around as the first lines arrive.
   */
  #logtext {{ height:180px; min-height:180px; overflow-y:auto; margin:6px 0 0;
             padding:8px; background:#0d1117; border:1px solid #2d333b;
             border-radius:6px; white-space:pre-wrap; word-break:break-all;
             font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
             color:#8b949e; }}
</style>
</head><body>
<div class="radio">
  <div class="powerbar">
    <button class="pwr" data-power="on">On</button>
    <button class="pwr" data-power="off">Off</button>
    <button class="pwr" data-power="reset">Reset</button>
    <span id="powerstate">-</span>
  </div>
  <div class="screenwrap" id="screenwrap">
    <img id="screen" src="/stream" alt="radio LCD"
         width="{128 * scale}" height="{64 * scale}">
  </div>
  <div class="body">
    <div class="sides">{sides}</div>
    <div class="pad">{grid}</div>
  </div>
  <div id="status">connecting...</div>
  <details id="logpane" open>
    <summary>Logs (firmware serial, qemu, power)</summary>
    <pre id="logtext"></pre>
  </details>
  <p class="hint">How long you hold a key is measured here and sent as one
  number, so the firmware sees exactly the press you made. Hold past 400 ms for a
  long press, which the firmware treats as a separate event and which repeats.
  Arrows move, Enter is MENU, Esc is EXIT, digits map straight through. No PTT
  button -- the keypad model has no PTT line.</p>
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

// Measure the real press and send it once, on release.
//
// The duration travels as a number rather than as separate down/up requests: the
// round trip between those would itself exceed the firmware's 400 ms held
// threshold, turning every tap into a hold.
//
// This deliberately waits for pointerup, which costs the click duration in
// latency. An earlier version fired a speculative tap at pointerdown and a second
// held press if the button was still down -- 152 ms faster, but it guessed, and a
// wrong guess sent both presses. The firmware then acted on both: an ordinary
// click in the menu moved gMenuCursor by 9 and opened the submenu. Measuring is
// exact, and hold-to-repeat works because the key is held for as long as the user
// actually holds it.
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

// '.key[data-key]', not '.key': the PTT button shares the styling but carries no
// data-key, and would otherwise register handlers that send the key "undefined".
document.querySelectorAll('.key[data-key]').forEach(btn => {{
  const key = btn.dataset.key;
  btn.addEventListener('pointerdown', ev => {{ ev.preventDefault(); down(key); }});
  btn.addEventListener('pointerup', ev => {{ ev.preventDefault(); up(key); }});
  btn.addEventListener('pointerleave', () => up(key));
  btn.addEventListener('pointercancel', () => up(key));
  btn.addEventListener('contextmenu', ev => ev.preventDefault());
}});

// PTT latches for as long as the button is held, rather than sending a measured
// duration. Transmitting is a state the operator stays in, so there is nothing to
// measure -- and the release matters more than the press: pointerleave and
// pointercancel are wired up so dragging off the button, or the browser stealing the
// pointer, cannot leave the radio keyed.
const pttBtn = document.getElementById('ptt');
let pttHeld = false;
function setPtt(held) {{
  if (held === pttHeld) return;
  pttHeld = held;
  pttBtn.classList.toggle('active', held);
  fetch('/api/ptt', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{held: held}}),
  }}).catch(() => {{}});
}}
pttBtn.addEventListener('pointerdown', ev => {{ ev.preventDefault(); setPtt(true); }});
pttBtn.addEventListener('pointerup', ev => {{ ev.preventDefault(); setPtt(false); }});
pttBtn.addEventListener('pointerleave', () => setPtt(false));
pttBtn.addEventListener('pointercancel', () => setPtt(false));
pttBtn.addEventListener('contextmenu', ev => ev.preventDefault());
// A closing tab must not leave it transmitting.
addEventListener('pagehide', () => {{ if (pttHeld) setPtt(false); }});

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

document.querySelectorAll('.pwr').forEach(btn => {{
  btn.addEventListener('click', async () => {{
    const action = btn.dataset.power;
    // Off ends the guest. A stray click should not do that silently.
    if (action === 'off' &&
        !confirm('Power off the emulator? Guest state is lost.')) {{
      return;
    }}
    document.querySelectorAll('.pwr').forEach(b => b.disabled = true);
    try {{
      const r = await fetch('/api/power/' + action, {{method: 'POST'}});
      if (!r.ok) {{
        const j = await r.json().catch(() => ({{}}));
        document.getElementById('status').textContent =
          'power ' + action + ' refused: ' + (j.error || r.status);
      }}
    }} catch (err) {{
      document.getElementById('status').textContent = 'power failed: ' + err;
    }} finally {{
      document.querySelectorAll('.pwr').forEach(b => b.disabled = false);
      poll();
      // Restart the stream: the old one ends when the emulator goes away.
      const img = document.getElementById('screen');
      img.src = '/stream?t=' + Date.now();
    }}
  }});
}});

function showPower(powered) {{
  const label = document.getElementById('powerstate');
  label.textContent = powered ? 'on' : 'off';
  label.classList.toggle('on', powered);
  document.getElementById('screenwrap')
    .classList.toggle('screen-off', !powered);
}}

async function poll() {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    showPower(!!s.powered);
    document.getElementById('status').textContent =
      s.powered ? ('guest: ' + (s.status || 'unknown'))
                : 'powered off -- press On to boot';
  }} catch (err) {{
    showPower(false);
    document.getElementById('status').textContent = 'server unreachable';
  }}
}}
poll();
setInterval(poll, 3000);

// Cap the DOM as well as the server-side buffer. The pane scrolls, but an
// unbounded <pre> would still grow memory over a long session.
const MAX_LOG_LINES = {MAX_LOG_LINES};
let logCursor = 0;

async function pollLogs() {{
  const pre = document.getElementById('logtext');
  try {{
    const r = await fetch('/api/logs?since=' + logCursor);
    const j = await r.json();
    logCursor = j.cursor;
    if (!j.entries.length) return;

    // Only stick to the bottom if the user is already there. Otherwise a new
    // line would yank the view away from whatever they scrolled up to read.
    const atBottom =
      pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;

    for (const e of j.entries) {{
      // IP sits between the time and the source. The log is shared by everyone
      // who opens the page, so attribution is what makes it readable when two
      // people are pressing keys. Entries with no client behind them -- firmware
      // serial, qemu output -- show a dash.
      const ip = e.ip ? e.ip : '-';
      pre.textContent += e.time + ' ' + ip + ' [' + e.source + '] '
                       + e.text + '\\n';
    }}
    const lines = pre.textContent.split('\\n');
    if (lines.length > MAX_LOG_LINES) {{
      pre.textContent = lines.slice(-MAX_LOG_LINES).join('\\n');
    }}
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }} catch (err) {{
    /* leave the pane as it is; the next poll will catch up */
  }}
}}
pollLogs();
setInterval(pollLogs, 2000);
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
    ap.add_argument("--attach", action="store_true",
                    help="attach to an emulator started elsewhere (run.sh) "
                         "instead of managing one. Off is then refused, since "
                         "this server did not start that process.")
    ap.add_argument("--qemu", default=os.path.expanduser(
        "~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"))
    ap.add_argument("--elf", default=os.path.expanduser(
        "~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf"))
    ap.add_argument("--flash", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "flash.img"))
    ap.add_argument("--gdb-port", type=int, default=1234)
    args = ap.parse_args()

    from uvk5_qmp import QmpClient
    from uvk5_supervisor import Supervisor, default_launcher, wait_for_socket

    def connect():
        if not wait_for_socket(args.qmp, timeout=15):
            raise RuntimeError(f"QMP socket never appeared at {args.qmp}")
        return QmpClient(args.qmp)

    # One buffer shared by the supervisor and the HTTP layer, so power events,
    # QEMU stderr and firmware serial all land in the same place.
    log = LogBuffer()
    supervisor = Supervisor(
        launch=default_launcher(args.qemu, args.flash, args.elf, args.qmp,
                                gdb_port=args.gdb_port),
        connect=connect, log=log)

    if args.attach:
        # Someone else owns the process; adopt it so the screen works, but Off
        # will refuse.
        supervisor.adopt(connect())
    # Otherwise the emulator stays OFF on purpose. The user presses On, so the
    # page behaves like walking up to a machine rather than finding it booted.

    app = create_app(supervisor.client(), args.frame_addr, args.status_addr,
                     args.scale, supervisor=supervisor, log=log)
    print(f"serving on http://{args.host}:{args.port}/")
    print("attached to a running emulator" if args.attach
          else "emulator is OFF; press On in the browser to boot it")
    print("no authentication: anyone who can reach this port controls the radio")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
