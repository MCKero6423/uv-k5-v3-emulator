#!/usr/bin/env bash
# Run every test, report what failed, and exit non-zero if anything did.
#
# This exists because the suite had grown to ten separate invocations that had to be
# remembered and pasted in the right order. That is how regressions slip through: it is
# too easy to run the two tests related to what you just changed and miss the one that
# broke.
#
# Emulator tests boot their own QEMU and take 20-30 s each, so a full run is a few
# minutes. Pass -q to run only the fast unit tests, which need no emulator at all and
# finish in about 15 s -- useful while iterating.
#
# The build is checked FIRST and a failure stops everything. ninja leaves the previous
# binary in place when it fails, so the tests would otherwise run happily against a
# stale build and report results for code that was never compiled. That has produced
# two rounds of meaningless output before now.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SIM=$(dirname "$HERE")
QEMU_SRC=${QEMU_SRC:-/root/qemu-build/qemu-7.2+dfsg}

QUICK=0
[ "${1:-}" = "-q" ] && QUICK=1

pass=0
fail=0
failed_names=""

run() {
    local name="$1"; shift
    printf '\n=== %s\n' "$name"
    # Strip control characters. Some tests shell out to gdb, whose output can carry
    # escape sequences and stray bytes; left alone they make the combined log a
    # "binary file" as far as grep is concerned, which silently swallows the summary.
    #
    # PIPESTATUS, not $?, because $? here is sed's status and would report success
    # for every failing test.
    "$@" 2>&1 | tr -cd '\11\12\15\40-\176' | sed 's/^/    /'
    if [ "${PIPESTATUS[0]}" = "0" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        failed_names="$failed_names $name"
    fi
}

# --- build ---------------------------------------------------------------
# Only when the model source differs from what the QEMU tree holds, so a plain test
# run does not pay for a rebuild it does not need.
if [ -d "$QEMU_SRC/build" ] && \
   ! cmp -s "$SIM/qemu/py32f071.c" "$QEMU_SRC/hw/arm/py32f071.c"; then
    echo "=== build (model source changed)"
    cp "$SIM/qemu/py32f071.c" "$QEMU_SRC/hw/arm/py32f071.c"
    if (cd "$QEMU_SRC/build" && ninja qemu-system-arm 2>&1 | tail -20) \
            | grep -qE 'FAILED|error:'; then
        echo "    BUILD FAILED -- stopping before any test runs"
        echo "    (a stale binary would otherwise be tested silently)"
        exit 1
    fi
    echo "    ok"
fi

# --- unit tests, no emulator --------------------------------------------
cd "$HERE"
# First, that this script itself reports failures. A runner that silently counts every
# test as passing is worse than no runner, because it gets trusted.
run "runner self-check"   bash "$HERE/test_run_tests.sh"
run "unit: model helpers" python3 -m unittest discover -p 'test_uvk5*.py' -q
run "unit: web UI"        python3 -m unittest test_webui -q

if [ "$QUICK" = "1" ]; then
    printf '\n%d passed, %d failed (unit tests only)\n' "$pass" "$fail"
    [ "$fail" = "0" ] || { echo "failed:$failed_names"; exit 1; }
    exit 0
fi

# --- emulator tests -----------------------------------------------------
# Ordered cheapest first, so an obvious breakage surfaces without waiting for the
# whole run.
cd "$SIM"
run "keypad"            python3 tools/keypad_test.py
run "BK4819 registers"  python3 tools/test_bk4819.py
run "register readback" bash tools/test_bk4819_readback.sh
run "S-meter"           python3 tools/test_smeter.py
run "PTT"               python3 tools/test_ptt.py
run "scan"              python3 tools/test_scan.py
run "audio path"        python3 tools/test_audio_path.py
run "battery"           python3 tools/test_battery.py
run "millis"            python3 tools/test_millis.py
run "spectrum"          python3 tools/test_spectrum.py
run "serial receive"    python3 tools/test_serial_rx.py
run "flash persistence" python3 tools/test_flash_persist.py
run "frequency entry"   python3 tools/test_freq_entry.py

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if [ "$fail" != "0" ]; then
    echo "failed:$failed_names"
    exit 1
fi
