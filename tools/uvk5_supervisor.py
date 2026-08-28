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
import subprocess
import threading
import time

DEFAULT_QMP = "/tmp/uvk5-qmp.sock"


def default_launcher(qemu: str, flash: str, elf: str,
                     qmp_path: str = DEFAULT_QMP, gdb_port: int = 1234,
                     capture_stderr: bool = True):
    """Reproduces the command line in tools/run.sh."""
    def launch():
        # A stale socket makes QEMU fail to bind, which looks like "power on did
        # nothing". Clear it first.
        if os.path.exists(qmp_path):
            os.unlink(qmp_path)
        return subprocess.Popen(
            [qemu, "-M", f"uv-k5-v3,flash-image={flash}",
             "-nographic", "-monitor", "none",
             "-qmp", f"unix:{qmp_path},server=on,wait=off",
             "-kernel", elf, "-gdb", f"tcp::{gdb_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL)
    return launch


def wait_for_socket(path: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
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

    def power_on(self) -> bool:
        with self._lock:
            if self._client is not None:
                return False
            self._proc = self._launch()
            self._client = self._connect()
            proc = self._proc
        self._note("power on")
        # Forward QEMU's own stderr, which run.sh and the tests used to discard.
        # Firmware serial arrives here too, tagged SERIAL by the machine model.
        if self._log is not None and getattr(proc, "stderr", None) is not None:
            threading.Thread(
                target=self._log.pump_stream, args=(proc.stderr,),
                kwargs={"default_source": "qemu"}, daemon=True).start()
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
