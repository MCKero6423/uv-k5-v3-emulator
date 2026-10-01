#!/usr/bin/env bash
# Put this machine into a QEMU 7.2 source tree, then build it.
#
# The same steps README.md documents, in the order that works, made idempotent and
# checked: doing them by hand is where "I could not get it to run" begins, and a
# hand-copied file with no record of what it replaced is what makes the next QEMU
# release a manual job.
#
#   QEMU_SRC=/path/to/qemu-7.2 bash tools/setup_qemu.sh
#
# Nothing here is machine-specific. QEMU_SRC defaults to a sibling of this checkout.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
QEMU_SRC=${QEMU_SRC:-$ROOT/../qemu-7.2}

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

[ -d "$QEMU_SRC" ] || die "no QEMU source tree at $QEMU_SRC -- set QEMU_SRC"
[ -f "$QEMU_SRC/hw/arm/meson.build" ] || die "$QEMU_SRC does not look like a QEMU tree"
for f in qemu/py32f071.c qemu/armv7m_systick.c.patched qemu/armv7m_systick.h.patched; do
    [ -f "$ROOT/$f" ] || die "missing $f"
done

say "== 1. machine and timer sources"
# py32f071.c is ours outright. The two systick files are whole upstream files with
# this machine's changes in them; they are copied over and the originals are kept as
# .orig so a diff against a new QEMU release is a diff, not an archaeology exercise.
cp "$ROOT/qemu/py32f071.c" "$QEMU_SRC/hw/arm/py32f071.c"
for pair in "hw/timer/armv7m_systick.c:qemu/armv7m_systick.c.patched" \
            "include/hw/timer/armv7m_systick.h:qemu/armv7m_systick.h.patched"; do
    dst="${pair%%:*}"; src="${pair##*:}"
    if [ -f "$QEMU_SRC/$dst" ] && [ ! -f "$QEMU_SRC/$dst.orig" ]; then
        cp "$QEMU_SRC/$dst" "$QEMU_SRC/$dst.orig"
    fi
    cp "$ROOT/$src" "$QEMU_SRC/$dst"
done

say "== 2. register the machine"
if ! grep -q 'config UVK5_V3' "$QEMU_SRC/hw/arm/Kconfig"; then
    cat >> "$QEMU_SRC/hw/arm/Kconfig" <<'KCONFIG'

config UVK5_V3
    bool
    default y
    depends on TCG && ARM
    select PY32F071_SOC

config PY32F071_SOC
    bool
    select ARM_V7M
    select UNIMP
KCONFIG
    say "   appended UVK5_V3 / PY32F071_SOC to hw/arm/Kconfig"
else
    say "   hw/arm/Kconfig already has UVK5_V3"
fi
if ! grep -q "py32f071.c" "$QEMU_SRC/hw/arm/meson.build"; then
    cat >> "$QEMU_SRC/hw/arm/meson.build" <<'MESON'

arm_ss.add(when: 'CONFIG_UVK5_V3', if_true: files('py32f071.c'))
MESON
    say "   added py32f071.c to hw/arm/meson.build"
else
    say "   hw/arm/meson.build already lists py32f071.c"
fi

say "== 3. configure and build"
cd "$QEMU_SRC"
if [ ! -f build/build.ninja ]; then
    ./configure --target-list=arm-softmmu --disable-docs --disable-tools
fi
ninja -C build qemu-system-arm

say ""
say "built: $QEMU_SRC/build/qemu-system-arm"
say "point the tools at it with QEMU=$QEMU_SRC/build/qemu-system-arm, or run"
say "    bash tools/run_tests.sh -q"
