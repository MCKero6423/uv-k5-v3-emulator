# Minesweeper — an overlay app for the F4HWN Labs edition

Our own app: a 9x9 minesweeper that runs on the radio inside the 4 KiB overlay that
`App/apps/app_overlay.h` reserves. It is written against upstream's `App/apps/app_api.h`
and built with upstream's `app.ld` — **neither is vendored here** (both are Apache-2.0
files from [armel/uv-k1-k5v3-firmware-custom](https://github.com/armel/uv-k1-k5v3-firmware-custom)),
so drop this folder into `App/apps/minesweeper/` next to them, or point `-I` at a copy.

## Why it looks the way it does

| constraint | consequence |
| --- | --- |
| the radio has **no left/right keys** (UP, DOWN, MENU, EXIT, STAR, F, 0-9 only) | the cursor walks the field with UP/DOWN and digits jump to a row then a column: `3` `5` = row 3, column 5 |
| **4 KiB** for text+rodata+data+bss together | no lookup tables, no floats, no libc; adjacency is counted on the fly and each cell is one bit |
| 81 cells do not fit a 16-bit mask | three 9-byte bit arrays addressed by `cell >> 3`, `cell & 7` — a `uint16_t` version compiled fine and was wrong past cell 15 |
| the resident pixel helpers do **not** bound-check | `put()`/`invert()` clip |
| no `rand()` in a freestanding blob | a small LCG; mines are placed **after the first reveal**, keeping the 3x3 around it clear |

Keys: UP/DOWN move, 1-9 pick row then column, MENU reveal, F flag, STAR new game,
EXIT quit. `M` in the corner is the remaining-mine count, `A1` is the cursor.

## Build

    arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -Os -std=gnu11 -ffreestanding \
        -nostdlib -nostartfiles -T app.ld -Wl,--defsym,APP_VMA=0x20000280 \
        -o minesweeper.elf minesweeper_app.c
    arm-none-eabi-objcopy -O binary minesweeper.elf minesweeper.bin
    pack_app.py minesweeper.bin Minesweeper.app --name Minesweeper --ver 1.0 \
        --vma 0x20000280 --api-min 1      # pack_app.py lives in App/apps/

`build.sh` does exactly that and needs the Arm GNU Toolchain on PATH.

Then install it **from the page**: *Overlay apps* → a slot → pick `Minesweeper.app` →
**Install** → **Ask the radio** should answer `Minesweeper`. On the radio press
**F** then **7** and **MENU** to run it.

## What is verified, and what is not

Verified here: the source compiles clean with `gcc -Wall -Wextra -Werror` against
upstream's real `app_api.h` (that check caught the API's actual member names —
`api->fb`, `print_tiny(s, x, y, statusbar, fill)` with **five** arguments — and the fact
that `APP_KEY_LEFT`/`APP_KEY_RIGHT` do not exist).

Not verified: it has never been built for ARM or run on the radio, because no
`arm-none-eabi-gcc` and no Docker exist on the machine it was written on. Treat the
first build and the first run as the real review.
