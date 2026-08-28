#!/usr/bin/env python3
"""Firmware serial output must reach the host.

App/main.c sends UART_Version over USART1 right after UART_Init(), and _putchar
routes every printf_ there too. The machine has no USART1 model -- it is one of the
logging catch-all stubs -- so without help those bytes vanish and the firmware
appears to print nothing at all.

Boots a real emulator on a private socket and checks the bytes come out of QEMU's
stderr as SERIAL lines.

Run: python3 tools/test_serial_capture.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)
QEMU = os.path.expanduser("~/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm")
ELF = os.path.expanduser("~/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf")
FLASH = os.path.join(SIM, "assets", "flash.img")
LOG = "/tmp/uvk5-serial-test.log"
QMP = "/tmp/uvk5-serial-test.sock"


def main():
    for path, what in ((QEMU, "QEMU binary"), (ELF, "firmware ELF"),
                       (FLASH, "flash image")):
        if not os.path.exists(path):
            sys.exit(f"missing {what}: {path}")
    if os.path.exists(QMP):
        os.unlink(QMP)

    with open(LOG, "wb") as fh:
        proc = subprocess.Popen(
            [QEMU, "-M", f"uv-k5-v3,flash-image={FLASH}", "-nographic",
             "-monitor", "none", "-qmp", f"unix:{QMP},server=on,wait=off",
             "-kernel", ELF],
            stdout=subprocess.DEVNULL, stderr=fh)
        try:
            time.sleep(14)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    if os.path.exists(QMP):
        os.unlink(QMP)

    text = open(LOG, errors="replace").read()
    lines = [l for l in text.splitlines() if l.startswith("SERIAL")]
    print(f"captured {len(lines)} SERIAL line(s)")
    for line in lines[:10]:
        print("   ", line)

    if not lines:
        print("\nFAIL no SERIAL output; USART1 DR writes are still being dropped")
        print("     (App/main.c:98 sends UART_Version, so something should appear)")
        return 1
    print("\nPASS firmware serial output reaches the host")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
