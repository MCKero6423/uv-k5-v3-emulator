#!/usr/bin/env bash
# Run the emulator with stderr captured, hold a key, then report what the TRACE
# points saw. Answers three questions in one shot:
#   - does keypad_update_rows fire?           (TRACE keypad row...)
#   - is the row irq non-NULL when it fires?  (irq=0x... vs irq=(nil))
#   - does the GPIO input callback run?       (TRACE gpio... set_input)
set -uo pipefail

QEMU="$HOME/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"
ELF="${1:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
FLASH="$HOME/uvk5-port/sim/assets/flash.img"
LOG=/tmp/uvk5-trace.log
QMP=/tmp/uvk5-qmp.sock

pkill -f 'M uv-k5-v3' 2>/dev/null || true
rm -f "$QMP" "$LOG"
sleep 1

"$QEMU" -M "uv-k5-v3,flash-image=$FLASH" \
        -nographic -monitor none \
        -qmp "unix:$QMP,server=on,wait=off" \
        -kernel "$ELF" -gdb tcp::1234 >"$LOG" 2>&1 &

sleep 12
python3 "$HOME/uvk5-port/sim/tools/key.py" MENU >/dev/null 2>&1 || true
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
