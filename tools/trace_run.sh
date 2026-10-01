#!/usr/bin/env bash
# Run the emulator with stderr captured, hold a key, then report what the TRACE
# points saw. Answers three questions in one shot:
#   - does keypad_update_rows fire?           (TRACE keypad row...)
#   - is the row irq non-NULL when it fires?  (irq=0x... vs irq=(nil))
#   - does the GPIO input callback run?       (TRACE gpio... set_input)
set -uo pipefail

QEMU="${QEMU:-$(command -v qemu-system-arm || true)}"
[ -n "$QEMU" ] || { echo "SKIP  no qemu-system-arm; set QEMU or put it on PATH" >&2; exit 0; }
ELF="${1:-}"
# shellcheck source=tools/uvk5_elf.sh
. "$(dirname "$0")/uvk5_elf.sh"
ELF=$(uvk5_find_elf) || { echo "SKIP  no firmware found; set ELF or put one in assets/firmware" >&2; exit 0; }
FLASH="${UVK5_FLASH_IMAGE:-$(cd "$(dirname "$0")/.." && pwd)/assets/flash.img}"
LOG=/tmp/uvk5-trace.log
QMP=/tmp/uvk5-qmp.sock

# Never `pkill -f 'M uv-k5-v3'` here: see tools/lib_kill_emulator.sh for why that
# takes down an unrelated webui.py along with it.
# shellcheck source=tools/lib_kill_emulator.sh
. "$(dirname "$0")/lib_kill_emulator.sh"
kill_emulators "$QMP"
rm -f "$QMP" "$LOG"
sleep 1

"$QEMU" -M "uv-k5-v3,flash-image=$FLASH" \
        -nographic -monitor none \
        -qmp "unix:$QMP,server=on,wait=off" \
        -kernel "$ELF" -gdb tcp::1234 >"$LOG" 2>&1 &

sleep 12
python3 "$(dirname "$0")/key.py" MENU >/dev/null 2>&1 || true
sleep 2

echo "== keypad row drives =="
grep 'keypad row' "$LOG" | tail -6 || echo "(none: keypad_update_rows never ran)"
echo
echo "== column notifications =="
echo "count: $(grep -c 'keypad col' "$LOG" || true)"
grep 'keypad col' "$LOG" | tail -3 || true
echo
echo "== GPIO input callback =="
echo "count: $(grep -c 'set_input' "$LOG" || true)"
grep 'set_input' "$LOG" | tail -6 || echo "(none: row lines never reach the port)"
