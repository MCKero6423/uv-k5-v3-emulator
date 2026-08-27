#!/usr/bin/env bash
# Measure how fast a SYSTICK_DelayUs loop is converging.
#
# r0 holds the target tick count, r1 the accumulated elapsed count, so sampling
# both twice gives the rate and an estimate of how long the delay will take.
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
GAP="${1:-4}"
SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

cat >"$SCRIPT" <<'EOF'
set confirm off
set pagination off
target remote :1234
info registers r0 r1
detach
quit
EOF

read_regs() {
    gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null \
        | awk '/^r0 /{t=strtonum($2)} /^r1 /{e=strtonum($2)} END{print t, e}'
}

first=$(read_regs)
sleep "$GAP"
second=$(read_regs)

awk -v a="$first" -v b="$second" -v gap="$GAP" 'BEGIN {
    split(a, x, " "); split(b, y, " ")
    target = y[1]; from = x[2]; to = y[2]
    rate = (to - from) / gap
    printf "target=%d elapsed %d -> %d  (%.0f ticks/s)\n", target, from, to, rate
    if (rate > 0 && target > to)
        printf "remaining: %.1f s\n", (target - to) / rate
    else if (target <= to)
        print "delay already satisfied"
    else
        print "not advancing"
}'
