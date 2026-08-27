#!/usr/bin/env bash
# Measure how long the emulated radio takes to reach its main loop.
#
# Restarts the machine, then polls the call stack until it shows APP_Update --
# the main loop -- and reports the elapsed wall-clock time. This is the number
# that matters in practice: how long until the screen is up and usable.
set -uo pipefail

TOOLS="$(cd "$(dirname "$0")" && pwd)"
TIMEOUT="${1:-120}"

"$TOOLS/run.sh" >/dev/null 2>&1 &
start=$(date +%s)

while :; do
    now=$(date +%s)
    elapsed=$((now - start))

    if [ "$elapsed" -gt "$TIMEOUT" ]; then
        echo "not in the main loop after ${TIMEOUT}s"
        echo "last stack: $("$TOOLS/where.sh" 1 2>/dev/null)"
        exit 1
    fi

    stack=$("$TOOLS/where.sh" 1 2>/dev/null || true)
    case "$stack" in
        *APP_Update*|*HandlePowerSave*|*UART_IsCommandAvailable*)
            echo "reached the main loop in ${elapsed}s"
            exit 0
            ;;
    esac
    sleep 2
done
