#!/usr/bin/env bash
# A register read must deliver the value the register holds.
#
# This exists because it did not. Reads came back shifted one place left -- 0x1248
# arrived as 0x2490 -- and the bug was invisible for a long time because writes were
# fine and the registers the firmware polls most were legitimately zero. Reading zero
# and getting zero proves nothing.
#
# The probe seeds REG_0C, which the firmware reads about 1700 times per 30 seconds, so
# a sample is guaranteed. The seed 0x1248 has bits in both halves, so a shift in either
# direction is unmistakable rather than plausible. Bit 0 is deliberately clear: with it
# set the firmware enters an acknowledge loop that has no timeout, and this test is
# about alignment, not interrupt semantics.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SIM=$(dirname "$HERE")
SRC="$SIM/qemu/py32f071.c"

# Same rules as the python tests (tools/uvk5_testenv.py): QEMU, GDB and ELF come from the
# environment, then PATH, then the checkout -- and a missing one is a SKIP with a reason
# rather than a failure. These were the author's home paths, which is why this was the one
# test that could not run anywhere else.
QEMU=${QEMU:-$(command -v qemu-system-arm || true)}
GDB=${GDB:-$(command -v gdb-multiarch || command -v arm-none-eabi-gdb || true)}
QEMU_SRC=${QEMU_SRC:-$SIM/../qemu-7.2}
QSRC="$QEMU_SRC/hw/arm/py32f071.c"
if [ -z "${ELF:-}" ]; then
    for cand in "$SIM"/assets/firmware/*.elf "$SIM"/work/*.elf; do
        [ -e "$cand" ] && ELF=$cand
    done
fi
for t in "$QEMU" "$GDB" "${ELF:-}"; do
    [ -n "$t" ] && [ -e "$t" ] || {
        echo "SKIP  missing ${t:-a prerequisite} (set QEMU, GDB and ELF; see the README Quick start)"
        exit 0
    }
done

# QMP travels over a unix socket here and a Windows QEMU cannot create one: say so rather
# than letting the launch fail and calling it a firmware problem.
if ! python3 -c "import socket; socket.socket(socket.AF_UNIX)" 2>/dev/null; then
    echo "SKIP  no unix sockets on this platform, so -qmp unix: is unavailable"
    exit 0
fi

SEED=0x1248
PORT=1259
SOCK=/tmp/bk-readback.sock
IMG=/tmp/bk-readback.img

cp "$SRC" /tmp/bk-readback-orig.c
trap 'cp /tmp/bk-readback-orig.c "$SRC"; [ -d "$QEMU_SRC/build" ] && cp "$SRC" "$QSRC" 2>/dev/null; rm -f "$IMG" "$SOCK" "$GDBFILE" 2>/dev/null' EXIT

python3 - "$SRC" "$SEED" <<'PY'
import sys
src, seed = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
needle = "    s->regs[BK4819_REG_NOISE] = 0x0010;"
if needle not in s:
    sys.exit("seed point not found; has bk4819_seed_measurements changed?")
open(src, "w", encoding="utf-8").write(s.replace(needle, f"{needle}\n    s->regs[0x0C] = {seed};", 1))
PY

if [ -d "$QEMU_SRC/build" ]; then
    cp "$SRC" "$QSRC"
fi
if [ -d "$QEMU_SRC/build" ] && (cd "$QEMU_SRC/build" && ninja qemu-system-arm 2>&1 \
        | grep -qE 'FAILED|error:'); then
    echo "FAIL  build error"
    exit 1
fi

gzip -dc "$SIM/assets/pristine/flash-pristine.img.gz" > "$IMG"
rm -f "$SOCK"
timeout 80 "$QEMU" -M "uv-k5-v3,flash-image=$IMG" \
    -nographic -monitor none -qmp "unix:$SOCK,server=on,wait=off" \
    -kernel "$ELF" -gdb tcp::$PORT 2>/dev/null >/dev/null &
QPID=$!
sleep 24

# A command file, not a pile of -ex flags: a `commands` block cannot survive being
# passed that way, and the failure looks exactly like "the firmware never read it".
GDBFILE=$(mktemp)
cat > "$GDBFILE" <<GDB
set confirm off
set pagination off
set height 0
target remote :$PORT
set \$n = 0
break BK4819_ReadRegister
commands
silent
if \$r0 == 0x0c
  set \$n = \$n + 1
  if \$n <= 1
    finish
    printf "GOT 0x%04X\n", \$r0
  end
end
continue
end
continue
GDB

GOT=$(timeout 45 "$GDB" -batch -x "$GDBFILE" "$ELF" 2>/dev/null \
    | grep -oE 'GOT 0x[0-9A-Fa-f]{4}' | head -1)

kill $QPID 2>/dev/null || true
wait $QPID 2>/dev/null || true

echo "  seeded $SEED, firmware received ${GOT:-nothing}"
if [ "$GOT" = "GOT 0x1248" ]; then
    echo "PASS  reads are bit-aligned"
    exit 0
fi
case "$GOT" in
    "GOT 0x2490") echo "FAIL  shifted one place left; the command byte's trailing falling edge is eating bit 15" ;;
    "GOT 0x0924") echo "FAIL  shifted one place right; a data bit is being presented twice" ;;
    "")           echo "FAIL  no sample taken; did the firmware boot?" ;;
    *)            echo "FAIL  unexpected value" ;;
esac
exit 1
