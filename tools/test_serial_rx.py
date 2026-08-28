#!/usr/bin/env python3
"""The firmware must receive serial bytes and answer a programming command.

Serial receive used to be impossible. USART1 was a register stub with no chardev, so
nothing could be sent in; and App/driver/uart.c locates incoming data with

    write_ptr = sizeof(UART_DMA_Buffer) - LL_DMA_GetDataLength(DMA1, CHANNEL_2)

over a circular DMA channel, while the DMA model only ever serviced SPI and never
decremented CNDTR for USART. That expression was therefore always 0 and the buffer
always looked empty, which made the whole UV-K5 programming protocol in App/app/uart.c
unreachable: 0x0514 handshake, 0x051B/0x051D EEPROM read and write, 0x05DD reset.

This sends a real 0x0514 handshake and checks for a reply.

Wire format, from App/app/uart.c:
    AB CD <len:16 LE> <payload> <crc:16 LE> DC BA
The payload is obfuscated with a fixed XOR key, and the CRC is CRC-16/XMODEM over the
payload. Replies use the same framing.
"""
import gzip
import os
import shutil
import socket
import struct
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

# App/app/uart.c: Obfuscation[] applied to the payload of every frame.
XOR_KEY = bytes([
    0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
    0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80,
])


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def obfuscate(payload: bytes) -> bytes:
    return bytes(b ^ XOR_KEY[i % len(XOR_KEY)] for i, b in enumerate(payload))


def build_frame(payload: bytes) -> bytes:
    body = payload + struct.pack("<H", crc16_xmodem(payload))
    return b"\xab\xcd" + struct.pack("<H", len(payload)) + obfuscate(body) + b"\xdc\xba"


def parse_frames(buf: bytes):
    """Yield deobfuscated payloads found in buf."""
    out = []
    i = 0
    while True:
        start = buf.find(b"\xab\xcd", i)
        if start < 0 or start + 4 > len(buf):
            break
        length = struct.unpack_from("<H", buf, start + 2)[0]
        end = start + 4 + length + 2
        if end + 2 > len(buf) or length > 512:
            i = start + 2
            continue
        body = obfuscate(buf[start + 4:end])
        out.append(body[:length])
        i = end + 2
    return out


def main():
    for path, what in ((QEMU, "QEMU"), (ELF, "firmware"), (PRISTINE, "pristine image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")

    workdir = tempfile.mkdtemp(prefix="uvk5-serial-")
    image = os.path.join(workdir, "flash.img")
    sock_path = os.path.join(workdir, "serial.sock")
    with gzip.open(PRISTINE, "rb") as src, open(image, "wb") as dst:
        shutil.copyfileobj(src, dst)

    # A listening socket for QEMU's serial chardev to connect back to.
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(40)

    proc = subprocess.Popen(
        [QEMU, "-M", f"uv-k5-v3,flash-image={image}", "-nographic", "-monitor", "none",
         "-serial", f"unix:{sock_path}", "-kernel", ELF],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    try:
        conn, _ = srv.accept()
        conn.settimeout(15)
        print("serial connected")
        time.sleep(BOOT_SECONDS)

        # Drain the boot banner the firmware transmits, so it is not mistaken for a
        # reply. Transmit already worked; this test is about the other direction.
        conn.setblocking(False)
        banner = b""
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                banner += chunk
            except BlockingIOError:
                time.sleep(0.1)
        print(f"boot output: {len(banner)} bytes"
              f"{' (' + banner[:40].decode(errors='replace').strip() + ')' if banner else ''}")
        conn.setblocking(True)

        # 0x0514: hello. Payload is the command id plus a 4-byte timestamp.
        payload = struct.pack("<HH", 0x0514, 4) + struct.pack("<I", 0x12345678)
        conn.sendall(build_frame(payload))
        print("sent 0x0514 hello")

        conn.settimeout(12)
        reply = b""
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            reply += chunk
            if b"\xdc\xba" in reply:
                break

        if not reply:
            failures.append("no reply at all -- the firmware never saw the command")
        else:
            print(f"got {len(reply)} bytes back: {reply[:24].hex()}")
            frames = parse_frames(reply)
            if not frames:
                failures.append(f"reply was not a valid frame: {reply[:32].hex()}")
            else:
                cmd = struct.unpack_from("<H", frames[0], 0)[0]
                print(f"reply command id: 0x{cmd:04X}")
                # 0x0515 is the hello ack.
                if cmd != 0x0515:
                    failures.append(f"expected 0x0515 ack, got 0x{cmd:04X}")
                else:
                    print("PASS  firmware answered the handshake")

                    # 0x051B: read EEPROM. Ask for 8 bytes at 0x0E70 (VFO indices),
                    # which proves the receive path carries a real request and that
                    # flash contents come back over the wire.
                    req = struct.pack("<HHHBBI", 0x051B, 8, 0x0E70, 8, 0,
                                      0x12345678)
                    conn.sendall(build_frame(req))
                    print("sent 0x051B read of 8 bytes at 0x0E70")

                    data = b""
                    deadline = time.time() + 12
                    while time.time() < deadline:
                        try:
                            chunk = conn.recv(4096)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data += chunk
                        if b"\xdc\xba" in data:
                            break

                    read_frames = parse_frames(data)
                    if not read_frames:
                        failures.append(
                            f"no valid frame for the EEPROM read: {data[:32].hex()}")
                    else:
                        rcmd = struct.unpack_from("<H", read_frames[0], 0)[0]
                        print(f"reply command id: 0x{rcmd:04X}")
                        if rcmd != 0x051C:
                            failures.append(
                                f"expected 0x051C read reply, got 0x{rcmd:04X}")
                        else:
                            body = read_frames[0]
                            print(f"payload: {body.hex()}")
                            print("PASS  firmware served an EEPROM read")

    except socket.timeout:
        failures.append("QEMU never connected its serial port")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        srv.close()
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print()
        for f in failures:
            print(f"FAIL {f}")
        sys.exit(1)
    print("\nserial receive works: the firmware accepts programming commands")


if __name__ == "__main__":
    main()
