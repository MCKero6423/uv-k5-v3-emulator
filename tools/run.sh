#!/usr/bin/env bash
# Start the emulated radio.
#
#   GDB stub  : tcp:1234  (screenshot.py and where.sh read memory through it)
#   QMP socket: /tmp/uvk5-qmp.sock  (key.sh injects keypresses through it)
#
# Usage: run.sh [firmware.elf]
set -euo pipefail

QEMU="$HOME/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"
ELF="${1:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
FLASH="$HOME/uvk5-port/sim/assets/flash.img"
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
