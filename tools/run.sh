#!/usr/bin/env bash
# Start the emulated radio.
#
#   GDB stub  : tcp:1234  (screenshot.py and where.sh read memory through it)
#   QMP socket: /tmp/uvk5-qmp.sock  (key.sh injects keypresses through it)
#
# Usage: run.sh [firmware.elf]
set -euo pipefail

QEMU="${QEMU:-$(command -v qemu-system-arm || true)}"
[ -n "$QEMU" ] || { echo "SKIP  no qemu-system-arm; set QEMU or put it on PATH" >&2; exit 0; }
ELF="${1:-}"
# shellcheck source=tools/uvk5_elf.sh
. "$(dirname "$0")/uvk5_elf.sh"
ELF=$(uvk5_find_elf) || { echo "SKIP  no firmware found; set ELF or put one in assets/firmware" >&2; exit 0; }
FLASH="${UVK5_FLASH_IMAGE:-$(cd "$(dirname "$0")/.." && pwd)/assets/flash.img}"
QMP=/tmp/uvk5-qmp.sock

# Never `pkill -f 'M uv-k5-v3'` here: see tools/lib_kill_emulator.sh for why that
# takes down an unrelated webui.py along with it.
# shellcheck source=tools/lib_kill_emulator.sh
. "$(dirname "$0")/lib_kill_emulator.sh"
kill_emulators "$QMP"
rm -f "$QMP"
sleep 1

# Headless: the screen is read out of guest memory rather than drawn by QEMU, so
# no display backend is needed.
exec "$QEMU" \
    -M "uv-k5-v3,flash-image=$FLASH" \
    -nographic -monitor none \
    -qmp "unix:$QMP,server=on,wait=off" \
    -kernel "$ELF" \
    -gdb tcp::1234
