#!/usr/bin/env python3
"""Unit tests for the QEMU supervisor. Uses a fake launcher, not real QEMU."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
