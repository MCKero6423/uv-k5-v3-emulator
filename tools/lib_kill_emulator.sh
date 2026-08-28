#!/usr/bin/env bash
# Terminate running emulator instances, and nothing else.
#
# Source this and call kill_emulators.
#
# Why this exists instead of a one-line pkill: `pkill -f 'M uv-k5-v3'` matches the
# whole command line, which includes
#
#   - the shell running the pkill, because the pattern sits in its own argv
#   - any test script or editor whose command line happens to mention the machine
#   - a webui.py-owned QEMU, killing the web UI out from under a user; that produced
#     a 502 through the reverse proxy twice in one session
#
# Matching the binary name exactly with pgrep -x, then confirming the machine type
# in /proc/PID/cmdline, keeps the blast radius to actual emulator processes.

# Kill emulator processes matching a QMP socket path, or all of them if none given.
#
# Scoping by socket matters even once the pattern is safe. A test that clears out
# "any emulator" still kills the one a running webui.py owns: the process survives
# but its guest is gone, so the user gets a dead screen and
# {"status":"unreachable"}. Passing the socket the caller is about to use leaves
# other people's instances alone.
kill_emulators() {
    local want_sock="${1:-}" pid cmdline self=$$

    for pid in $(pgrep -x qemu-system-arm 2>/dev/null || true); do
        [ "$pid" = "$self" ] && continue
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || continue

        # Must be one of ours.
        case "$cmdline" in
            *uv-k5-v3*) ;;
            *) continue ;;
        esac

        # If a socket was named, only touch the instance serving it.
        if [ -n "$want_sock" ]; then
            case "$cmdline" in
                *"$want_sock"*) ;;
                *) continue ;;
            esac
        fi

        kill "$pid" 2>/dev/null || true
    done
}
