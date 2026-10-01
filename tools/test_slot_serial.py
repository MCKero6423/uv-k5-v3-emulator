#!/usr/bin/env python3
"""A host tool can write a firmware slot through the firmware's own commands.

The firmware exposes slot management over its programming port (App/app/uart.c, the
"Firmware Slots" block): 0x0720 reads a header, 0x0722 erases a slot, 0x0724 programs
bytes at an offset, 0x0726 returns the image CRC. tools/uvk5_slots_serial.py speaks
them, and this boots a real instance to check that a slot written that way lands in
the external flash with a header the firmware itself validates.

Four things have to line up, and each failed silently on its own before it did:

- The first 0x0514 is often swallowed, so the handshake is retried.
- A frame carries 8 bytes of framing, 4 of header and 12 of payload before the data,
  and the receive buffer is 256 bytes (App/driver/uart.c: UART_DMA_Buffer[256]), so
  chunks are 200 bytes.
- The serial session times out after ~6 s without a 0x0514
  (gSerialConfigCountDown_500ms = 12), which a transfer of any size crosses, so the
  session is renewed as it goes.
- K5Viewer streams the screen down the same link, so the port has to be drained
  continuously; a client that reads only while waiting for a reply blocks the guest.

Needs a build with ENABLE_FEAT_F4HWN_MULTIBOOT -- the slot commands simply are not
there without it -- so it skips unless one is pointed at with UVK5_MULTIBOOT_IMAGE,
and unless a QEMU is known (UVK5_QEMU, or one on PATH).
"""
import gzip
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import uvk5_slots as slots                                     # noqa: E402
import uvk5_slots_serial as serial                             # noqa: E402

PRISTINE = os.path.join(ROOT, "assets", "pristine", "flash-pristine.img.gz")
BOOT_SECONDS = 20
SLOT = 3
IMAGE = bytes((i * 7 + 3) & 0xFF for i in range(4096))         # small, recognisable


def qemu_path():
    """QEMU, from QEMU (the runner's variable) or UVK5_QEMU, else from PATH."""
    for var in ("QEMU", "UVK5_QEMU"):
        env = os.environ.get(var)
        if env and os.path.exists(env):
            return env
    return shutil.which("qemu-system-arm")


def multiboot_image():
    """Where a build with ENABLE_FEAT_F4HWN_MULTIBOOT might be.

    assets/firmware/ is what tools/fetch_firmware.py fills; work/ is where hand-kept
    images live. Neither is in the repository, so the test skips rather than pretends.
    """
    env = os.environ.get("UVK5_MULTIBOOT_IMAGE")
    if env and os.path.exists(env):
        return env
    for candidate in (os.path.join(ROOT, "assets", "firmware", "f4hwn.fieldops.v6.0.0.bin"),
                      os.path.join(ROOT, "assets", "firmware", "f4hwn.fusion.v6.0.0.bin"),
                      os.path.join(ROOT, "work", "multiboot.bin")):
        if os.path.exists(candidate):
            return candidate
    return None


def free_port():
    """A port nothing is listening on, for QEMU to listen on instead.

    TCP, not a unix socket: a Windows QEMU cannot create one, and the point of this
    test is that it runs wherever the emulator does.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class TestSlotOverSerial(unittest.TestCase):
    def setUp(self):
        self.qemu = qemu_path()
        if not self.qemu:
            self.skipTest("no qemu-system-arm (set UVK5_QEMU)")
        self.firmware = multiboot_image()
        if not self.firmware:
            self.skipTest("no multi-system build: run tools/fetch_firmware.py, or set "
                          "UVK5_MULTIBOOT_IMAGE. The slot commands do not exist "
                          "without ENABLE_FEAT_F4HWN_MULTIBOOT")
        if not os.path.exists(PRISTINE):
            self.skipTest("assets/pristine/flash-pristine.img.gz is missing")

        self.tmp = tempfile.mkdtemp(prefix="uvk5-slotserial-")
        self.log = open(os.path.join(self.tmp, "qemu.log"), "w+b")
        self.image = os.path.join(self.tmp, "flash.img")
        with gzip.open(PRISTINE, "rb") as src, open(self.image, "wb") as dst:
            shutil.copyfileobj(src, dst)

        port = free_port()
        env = dict(os.environ)
        env["UVK5_FLASH_IMAGE"] = self.image
        self.proc = subprocess.Popen(
            [self.qemu, "-M", "uv-k5-v3", "-nographic", "-monitor", "none",
             "-serial", "tcp:127.0.0.1:%d,server=on,wait=off" % port,
             "-kernel", self.firmware],
            stdout=subprocess.DEVNULL, stderr=self.log, env=env)
        # QEMU listens; we connect, retrying while it comes up.
        deadline = time.monotonic() + BOOT_SECONDS + 40
        self.conn = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                self.conn = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError:
                time.sleep(0.2)
        if self.conn is None:
            # Say why, and keep QEMU's own output: a native Windows QEMU dies at load
            # unless its DLL directory is on PATH, and with stderr discarded that looks
            # exactly like a machine that never listened.
            self.fail("no connection on 127.0.0.1:%d (qemu exit %r)\n%s\n"
                      "%s" % (port, self.proc.poll(), self._qemu_output(),
                               "on Windows the MSYS2 mingw64 bin directory has to be "
                               "on PATH, or QEMU cannot load its DLLs"))
        time.sleep(BOOT_SECONDS)

    def _qemu_output(self):
        try:
            self.log.flush()
            with open(self.log.name, "rb") as fh:
                return fh.read()[-2000:].decode("utf-8", "replace")
        except OSError:
            return "(no QEMU output)"

    def tearDown(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            self.log.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_erased_and_written_slot_validates(self):
        radio = serial.Radio(self.conn, log=lambda *a: None)
        try:
            radio.session()
            self.assertEqual(radio.erase(SLOT), 0, "the firmware refused to erase the slot")
            serial.install(radio, SLOT, IMAGE, "test-image", "1.2.3", log=lambda *a: None)
            status, crc = radio.validate(SLOT)
            self.assertEqual(status, 0)
            self.assertEqual(crc, zlib.crc32(IMAGE) & 0xFFFFFFFF)
        finally:
            radio.close()

    def test_the_slot_lands_in_the_flash_image_on_disk(self):
        radio = serial.Radio(self.conn, log=lambda *a: None)
        try:
            radio.session()
            radio.erase(SLOT)
            serial.install(radio, SLOT, IMAGE, "test-image", "1.2.3", log=lambda *a: None)
        finally:
            radio.close()
        # The model writes the changed range back as it goes, so the file is current
        # without waiting for the emulator to exit.
        with open(self.image, "rb") as fh:
            buf = fh.read()
        header, image = slots.read_slot(buf, SLOT)
        self.assertIsNotNone(header, "slot %d is not committed in the image" % SLOT)
        self.assertEqual(header["name"], "test-image")
        self.assertEqual(header["image_size"], len(IMAGE))
        self.assertTrue(header["crc_ok"], "the header CRC does not describe the image")
        self.assertEqual(image, IMAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
