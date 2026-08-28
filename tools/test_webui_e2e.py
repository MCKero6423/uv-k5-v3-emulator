#!/usr/bin/env python3
"""End-to-end check: real QEMU, real HTTP, real keypress.

Boots its own emulator on a private QMP socket and HTTP port, so it does not
disturb a run.sh session or fight it for the QMP socket. Then drives the server
over HTTP and asserts the LCD actually changed.

The unit tests stub QMP, so they cannot catch a wrong QMP command -- which is a
real risk here: pmemsave and memsave differ only in whether the address is
treated as physical or virtual, and pmemsave fails by silently returning zeros.
This test is what notices.

Run: python3 tools/test_webui_e2e.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
sys.path.insert(0, HERE)

QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
FLASH = os.path.join(SIM, "assets", "flash.img")
QMP = "/tmp/uvk5-webui-e2e.sock"
HTTP_PORT = 8099
FRAME_ADDR, STATUS_ADDR = 0x200013DC, 0x2000175C

BASE = f"http://127.0.0.1:{HTTP_PORT}"


def post(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b"{}"
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def get_frame():
    with urllib.request.urlopen(BASE + "/frame.png", timeout=5) as r:
        return r.read()


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)


def tap(key, hold=0.20, gap=0.30):
    """A real short press: two edges, duration decided here, not by the server."""
    post("/api/key", {"key": key, "action": "down"})
    time.sleep(hold)
    post("/api/key", {"key": key, "action": "up"})
    time.sleep(gap)


def main():
    for path in (QMP,):
        if os.path.exists(path):
            os.unlink(path)
    for path, what in ((QEMU, "QEMU binary"), (ELF, "firmware ELF"),
                       (FLASH, "flash image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")

    qemu = subprocess.Popen(
        [QEMU, "-M", f"uv-k5-v3,flash-image={FLASH}", "-nographic",
         "-monitor", "none", "-qmp", f"unix:{QMP},server=on,wait=off",
         "-kernel", ELF],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    try:
        for _ in range(150):
            if os.path.exists(QMP):
                break
            if qemu.poll() is not None:
                sys.exit("QEMU exited during startup")
            time.sleep(0.1)
        else:
            sys.exit(f"QMP socket never appeared at {QMP}")

        from uvk5_qmp import QmpClient
        import webui

        client = QmpClient(QMP)
        app = webui.create_app(client, FRAME_ADDR, STATUS_ADDR)
        threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=HTTP_PORT,
                                   threaded=True, use_reloader=False),
            daemon=True).start()

        # Boot is ~5 s; power save engages ~6 s in. Waiting past it also proves
        # keys work once the radio is asleep.
        time.sleep(16)

        # 1. a frame renders, and is not blank
        before = get_frame()
        if not before.startswith(b"\x89PNG\r\n\x1a\n"):
            failures.append("frame.png did not return a PNG")
        else:
            print(f"PASS  frame.png returned {len(before)} bytes of PNG")

        # 2. the screen has real content. A blank screen is exactly how the
        #    pmemsave mistake presented, so assert lit pixels rather than trust
        #    that a PNG came back at all.
        from uvk5_lcd import FrameGrabber, unpack
        status, frame = FrameGrabber(client, FRAME_ADDR, STATUS_ADDR).raw()
        lit = sum(sum(row) for row in unpack(status, frame))
        if lit == 0:
            failures.append("screen is blank: 0 lit pixels, check the QMP "
                            "memory command and the framebuffer addresses")
        else:
            print(f"PASS  screen has content ({lit} lit pixels)")

        # 3. a tap opens the menu, which must visibly change the screen
        tap("MENU")
        time.sleep(0.6)
        after = get_frame()
        if after == before:
            failures.append("screen did not change after a MENU tap")
        else:
            print("PASS  screen changed after a MENU tap")

        # 4. the guest is still running: streaming must not pause it
        st = get_json("/api/status")
        if not st.get("running"):
            failures.append(f"guest not running after streaming: {st}")
        else:
            print("PASS  guest still running after reading frames")

        # 5. an invalid key is rejected, not forwarded to QMP
        try:
            post("/api/key", {"key": "PTT", "action": "tap"})
            failures.append("PTT was accepted; it should be rejected")
        except urllib.error.HTTPError as err:
            if err.code != 400:
                failures.append(f"PTT rejected with {err.code}, expected 400")
            else:
                print("PASS  PTT rejected with 400")

        # 6. the stream really is multipart with a PNG part
        with urllib.request.urlopen(BASE + "/stream?limit=1", timeout=10) as r:
            ctype = r.headers.get("Content-Type", "")
            chunk = r.read(4096)
        if "multipart/x-mixed-replace" not in ctype:
            failures.append(f"stream Content-Type was {ctype!r}")
        elif b"\x89PNG\r\n\x1a\n" not in chunk:
            failures.append("stream did not contain a PNG part")
        else:
            print("PASS  /stream is multipart and carries a PNG")
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            qemu.kill()
        if os.path.exists(QMP):
            os.unlink(QMP)

    print()
    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("all end-to-end checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
