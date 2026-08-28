#!/usr/bin/env python3
"""Unit tests for the QEMU supervisor. Uses a fake launcher, not real QEMU."""
import os
import socket
import unittest
import unittest.mock

from uvk5_supervisor import Supervisor


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._alive = True
        self.stderr = None

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


class FakeClient:
    def __init__(self):
        self.commands = []
        self.closed = False

    def command(self, name, **args):
        self.commands.append(name)
        if name == "query-status":
            return {"status": "running", "running": True}
        return {}

    def close(self):
        self.closed = True


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.procs = []
        self.clients = []

        def launch():
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        def connect():
            client = FakeClient()
            self.clients.append(client)
            return client

        self.sup = Supervisor(launch=launch, connect=connect)

    def test_starts_powered_off_when_nothing_is_running(self):
        """The user presses On; the server does not boot it for them."""
        self.assertFalse(self.sup.is_running())
        self.assertIsNone(self.sup.client())
        self.assertEqual(self.procs, [])

    def test_power_on_launches_and_connects(self):
        self.assertTrue(self.sup.power_on())
        self.assertTrue(self.sup.is_running())
        self.assertEqual(len(self.procs), 1)
        self.assertIsNotNone(self.sup.client())

    def test_power_on_twice_does_not_launch_twice(self):
        self.sup.power_on()
        self.assertFalse(self.sup.power_on())
        self.assertEqual(len(self.procs), 1)

    def test_power_off_quits_via_qmp_then_stops_the_process(self):
        self.sup.power_on()
        client = self.sup.client()
        self.assertTrue(self.sup.power_off())
        self.assertIn("quit", client.commands)
        self.assertTrue(client.closed)
        self.assertFalse(self.sup.is_running())
        self.assertIsNone(self.sup.client())

    def test_power_cycle_boots_a_fresh_process(self):
        """Off then On is a cold boot, like mains power cut and restored."""
        self.sup.power_on()
        self.sup.power_off()
        self.sup.power_on()
        self.assertEqual(len(self.procs), 2)
        self.assertTrue(self.sup.is_running())

    def test_reset_uses_system_reset_when_running(self):
        self.sup.power_on()
        client = self.sup.client()
        self.sup.reset()
        self.assertIn("system_reset", client.commands)
        self.assertEqual(len(self.procs), 1, "reset must not respawn QEMU")

    def test_reset_powers_on_when_stopped(self):
        self.sup.reset()
        self.assertTrue(self.sup.is_running())
        self.assertEqual(len(self.procs), 1)

    def test_pause_and_resume(self):
        self.sup.power_on()
        client = self.sup.client()
        self.assertTrue(self.sup.pause())
        self.assertTrue(self.sup.resume())
        self.assertIn("stop", client.commands)
        self.assertIn("cont", client.commands)

    def test_pause_when_off_is_refused(self):
        self.assertFalse(self.sup.pause())
        self.assertFalse(self.sup.resume())

    def test_power_off_when_already_off_is_harmless(self):
        self.assertFalse(self.sup.power_off())
        self.assertFalse(self.sup.is_running())

    def test_power_off_survives_a_quit_that_raises(self):
        """The socket usually drops mid-quit; that is success, not failure."""
        class Rude(FakeClient):
            def command(self, name, **args):
                if name == "quit":
                    raise RuntimeError("connection reset")
                return super().command(name, **args)

        sup = Supervisor(launch=lambda: FakeProc(), connect=Rude)
        sup.power_on()
        self.assertTrue(sup.power_off())
        self.assertFalse(sup.is_running())

    def test_dead_process_is_reported_as_not_running(self):
        self.sup.power_on()
        self.procs[0]._alive = False       # QEMU crashed on its own
        self.assertFalse(self.sup.is_running())

    def test_adopting_an_external_emulator_does_not_own_the_process(self):
        """Attaching to a run.sh instance must not let power_off kill it."""
        client = FakeClient()
        sup = Supervisor(launch=lambda: self.fail("must not launch"),
                         connect=lambda: client)
        sup.adopt(client)
        self.assertTrue(sup.is_running())
        self.assertFalse(sup.owns_process())

    def test_owns_process_is_true_after_power_on(self):
        self.sup.power_on()
        self.assertTrue(self.sup.owns_process())


class TestSupervisorLogging(unittest.TestCase):
    def setUp(self):
        from uvk5_logs import LogBuffer
        self.log = LogBuffer(capacity=50)
        self.procs = []

        def launch():
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        self.sup = Supervisor(launch=launch, connect=FakeClient, log=self.log)

    def texts(self):
        return [e["text"].lower() for e in self.log.entries()]

    def test_power_on_is_logged(self):
        self.sup.power_on()
        self.assertTrue(any("power on" in t for t in self.texts()), self.texts())

    def test_power_off_is_logged(self):
        self.sup.power_on()
        self.sup.power_off()
        self.assertTrue(any("power off" in t for t in self.texts()), self.texts())

    def test_reset_is_logged(self):
        self.sup.power_on()
        self.sup.reset()
        self.assertTrue(any("reset" in t for t in self.texts()), self.texts())

    def test_events_are_tagged_as_power(self):
        self.sup.power_on()
        self.assertTrue(any(e["source"] == "power"
                            for e in self.log.entries()))

    def test_works_without_a_log(self):
        """The log is optional; a supervisor with none must not crash."""
        sup = Supervisor(launch=lambda: FakeProc(), connect=FakeClient)
        sup.power_on()
        sup.power_off()


class TestWaitForSocket(unittest.TestCase):
    """A leftover socket file must not be mistaken for a listening emulator.

    Hit for real: a killed QEMU left /tmp/uvk5-qmp.sock behind, wait_for_socket
    returned immediately because the path existed, and the connect then failed with
    ECONNREFUSED -- which the user saw as power on returning HTTP 500.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "qmp.sock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_returns_false_for_a_stale_socket_file(self):
        from uvk5_supervisor import wait_for_socket
        # A socket file with nothing listening: bind then close.
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.close()
        self.assertTrue(os.path.exists(self.path), "need a leftover file")
        self.assertFalse(wait_for_socket(self.path, timeout=0.5))

    def test_returns_true_when_something_is_listening(self):
        from uvk5_supervisor import wait_for_socket
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(1)
        self.addCleanup(srv.close)
        self.assertTrue(wait_for_socket(self.path, timeout=2))

    def test_returns_false_when_the_path_never_appears(self):
        from uvk5_supervisor import wait_for_socket
        self.assertFalse(wait_for_socket(self.path + ".missing", timeout=0.3))


class TestRecoversFromAnExternalKill(unittest.TestCase):
    """Power on must work again after the emulator dies behind the supervisor's back.

    Hit for real, twice: a stray cleanup killed the QEMU that webui.py owned. The
    supervisor kept its QMP client object, so is_running() reported a broken pipe and
    power_on() returned False immediately without relaunching -- the Power button was
    dead until the whole service was restarted.
    """

    def _supervisor(self, clients):
        launched = []

        def launch():
            launched.append(1)
            proc = unittest.mock.MagicMock()
            proc.poll.return_value = None
            proc.stderr = None
            return proc

        def connect():
            return clients.pop(0)

        sup = Supervisor(launch, connect)
        return sup, launched

    def test_power_on_relaunches_when_the_client_is_dead(self):
        dead = unittest.mock.MagicMock()
        dead.command.side_effect = BrokenPipeError("dead")
        fresh = unittest.mock.MagicMock()
        fresh.command.return_value = {"status": "running"}

        sup, launched = self._supervisor([dead, fresh])
        self.assertTrue(sup.power_on())
        self.assertEqual(len(launched), 1)

        # An external kill: the process is gone, so poll() reports an exit status.
        sup._proc.poll.return_value = -15
        self.assertFalse(sup.is_running())

        # Power on must notice the client is unusable and start a new emulator.
        self.assertTrue(sup.power_on(), "power_on refused to relaunch a dead guest")
        self.assertEqual(len(launched), 2, "no new emulator was launched")
        self.assertTrue(sup.is_running())

    def test_power_on_still_refuses_when_genuinely_running(self):
        live = unittest.mock.MagicMock()
        live.command.return_value = {"status": "running"}
        sup, launched = self._supervisor([live])
        self.assertTrue(sup.power_on())
        self.assertFalse(sup.power_on(), "started a second emulator over a live one")
        self.assertEqual(len(launched), 1)


if __name__ == "__main__":
    unittest.main()
