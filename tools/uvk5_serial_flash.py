#!/usr/bin/env python3
"""Program a UV-K5 over its serial bootloader protocol.

Not the same protocol as the EEPROM read/write commands (0x0514/0x051B). This one is
the firmware-update flow, and its message set and framing come from the firmware's own
host tool (`tools/serialtool` in armel/uv-k1-k5v3-firmware-custom, MIT licensed):

    0x0518  device -> host   UID and bootloader version, announced repeatedly
    0x0530  host -> device   the bootloader version we expect (handshake)
    0x0519  host -> device   timestamp, page index, page count, then up to 256 bytes
    0x051A  device -> host   timestamp, page index, error code

Framing is `AB CD | len | payload | crc | DC BA` with the payload XOR-ed by a fixed
16-byte table. The CRC is only checked on the host side: the device does not send a
useful one, which is why the reference client ignores it.

The transport here is a socket, because that is what the emulator offers through
`-serial tcp:host:port`. A real radio needs a serial port; pass one as *endpoint* and
pyserial is used instead (`pip install pyserial`).
"""
import argparse
import socket
import struct
import sys
import time

MSG_NOTIFY_DEV_INFO = 0x0518
MSG_NOTIFY_BL_VER = 0x0530
MSG_PROG_FW = 0x0519
MSG_PROG_FW_RESP = 0x051A

PAGE_SIZE = 256
MAGIC = b"\xab\xcd"
END = b"\xdc\xba"
# The obfuscation table, copied from tools/serialtool/msg.py.
OBFUS = bytes([0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
               0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80])


def obfuscate(data: bytes) -> bytes:
    return bytes(b ^ OBFUS[i % len(OBFUS)] for i, b in enumerate(data))


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build(msg_type: int, data: bytes = b"") -> bytes:
    """One frame: header, payload, CRC, footer, obfuscated in one pass.

    The length field counts the *message* -- type, length and data -- and excludes the
    CRC. A device announcement reads `ab cd 24 00` for a 36-byte message (a 32-byte
    UID+version body), which is what pins this down. Counting the CRC as well makes
    every frame two bytes too long, and the device then drops all of them without a
    word: the handshake is ignored and no page is ever acknowledged.
    """
    payload = struct.pack("<HH", msg_type, len(data)) + data
    body = payload + struct.pack("<H", crc16_xmodem(payload))
    return MAGIC + struct.pack("<H", len(payload)) + obfuscate(body) + END


class Frames:
    """Reassembles frames from a byte stream and de-obfuscates them."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        self._buf.extend(chunk)

    def take(self):
        """The next (type, data) pair, or None if one is not complete yet."""
        while True:
            start = self._buf.find(MAGIC)
            if start < 0:
                del self._buf[:]           # nothing usable
                return None
            if len(self._buf) < start + 8:
                del self._buf[:start]
                return None
            length = struct.unpack_from("<H", self._buf, start + 2)[0]
            end = start + 6 + length
            if len(self._buf) < end + 2:
                del self._buf[:start]
                return None
            if bytes(self._buf[end:end + 2]) != END:
                del self._buf[:start + 2]
                continue
            body = obfuscate(bytes(self._buf[start + 4:end]))
            del self._buf[:end + 2]
            if len(body) < 4:
                continue
            msg_type, data_len = struct.unpack_from("<HH", body, 0)
            return msg_type, body[4:4 + data_len]


def open_transport(endpoint: str, timeout: float = 10.0):
    """A socket to host:port, or a serial port for anything else."""
    host, _, port = endpoint.rpartition(":")
    if host and port.isdigit():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((host, int(port)), timeout=2.0)
                sock.settimeout(0.5)
                return sock
            except OSError:
                time.sleep(0.2)
        raise SystemExit("no connection to %s" % endpoint)
    import serial                                  # pyserial, only for a real radio
    port_obj = serial.Serial(endpoint, 38400, timeout=0.5)
    return port_obj


class Flasher:
    def __init__(self, transport, log=print):
        self._t = transport
        self._frames = Frames()
        self._log = log

    def _pump(self, seconds=1.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                chunk = self._t.recv(4096) if isinstance(self._t, socket.socket) \
                        else self._t.read(4096)
            except (socket.timeout, OSError):
                chunk = b""
            if chunk:
                self._frames.feed(chunk)

    def _send(self, msg_type: int, data: bytes = b""):
        self._t.sendall(build(msg_type, data)) if isinstance(self._t, socket.socket) \
            else self._t.write(build(msg_type, data))

    def wait_for_device(self, timeout=20.0):
        """Wait for 0x0518, which the bootloader sends about every 200 ms."""
        self._log("waiting for the device announcement (0x0518) ...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump(0.5)
            while True:
                msg = self._frames.take()
                if msg is None:
                    break
                msg_type, data = msg
                if msg_type == MSG_NOTIFY_DEV_INFO:
                    uid = data[:16].hex()
                    bl = data[16:32].split(b"\x00")[0].decode("ascii", "replace")
                    self._log("device: uid %s, bootloader %r" % (uid, bl))
                    return bl
        raise SystemExit("no 0x0518 announcement; is the radio in flashing mode?")

    def handshake(self, bl_ver: str, times: int = 3):
        """0x0530 in reply to an announcement, three times, like the reference client.

        The reply has to go out immediately: the reference client sits in a blocking
        read and answers each announcement the moment it lands. Batching (read for
        400 ms, then send) leaves the reply ~hundreds of announcements late, and the
        bootloader just keeps announcing -- which looks exactly like a device that
        never received anything.
        """
        want = bl_ver[:4].encode("ascii").ljust(4, b"\x00")
        sent = 0
        deadline = time.monotonic() + 10.0
        while sent < times and time.monotonic() < deadline:
            self._pump(0.05)
            announced = False
            while True:
                msg = self._frames.take()
                if msg is None:
                    break
                if msg[0] == MSG_NOTIFY_DEV_INFO:
                    announced = True
            if announced:
                self._send(MSG_NOTIFY_BL_VER, want)
                sent += 1
        self._log("handshake sent %d time(s) (expecting %r)" % (sent, bl_ver[:4]))

    def program(self, image: bytes, pages=None, retries: int = 3):
        """Write *image* page by page. Returns the number of pages written."""
        total = (len(image) + PAGE_SIZE - 1) // PAGE_SIZE
        if pages is not None:
            total = min(total, pages)
        stamp = int(time.time() * 100) & 0xFFFFFFFF
        written = 0
        # Layout copied from the reference client: timestamp, page index, page count,
        # then FOUR reserved bytes before the payload -- the data starts at offset 16
        # of the message, not 12. Getting that wrong is silent: the device simply
        # never acknowledges the page.
        for index in range(total):
            page = image[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
            data = struct.pack("<IHH", stamp, index, total) + b"\x00" * 4 + page
            for attempt in range(retries):
                self._send(MSG_PROG_FW, data)
                self._pump(1.0)
                reply = None
                while True:
                    msg = self._frames.take()
                    if msg is None:
                        break
                    if msg[0] == MSG_PROG_FW_RESP:
                        reply = msg[1]
                if reply is None:
                    continue
                _stamp, page_index, err = struct.unpack_from("<IHH", reply, 0)
                if err == 0 and page_index == index:
                    written += 1
                    break
            else:
                raise SystemExit("page %d never acknowledged" % index)
            if (index + 1) % 32 == 0 or index + 1 == total:
                self._log("  programmed %d / %d pages" % (index + 1, total))
        return written


def main() -> int:
    ap = argparse.ArgumentParser(description="program a UV-K5 over its serial bootloader")
    ap.add_argument("--endpoint", default="127.0.0.1:4568",
                    help="host:port of a socket chardev, or a serial port name")
    ap.add_argument("--image", required=True, help="firmware image to program")
    ap.add_argument("--pages", type=int, default=None,
                    help="program only the first N pages (for testing)")
    args = ap.parse_args()

    with open(args.image, "rb") as fh:
        image = fh.read()

    transport = open_transport(args.endpoint)
    flasher = Flasher(transport)
    bl_ver = flasher.wait_for_device()
    flasher.handshake(bl_ver)
    written = flasher.program(image, pages=args.pages)
    print("programmed %d page(s) from %s" % (written, args.image))
    return 0


if __name__ == "__main__":
    sys.exit(main())
