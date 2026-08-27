#!/usr/bin/env bash
# Print the program counter and the two delay-loop registers, once per line.
#
# Comparing successive lines distinguishes three cases: a stuck delay (same PC,
# same r1), a converging delay (same PC, rising r1) and forward progress
# (different PC, or r1 reset for a new call).
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
SAMPLES="${1:-6}"
GAP="${2:-2}"
SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

cat >"$SCRIPT" <<'EOF'
set confirm off
set pagination off
target remote :1234
info registers pc r0 r1 lr
detach
quit
EOF

for _ in $(seq "$SAMPLES"); do
    gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null \
        | awk '/^pc /{pc=$2} /^r0 /{r0=$2} /^r1 /{r1=$2} /^lr /{lr=$2}
               END{printf "pc=%s lr=%s target=%s elapsed=%s\n", pc, lr, r0, r1}'
    sleep "$GAP"
done
