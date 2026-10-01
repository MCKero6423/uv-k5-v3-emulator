#!/usr/bin/env python3
"""Owns the QEMU process, so the web UI can power the emulator on and off.

QMP `quit` stops the emulator but also destroys the socket, so nothing is left to
receive a later "power on". Power control therefore needs something outside the
QMP connection that can spawn the process again -- that is this.

Off then On is a cold boot: the process is replaced and the guest starts from
reset, the same as cutting mains power and restoring it. `system_reset` is the
warm alternative and keeps the process.

`adopt()` covers the other case: the server attached to an emulator someone else
started with run.sh. Then `power_off` must refuse, because we did not start that
process and killing it is not ours to do.
"""
import os
import socket
import subprocess
import threading
import time

DEFAULT_QMP = "/tmp/uvk5-qmp.sock"


def qmp_argument(endpoint: str) -> str:
    """The -qmp argument for an endpoint, unix or TCP.

    Linux gets a unix socket. A Windows build of QEMU cannot create one at all, so
    "host:port" is passed through as a TCP listener instead -- the same shape the
    QMP client and wait_for_socket already accept.
    """
    host, _, port = endpoint.rpartition(":")
    if host and port.isdigit():
        return f"tcp:{host}:{port},server=on,wait=off"
    return f"unix:{endpoint},server=on,wait=off"


class FlashSlot:
    """The external-flash image the emulator boots from.

    Mutable and read at spawn time, like the image slot: the page writes a firmware
    into one of its slots (or swaps the whole image) and powers on, and nobody has to
    re-create the launcher or restart the server.
    """

    def __init__(self, path):
        self.path = path


class BootKey:
    """A key to hold from reset on the next power-on, and for how long.

    Mutable and read at spawn time, like the image slot: the page turns it on for one
    boot and off again. Getting a boot mode by hand otherwise means pausing the VM,
    setting the keypad over QMP and continuing -- which is fine in a script and
    unusable from a browser.
    """

    def __init__(self, name=None, hold_ms=1500):
        self.name = name
        self.hold_ms = hold_ms

    def clear(self):
        self.name = None


def resolve_image(image):
    """(path, app_offset) for whatever was handed to the launcher.

    Accepts a path, an ImageInfo, or an ImageSlot. The slot is what the web UI
    passes, and it is read *here*, at spawn time, so uploading a firmware takes
    effect at the next power-on without restarting the server.

    app_offset is None when nothing is known, in which case the machine's own
    default (an application at 0x2800) is used.
    """
    if hasattr(image, "current"):          # ImageSlot
        image = image.current
    if image is None:
        return None, None
    if isinstance(image, str):
        return image, None
    return image.path, image.app_offset


def default_launcher(qemu: str, flash: str, elf, boot_key=None,
                     qmp_path: str = DEFAULT_QMP, gdb_port: int = 1234,
                     capture_stderr: bool = True):
    """Reproduces the command line in tools/run.sh.

    `elf` may be a path, an ImageInfo, or an ImageSlot -- the last is what the web UI
    passes so an uploaded firmware takes effect at the next power-on.
    """
    def launch():
        path, app_offset = resolve_image(elf)
        if path is None:
            raise RuntimeError("no firmware loaded: upload a .bin first")

        # No app-offset here on purpose: the machine works out the image's shape from
        # the image (see uvk5_sniff_app_offset). Passing it as a -machine property
        # looked equivalent and was not -- through this launcher QEMU rejected the
        # whole machine string with "unsupported machine type", while the identical
        # argv run by hand started fine. One less thing to get wrong.
        machine = "uv-k5-v3"

        # The flash image travels in the environment rather than as a -machine
        # property. Same reasoning as above: -M with properties was rejected by QEMU
        # when this launcher spawned it (and only then), and the environment is a
        # channel that arrives intact. The model reads UVK5_FLASH_IMAGE as a fallback.
        env = dict(os.environ)
        env["UVK5_FLASH_IMAGE"] = getattr(flash, "path", flash)

        # Same channel for the boot key: a -machine property list was rejected by
        # QEMU through this launcher, and the environment arrives intact.
        name = getattr(boot_key, "name", None) if boot_key is not None else None
        if name:
            env["UVK5_BOOT_KEY"] = name
            env["UVK5_BOOT_KEY_MS"] = str(getattr(boot_key, "hold_ms", 1500))
        # A stale socket makes QEMU fail to bind, which looks like "power on did
        # nothing". Clear it first -- but only for a socket file there is one of.
        if ":" not in qmp_path and os.path.exists(qmp_path):
            os.unlink(qmp_path)
        return subprocess.Popen(
            [qemu, "-M", machine,
             "-nographic", "-monitor", "none",
             "-qmp", qmp_argument(qmp_path),
             "-kernel", path, "-gdb", "tcp::%d" % gdb_port],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL)
    return launch


def wait_for_socket(path: str, timeout: float = 15.0) -> bool:
    """Wait until something is actually accepting connections on `path`.

    Existence is not enough. A unix socket file outlives the process that created
    it, so a crashed or killed emulator leaves one behind, and a plain
    os.path.exists() check returns immediately and the connect then fails with
    ECONNREFUSED -- which surfaces as power on returning 500. Probing with a real
    connect distinguishes "listening" from "leftover file".
    """
    # "host:port" is a TCP QMP endpoint, which is all a Windows build of QEMU
    # can offer; there is no socket file to stat, so probe the port directly.
    host, _, port = path.rpartition(":")
    tcp = bool(host) and port.isdigit()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp:
            try:
                socket.create_connection((host, int(port)), timeout=1.0).close()
                return True
            except OSError:
                pass
        elif os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1.0)
                probe.connect(path)
                return True
            except OSError:
                pass                  # stale, or not listening yet
            finally:
                probe.close()
        time.sleep(0.05)
    return False


class Supervisor:
    def __init__(self, launch, connect, log=None):
        self._launch = launch
        self._connect = connect
        self._log = log
        self._lock = threading.Lock()
        self._proc = None
        self._client = None

    def _note(self, text: str):
        """Record a power event, if anyone is collecting them."""
        if self._log is not None:
            self._log.add("power", text)

    def is_running(self) -> bool:
        with self._lock:
            if self._client is None:
                return False
            # A process that exited on its own is not running, whatever we think.
            if self._proc is not None and self._proc.poll() is not None:
                return False
            return True

    def owns_process(self) -> bool:
        """True when we launched it, and may therefore stop it."""
        with self._lock:
            return self._proc is not None

    def client(self):
        with self._lock:
            return self._client

    def process(self):
        with self._lock:
            return self._proc

    def adopt(self, client):
        """Use an emulator we did not start. power_off will refuse to kill it."""
        with self._lock:
            self._client = client
            self._proc = None

    def _start_stderr_pump(self, proc) -> bool:
        """Forward QEMU's stderr to the log, and never stop draining it.

        This has to read the pipe unconditionally, because QEMU blocks on write when it
        fills -- which stops its main loop, and then QMP never answers and the guest looks
        dead. The firmware streams its display down this same pipe (the model tags it
        SERIAL), so it fills with binary that has no line breaks in it, and 64 KB is
        reached in about a second.

        Measured: with the pipe drained the launcher's QEMU accepts QMP in 0.5 s; with it
        left unread, the same command line never answers at all. So: read in fixed-size
        chunks (a readline() on a stream with no newlines hoards it), decode leniently, and
        swallow anything the log throws -- a logging failure must not become a stopped
        drain, which is a deadlock rather than a lost line.
        """
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return False

        def drain():
            while True:
                try:
                    chunk = stream.read(65536)
                except Exception:
                    return
                if not chunk:
                    return
                if self._log is None:
                    continue
                try:
                    text = chunk.decode("utf-8", "replace")
                    for line in text.splitlines():
                        if line:
                            self._log.add("qemu", line[:400])
                except Exception:
                    pass

        threading.Thread(target=drain, daemon=True).start()
        return True


    def power_on(self) -> bool:
        with self._lock:
            # A client object is not proof of a live guest. If the process died
            # behind our back -- crashed, OOM-killed, or caught by someone else's
            # cleanup -- the stale client made this return False forever, so the
            # Power button did nothing until the whole service was restarted.
            # Observed twice for real.
            if self._client is not None and self._proc is not None \
                    and self._proc.poll() is not None:
                self._note(f"emulator exited on its own "
                           f"(status {self._proc.returncode}); restarting")
                self._client = None
                self._proc = None

            if self._client is not None:
                return False
            self._proc = self._launch()
            # Drain QEMU's stderr from the moment it starts, not after a successful
            # connect. The firmware streams its screen down that pipe -- the model tags
            # it SERIAL -- and 64 KB of it fills the pipe while we are still waiting for
            # QMP. QEMU then blocks writing to stderr, its main loop stops, and the
            # connect times out with the guest perfectly healthy. That reads as "the
            # emulator never started", and it is why power-on worked with some firmware
            # and not others: only the ones that stream hard fill the pipe in time.
            draining = self._start_stderr_pump(self._proc)
            try:
                self._client = self._connect()
            except Exception as exc:
                # Do not leave a half-started emulator behind: the process would
                # keep running with no client tracking it, hold the QMP socket, and
                # block the next power on. Clean up and report instead.
                proc, self._proc = self._proc, None
                self._client = None
                if proc is not None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    # Why it died is almost always in its stderr, and until now that
                    # was read only after a *successful* connect -- so a failed power
                    # on reported nothing but "the QMP port never appeared", which is
                    # a symptom. Close the pipe and show what QEMU said.
                    if getattr(proc, "stderr", None) is not None:
                        try:
                            tail = proc.stderr.read(4096).decode("utf-8", "replace")
                        except Exception:
                            tail = ""
                        tail = " ".join(tail.split())
                        if tail:
                            # The end, not the start: a bind failure or an error
                            # exit is the last thing QEMU says.
                            # The *whole* message, not a tail: QEMU reports the real
                            # problem first and then a summary line, so keeping only the
                            # end hid the cause behind "unsupported machine type" -- which
                            # is what a rejected -machine property looks like from below.
                            self._note("qemu said: %s" % tail[:1500])
                # What we actually ran is the first thing anyone asks, and
                # until now it was not recorded anywhere.
                if proc is not None and getattr(proc, "args", None):
                    self._note("command: %s" % " ".join(str(a) for a in proc.args))
                    self._note("argv repr: %r" % (list(proc.args),))
                self._note(f"power on failed: {exc}")
                raise
            proc = self._proc
        self._note("power on")
        # Forward QEMU's own stderr, which run.sh and the tests used to discard.
        # Firmware serial arrives here too, tagged SERIAL by the machine model.
        if not draining:
            self._start_stderr_pump(proc)
        return True

    def power_off(self) -> bool:
        with self._lock:
            client, proc = self._client, self._proc
            self._client, self._proc = None, None
        if client is None:
            return False
        try:
            client.command("quit")
        except Exception:
            # Expected: quit tears the socket down, often before the reply.
            pass
        try:
            client.close()
        except Exception:
            pass
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=10)
            except Exception:
                proc.terminate()
                try:
                    code = proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        self._note("power off" if code in (None, 0)
                   else f"power off (qemu exited with {code})")
        return True

    def reset(self) -> bool:
        """Warm reboot, or a cold boot when the emulator is off."""
        client = self.client()
        if client is None:
            return self.power_on()
        client.command("system_reset")
        self._note("reset")
        return True

    def pause(self) -> bool:
        client = self.client()
        if client is None:
            return False
        client.command("stop")
        return True

    def resume(self) -> bool:
        client = self.client()
        if client is None:
            return False
        client.command("cont")
        return True
