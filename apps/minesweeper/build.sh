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

# Two routes, because only one of them was available where this was written:
#   1. the Arm GNU Toolchain, which is what upstream's own build.sh expects
#   2. Zig's bundled clang (pip install ziglang), which cross-compiles to
#      thumb-freestanding-eabi and needs no arm-none-eabi-gcc at all
ZIG=""
if ! CC=${CC:-$(command -v arm-none-eabi-gcc 2>/dev/null)} || [ -z "$CC" ]; then
  ZIG=${ZIG:-$(command -v zig 2>/dev/null || echo "python -m ziglang")}
  CC="$ZIG cc"
  echo "no arm-none-eabi-gcc; using $ZIG cc"
fi

CFLAGS="-mcpu=cortex-m0plus -mthumb -Os -std=gnu11 -ffreestanding -fno-builtin -fno-common \
  -fomit-frame-pointer -ffunction-sections -fdata-sections -Wall -Wextra"
# Zig refuses --defsym, -Ttext and --section-start, so the VMA is resolved into a copy of
# the linker script instead, and the entry is pinned by the section attribute the source
# already carries (.text.entry, exactly as upstream's apps do).
sed "s/APP_VMA *= *DEFINED(APP_VMA) *? *APP_VMA *: *0x[0-9A-Fa-f]*;/APP_VMA = ${APP_VMA};/" \
    app.ld > app-resolved.ld
LDFLAGS="-nostdlib -T app-resolved.ld -Wl,-e,app_main -Wl,--gc-sections -Wl,--build-id=none"

rm -f ./*.app ./*.elf ./*.bin
# shellcheck disable=SC2086
$CC $CFLAGS $LDFLAGS -o "${APP}.elf" "${APP}_app.c"
# objcopy if there is one, otherwise the section-based extractor from this repository
if command -v arm-none-eabi-objcopy >/dev/null 2>&1; then
  arm-none-eabi-objcopy -O binary "${APP}.elf" "${APP}.bin"
else
  python3 "$(dirname "${BASH_SOURCE[0]}")/../../../tools/elf2bin.py" "${APP}.elf" "${APP}.bin" --min "${APP_VMA}"
fi
python3 pack_app.py "${APP}.bin" "${OUT}.app" --name "$APP_NAME" --ver "$APP_VER" \
        --vma "$APP_VMA" --api-min "$APP_API_MIN"
ls -l "${OUT}.app"
