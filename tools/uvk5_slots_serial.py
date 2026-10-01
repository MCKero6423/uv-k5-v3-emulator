"""Write the firmware's own firmware slots over the serial link.

The multi-system release exposes its slots to a host (App/app/uart.c, "Firmware Slots"):

    0x0720  slot info      Data[0] = slot                 -> 0x0721 Slot, Status, Hdr[64]
    0x0722  slot erase     Data[0] = slot, Data[2..5] = ts -> 0x0723 Slot, Status
    0x0724  slot write     Data[0] = slot, Data[2..5] = offset, Data[6..7] = len,
                           Data[8..11] = ts, Data[12..] = data -> 0x0725 Slot, Status
    0x0726  slot validate  Data[0] = slot                 -> 0x0727 Crc32, Slot, Status

Every one of them is gated on the timestamp the 0x0514 session handshake latched
(UART_Timestamp), exactly like the EEPROM write (CMD_051D): send a different one and the
firmware answers MB_ERR_AUTH without doing anything.

The frames are the AB CD .. DC BA protocol the rest of the programming interface uses, so
uvk5_serial_flash builds them and this only adds the message ids and payload layouts.

Nothing in the emulator's own path needs this: the page writes slots straight into the
flash image. It exists because it is the path a real radio takes, and the only way to
write a slot on hardware.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvk5_serial_flash as proto          # build(), Frames
import uvk5_slots as slots                 # the header layout, in one place

MSG_SLOT_INFO = 0x0720
MSG_SLOT_INFO_ACK = 0x0721
MSG_SLOT_ERASE = 0x0722
MSG_SLOT_ERASE_ACK = 0x0723
MSG_SLOT_WRITE = 0x0724
MSG_SLOT_WRITE_ACK = 0x0725
MSG_SLOT_VALIDATE = 0x0726
MSG_SLOT_VALIDATE_ACK = 0x0727

STATUS = {
    0: "ok",
    1: "no/invalid slot header",
    2: "header format too new",
    3: "image not marked committed",
    4: "image size out of range",
    5: "image CRC mismatch",
    6: "external flash timed out",
    7: "slot index out of range",
    8: "timestamp mismatch",
    9: "restore stub RAM mismatch",
}


CHUNK = 200                     # see Radio.write


class SlotError(RuntimeError):
    pass


class Radio:
    """A connection to the firmware's slot commands."""

    def __init__(self, endpoint, timeout: float = 6.0, log=print):
        """Talk over @endpoint (host:port, or a device path), or an open socket.

        An already-connected socket is accepted because a test that wants QEMU to be
        the one connecting has to listen first, and handing the accepted socket in is
        simpler than racing on a free port.
        """
        self.log = log
        if hasattr(endpoint, "recv"):
            self.sock = endpoint
            self.sock.settimeout(timeout)
        else:
            self.sock = proto.open_transport(endpoint)
        self.sock.settimeout(timeout)
        self.frames = proto.Frames()
        self.timestamp = (int(time.time() * 100) & 0xFFFFFFFF)
        self._lock = threading.Lock()
        self._stop = False
        # Drain the port on a thread of its own. The firmware streams its screen over
        # this same link (K5Viewer), so a client that only reads when it is waiting for
        # a reply backs the socket up and the *guest* then blocks writing to it:
        # measured, a single 64-byte slot write took six seconds that way, and longer
        # transfers lost replies and stalled. Reading continuously is what keeps the
        # radio responsive.
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_loop(self):
        while not self._stop:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, OSError):
                continue
            if not chunk:
                return
            if os.environ.get("UVK5_SLOT_DEBUG"):
                self.log("rx %s" % chunk.hex()[:120])
            with self._lock:
                self.frames.feed(chunk)

    def _pump(self, seconds: float, want=None):
        """Wait for @want, or @seconds, whichever comes first.

        The reading itself happens on the reader thread; this only looks at what it has
        reassembled, so waiting never stops the port being drained.
        """
        end = time.monotonic() + seconds
        got = {}
        while time.monotonic() < end:
            with self._lock:
                while True:
                    msg = self.frames.take()
                    if msg is None:
                        break
                    got[msg[0]] = msg[1]
            if want is not None and want in got:
                return got
            time.sleep(0.002)
        return got

    def session(self):
        """0x0514, which latches our timestamp on the device."""
        # CMD_0514_t: the timestamp is what matters; the rest is padding.
        body = struct.pack("<I", self.timestamp) + b"\x00" * 4
        frame = proto.build(0x0514, body)
        # Retry rather than send once: the first 0x0514 is what makes the firmware enter
        # its serial mode, and how long that takes depends on what the main loop is
        # busy with, so a single handshake either arrives before it is listening or is
        # simply too early. Each retry is cheap and the reply is unambiguous.
        ack = None
        for attempt in range(6):
            if os.environ.get("UVK5_SLOT_DEBUG"):
                self.log("tx [%d] %s" % (attempt, frame.hex()))
            self.sock.sendall(frame)
            replies = self._pump(2.5, want=0x0515)
            ack = replies.get(0x0515)
            if ack is not None:
                break
        if ack is None:
            raise SlotError("no 0x0515 reply after 6 handshakes: is the firmware "
                            "running, and is the serial port the programming one?")
        version = ack[:16].split(b"\x00")[0].decode("ascii", "replace")
        self.log("session timestamp %08x, firmware %s" % (self.timestamp, version))
        return version

    def _command(self, msg_id: int, data: bytes, ack_id: int, wait: float = 4.0,
                 tries: int = 4):
        """Send @data and wait for @ack_id, resending a few times.

        The firmware drops the odd command when it is busy elsewhere -- the first one
        after the handshake most often -- and a dropped command looks exactly like one
        the firmware does not implement. Everything here is idempotent (a repeated erase
        erases, a repeated chunk write writes the same bytes), so resending is safe.
        """
        frame = proto.build(msg_id, data)
        for attempt in range(tries):
            self.sock.sendall(frame)
            replies = self._pump(wait, want=ack_id)
            ack = replies.get(ack_id)
            if ack is not None:
                return ack
            if os.environ.get("UVK5_SLOT_DEBUG"):
                self.log("0x%04x: no reply on attempt %d" % (msg_id, attempt + 1))
            # The firmware leaves its serial mode after ~6 s without a session
            # handshake (gSerialConfigCountDown_500ms = 12), and a slot write is slow
            # enough in host time to cross that: measured, the writes stop dead and the
            # guest spins at one address until a fresh 0x0514 restores it. Handshaking
            # again is what makes the rest of the transfer land.
            if attempt + 1 < tries:
                try:
                    self.session()
                except SlotError:
                    pass
        return None

    def info(self, slot: int):
        """(status, header bytes) for @slot, without a CRC pass."""
        ack = self._command(MSG_SLOT_INFO, bytes([slot]), MSG_SLOT_INFO_ACK)
        if ack is None:
            raise SlotError("0x0720 got no reply")
        return ack[1], ack[2:66]

    def erase(self, slot: int):
        data = bytes([slot, 0]) + struct.pack("<I", self.timestamp)
        ack = self._command(MSG_SLOT_ERASE, data, MSG_SLOT_ERASE_ACK, wait=15.0)
        if ack is None:
            raise SlotError("0x0722 got no reply")
        return ack[1]

    def write(self, slot: int, offset: int, blob: bytes):
        log = getattr(self, "log", None) or (lambda *a: None)
        # 200 bytes of data, not 240: the frame carries 8 bytes of framing, 4 of header
        # and 12 of payload before the data, and the firmware's receive buffer is 256
        # bytes (App/driver/uart.c: UART_DMA_Buffer[256]). A full-size chunk overran it
        # and the firmware silently dropped every one of them.
        began = time.monotonic()
        chunk_no = 0
        for start in range(0, len(blob), CHUNK):
            piece = blob[start:start + CHUNK]
            chunk_no += 1
            if chunk_no > 1 and (chunk_no - 1) % 25 == 0:
                # The firmware leaves its serial mode after ~6 s without a session
                # handshake (gSerialConfigCountDown_500ms = 12), and this transfer runs
                # longer than that. Measured: the writes stop dead at that point and only
                # a fresh 0x0514 revives them, so renew well inside the window.
                self.session()
            data = (bytes([slot, 0]) + struct.pack("<I", offset + start)
                    + struct.pack("<H", len(piece))
                    + struct.pack("<I", self.timestamp) + piece)
            ack = self._command(MSG_SLOT_WRITE, data, MSG_SLOT_WRITE_ACK)
            if ack is None:
                raise SlotError("0x0724 got no reply at offset 0x%x" % (offset + start))
            if ack[1] != 0:
                raise SlotError("0x0724 offset 0x%x: %s"
                                % (offset + start, STATUS.get(ack[1], ack[1])))
            if chunk_no % 100 == 0 or start + CHUNK >= len(blob):
                log("  %6d/%d bytes, %.1fs" % (start + len(piece), len(blob),
                         time.monotonic() - began))

    def validate(self, slot: int):
        ack = self._command(MSG_SLOT_VALIDATE, bytes([slot]), MSG_SLOT_VALIDATE_ACK,
                            wait=20.0)
        if ack is None:
            raise SlotError("0x0726 got no reply")
        crc = struct.unpack_from("<I", ack, 0)[0]
        return ack[5], crc


def install(radio: Radio, slot: int, image: bytes, name: str = "",
            version: str = "", log=print):
    """Erase @slot, write the header and the image, then validate the CRC."""
    header = slots.build_header(image, name, version)
    log("erase slot %d" % slot)
    status = radio.erase(slot)
    if status != 0:
        raise SlotError("erase: %s" % STATUS.get(status, status))
    log("write header (%d bytes) and image (%d bytes)" % (len(header), len(image)))
    radio.write(slot, 0, header)
    radio.write(slot, slots.SLOT_IMAGE_OFFSET, image)
    status, crc = radio.validate(slot)
    if status != 0:
        raise SlotError("validate: %s" % STATUS.get(status, status))
    if crc != (zlib.crc32(image) & 0xFFFFFFFF):
        raise SlotError("device CRC %08x does not match the host's %08x"
                        % (crc, zlib.crc32(image) & 0xFFFFFFFF))
    log("slot %d validates: crc %08x" % (slot, crc))
    return crc


def main(argv=None):
    ap = argparse.ArgumentParser(description="write a firmware slot over the serial link")
    ap.add_argument("--endpoint", required=True,
                    help="host:port or unix socket of the emulator's serial chardev")
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--image", help="firmware .bin to put in the slot (not needed for --info)")
    ap.add_argument("--name", default="", help="name to store in the header")
    ap.add_argument("--version", default="", help="version string to store")
    ap.add_argument("--info", action="store_true", help="only read the slot header")
    args = ap.parse_args(argv)
    if not args.info and not args.image:
        ap.error("--image is required unless --info is given")

    radio = Radio(args.endpoint)
    try:
        radio.session()
        if args.info:
            status, header = radio.info(args.slot)
            print("slot %d: %s" % (args.slot, STATUS.get(status, status)))
            if status == 0:
                print("  header:", header.hex())
            return 0
        with open(args.image, "rb") as fh:
            image = fh.read()
        install(radio, args.slot, image, args.name, args.version)
        return 0
    except SlotError as exc:
        print("failed: %s" % exc, file=sys.stderr)
        return 1
    finally:
        radio.close()


if __name__ == "__main__":
    sys.exit(main())
