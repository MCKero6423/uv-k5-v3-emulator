#!/usr/bin/env python3
"""A typed frequency must take effect and survive a power cycle.

This is the user-visible bug that took four separate model faults to explain:

  1. Page-program did not wrap within its 256-byte page, so a 512-byte burst at
     0x008F00 spilled into the VFO frequency area at 0x009000.
  2. DMA transfers started when a channel was enabled rather than when the
     peripheral requested one, so a read ran before the command had been clocked.
  3. Each DMA channel ran to completion independently, so on a duplex transfer the
     TX side finished before RX ever sampled the bus.
  4. DMA used address_space_memory, which cannot decode this SoC's SRAM at all --
     the container region is handed only to the CPU. Reads returned
     MEMTX_DECODE_ERROR with zeros and writes went nowhere.

Any one of them zeroed the sector that holds per-band frequencies, and
RADIO_ConfigureChannel substitutes the band's lower limit only for 0xFFFFFFFF, so a
stored zero was taken literally and clamped to BX4819_band1_lower -- 18 MHz. Hence
"the frequency will not change" and "it forgets after power off" were one bug.

Drives the emulator over QMP with no debugger attached: the frequency input box
times out in about 2.5 s and a gdb attach takes longer, which silently clears the
box and invalidates the run.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
PRISTINE = os.path.join(ROOT, "assets", "pristine", "flash-pristine.img.gz")

BOOT_SECONDS = 20

# Flash layout, from App/driver/eeprom_compat.c: 14 VFO slots of 16 bytes at 0x9000,
# two slots per band. The frequency is the first word, in units of 10 Hz.
VFO_BASE = 0x9000
BAND_STRIDE = 32
WANT_MHZ = 435.0
WANT_RAW = 43_500_000
WANT_BAND = 5           # 400-470 MHz contains 435


class Qmp:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(path)
        self.buf = b""
        self._read()                      # greeting
        self.cmd("qmp_capabilities")

    def _read(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._read()
            if "return" in reply or "error" in reply:
                return reply

    def press(self, key, hold=0.08):
        self.cmd("qom-set", path="/machine/keypad", property="press", value=key)
        time.sleep(hold)
        self.cmd("qom-set", path="/machine/keypad", property="press", value="")
        time.sleep(0.12)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def boot(image, sock_path):
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    proc = subprocess.Popen(
        [QEMU, "-M", f"uv-k5-v3,flash-image={image}", "-nographic",
         "-monitor", "none", "-qmp", f"unix:{sock_path},server=on,wait=off",
         "-kernel", ELF],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        if os.path.exists(sock_path):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("QMP socket never appeared")
    time.sleep(BOOT_SECONDS)
    return proc


def shutdown(proc, qmp):
    try:
        qmp.cmd("quit")
    except Exception:
        pass
    qmp.close()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def stored_frequency(image, band, vfo=0):
    with open(image, "rb") as fh:
        data = fh.read()
    off = VFO_BASE + band * BAND_STRIDE + vfo * 16
    return int.from_bytes(data[off:off + 4], "little")


def main():
    for path, what in ((QEMU, "QEMU"), (ELF, "firmware"), (PRISTINE, "pristine image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")

    workdir = tempfile.mkdtemp(prefix="uvk5-freq-")
    image = os.path.join(workdir, "flash.img")
    sock_path = os.path.join(workdir, "qmp.sock")
    with gzip.open(PRISTINE, "rb") as src, open(image, "wb") as dst:
        shutil.copyfileobj(src, dst)

    failures = []
    try:
        proc = boot(image, sock_path)
        qmp = Qmp(sock_path)

        qmp.press("EXIT")
        time.sleep(0.8)

        # F+1 leaves memory mode. It only does that while the VFO is on a memory
        # channel; in frequency mode the same shortcut cycles the band instead.
        qmp.press("F")
        time.sleep(0.5)
        qmp.press("1")
        time.sleep(1.5)

        # Six digits back to back. The gap between them must stay well under
        # key_input_timeout_500ms / 3, roughly 2.5 s, or the box clears.
        start = time.time()
        for digit in "435000":
            qmp.press(digit)
        elapsed = time.time() - start
        print(f"typed 435000 in {elapsed:.2f}s")
        if elapsed > 2.0:
            failures.append(f"digits took {elapsed:.2f}s, close to the input timeout")

        time.sleep(5)                     # let the save reach flash
        shutdown(proc, qmp)

        raw = stored_frequency(image, WANT_BAND)
        print(f"stored in band{WANT_BAND}: "
              f"{'blank' if raw == 0xFFFFFFFF else f'{raw / 100000:.5f} MHz'}")
        if raw != WANT_RAW:
            failures.append(
                f"band{WANT_BAND} holds {raw:#x}, expected {WANT_RAW:#x} "
                f"({WANT_MHZ} MHz)")
        else:
            print(f"PASS  {WANT_MHZ} MHz was stored")

        # Other bands must be untouched, which is what a spilling write breaks.
        clobbered = [b for b in range(7)
                     if b != WANT_BAND and stored_frequency(image, b) == 0]
        if clobbered:
            failures.append(
                f"bands {clobbered} were zeroed -- a write spilled across sectors")
        else:
            print("PASS  no other band was zeroed")

        # And it has to still be there after a power cycle.
        proc = boot(image, sock_path)
        qmp = Qmp(sock_path)
        time.sleep(2)
        shutdown(proc, qmp)

        raw_after = stored_frequency(image, WANT_BAND)
        if raw_after != WANT_RAW:
            failures.append(
                f"after a power cycle band{WANT_BAND} holds {raw_after:#x}, "
                f"expected {WANT_RAW:#x}")
        else:
            print(f"PASS  {WANT_MHZ} MHz survived a power cycle")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print()
        for f in failures:
            print(f"FAIL {f}")
        sys.exit(1)
    print("\na typed frequency takes effect and persists")


if __name__ == "__main__":
    main()
