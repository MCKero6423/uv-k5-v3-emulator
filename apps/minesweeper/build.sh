#!/usr/bin/env bash
# Build one overlay-app blob (.app) locally, without Docker.
#
# Upstream runs this same file inside the uvk1-uvk5v3 image; it already prefers
# arm-none-eabi-gcc from PATH when there is one, which is what makes a local build
# possible. Link VMA is pinned at 0x20000280 and must match the firmware's
# __mb_workspace_start -- the loader compares them and refuses with APP_VMA.
set -euo pipefail

APP="$(basename "$PWD")"
APP_NAME="Minesweeper"          # <-- the only per-app line: the label in the 64-byte header
APP_VER="1.0"
APP_API_MIN=1
APP_VMA=${APP_VMA:-0x20000280}
OUT="${APP_NAME// /}"

CC=${CC:-arm-none-eabi-gcc}
OBJCOPY=${OBJCOPY:-arm-none-eabi-objcopy}
command -v "$CC" >/dev/null 2>&1 || {
  echo "no $CC on PATH; install the Arm GNU Toolchain (arm-none-eabi) first" >&2; exit 2; }

CFLAGS="-mcpu=cortex-m0plus -mthumb -Os -std=gnu11 -ffreestanding -fno-builtin -fno-common \
  -fomit-frame-pointer -ffunction-sections -fdata-sections -Wall -Wextra"
LDFLAGS="-nostdlib -nostartfiles -T app.ld -Wl,--defsym,APP_VMA=${APP_VMA} \
  -Wl,--gc-sections -Wl,-Map=${APP}.map -Wl,--build-id=none"

rm -f ./*.app ./*.elf ./*.bin
"$CC" $CFLAGS $LDFLAGS -o "${APP}.elf" "${APP}_app.c"
"$OBJCOPY" -O binary "${APP}.elf" "${APP}.bin"
python3 pack_app.py "${APP}.bin" "${OUT}.app" --name "$APP_NAME" --ver "$APP_VER" \
        --vma "$APP_VMA" --api-min "$APP_API_MIN"
ls -l "${OUT}.app"
