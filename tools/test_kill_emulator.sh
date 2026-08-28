#!/usr/bin/env bash
# The cleanup in run.sh must not kill anything other than an emulator.
#
# Regression test for a real incident: `pkill -f 'M uv-k5-v3'` matched a running
# webui.py's QEMU child and the shell issuing the pkill, taking the web UI offline
# and returning 502 through the reverse proxy. Twice.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tools/lib_kill_emulator.sh
. "$HERE/lib_kill_emulator.sh"

fails=0
note() { printf '  %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# A decoy whose command line mentions the machine type but is not the emulator.
# The old pkill pattern would have killed this.
sleep 120 &
decoy=$!
# Rename via a wrapper so the cmdline contains the trap string.
bash -c 'exec -a "sleep -M uv-k5-v3 decoy" sleep 120' &
decoy2=$!
sleep 1

note "decoy pids: $decoy $decoy2"

kill_emulators

if ! kill -0 "$decoy" 2>/dev/null; then
    fail "killed a plain sleep"
else
    note "PASS  plain process untouched"
fi

if ! kill -0 "$decoy2" 2>/dev/null; then
    fail "killed a process merely mentioning uv-k5-v3 in its command line"
else
    note "PASS  non-emulator process mentioning uv-k5-v3 untouched"
fi

# The script running this must survive too; the old pattern matched its own argv.
note "PASS  this script survived (pid $$)"

kill "$decoy" "$decoy2" 2>/dev/null || true
wait 2>/dev/null || true

# And a real emulator must actually be terminated.
QEMU="$HOME/qemu-build/qemu-7.2+dfsg/build/qemu-system-arm"
ELF="$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf"
IMG=$(mktemp /tmp/kill-test-XXXX.img)
gzip -dc "$HERE/../assets/pristine/flash-pristine.img.gz" > "$IMG"

IMG2=$(mktemp /tmp/kill-test2-XXXX.img)
gzip -dc "$HERE/../assets/pristine/flash-pristine.img.gz" > "$IMG2"

if [ -x "$QEMU" ] && [ -f "$ELF" ]; then
    # Two instances on different sockets, standing in for "the test's own" and
    # "the one a running web UI owns".
    "$QEMU" -M "uv-k5-v3,flash-image=$IMG" -nographic -monitor none \
        -qmp "unix:/tmp/kill-test-a.sock,server=on,wait=off" -kernel "$ELF" \
        >/dev/null 2>&1 &
    mine=$!
    "$QEMU" -M "uv-k5-v3,flash-image=$IMG2" -nographic -monitor none \
        -qmp "unix:/tmp/kill-test-b.sock,server=on,wait=off" -kernel "$ELF" \
        >/dev/null 2>&1 &
    theirs=$!
    sleep 3

    if ! kill -0 "$mine" 2>/dev/null || ! kill -0 "$theirs" 2>/dev/null; then
        fail "test emulators did not start"
    else
        kill_emulators /tmp/kill-test-a.sock
        sleep 2

        if kill -0 "$mine" 2>/dev/null; then
            fail "the targeted emulator was left running"
        else
            note "PASS  the emulator on the named socket is terminated"
        fi

        # This is the property that protects a live session: someone else's
        # instance must survive a scoped cleanup.
        if kill -0 "$theirs" 2>/dev/null; then
            note "PASS  an emulator on another socket survives"
        else
            fail "killed an emulator belonging to a different socket"
        fi

        # Unscoped still clears everything.
        kill_emulators
        sleep 2
        if kill -0 "$theirs" 2>/dev/null; then
            fail "unscoped cleanup left an emulator running"
            kill "$theirs" 2>/dev/null || true
        else
            note "PASS  unscoped cleanup terminates all emulators"
        fi
    fi
else
    note "SKIP  emulator binary or firmware missing"
fi

rm -f "$IMG" "$IMG2" /tmp/kill-test-a.sock /tmp/kill-test-b.sock
wait 2>/dev/null || true

if [ "$fails" -gt 0 ]; then
    echo "$fails check(s) failed"
    exit 1
fi
echo "cleanup only ever kills emulators"
