#!/usr/bin/env bash
# Print the emulated firmware's current call stack.
#
# Usage: where.sh [samples]
set -uo pipefail

# shellcheck source=tools/uvk5_elf.sh
. "$(dirname "$0")/uvk5_elf.sh"
ELF=$(uvk5_find_elf) || { echo "SKIP  no firmware found; set ELF or put one in assets/firmware" >&2; exit 0; }
SAMPLES="${1:-1}"
SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

cat >"$SCRIPT" <<'EOF'
set confirm off
set pagination off
target remote :1234
bt 5
detach
quit
EOF

for _ in $(seq "$SAMPLES"); do
    gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null \
        | grep '^#' \
        | sed 's/ (.*//; s/^#[0-9]* *//; s/0x[0-9a-f]* in //' \
        | grep -v 'signal handler' \
        | paste -sd' < ' -
    [ "$SAMPLES" -gt 1 ] && sleep 1
done
