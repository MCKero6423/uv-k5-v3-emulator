#!/usr/bin/env bash
# Dump every GPIOB register, to see how the port is actually configured.
#
# Layout from py32f071xB.h: MODER 0x00, OTYPER 0x04, OSPEEDR 0x08, PUPDR 0x0C,
# IDR 0x10, ODR 0x14.
set -uo pipefail

ELF="${ELF:-$HOME/uvk5-port/uvk5-sat/build/CW/nr7y.cw.elf}"
BASE=0x50000400
SCRIPT=$(mktemp --suffix=.gdb)
trap 'rm -f "$SCRIPT"' EXIT

{
    echo "set confirm off"
    echo "set pagination off"
    echo "target remote :1234"
    echo "printf \"MODER   \""
    echo "x/1xw $((BASE + 0x00))"
    echo "printf \"PUPDR   \""
    echo "x/1xw $((BASE + 0x0c))"
    echo "printf \"IDR     \""
    echo "x/1xw $((BASE + 0x10))"
    echo "printf \"ODR     \""
    echo "x/1xw $((BASE + 0x14))"
    echo "detach"
    echo "quit"
} >"$SCRIPT"

gdb-multiarch -batch -x "$SCRIPT" "$ELF" 2>/dev/null | grep -E "MODER|PUPDR|IDR|ODR|0x5000"
