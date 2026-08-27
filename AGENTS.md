# Working on this repo

Notes for whoever picks this up next. Focused on what is not obvious from the
code, and on mistakes that already cost time here.

## What this is

A QEMU machine for the Puya PY32F071 (Cortex-M0+), so Quansheng UV-K5 V3
firmware runs on a PC. Boots to the main loop in ~5 s; the LCD is readable.

The machine and every device model live in one file, `qemu/py32f071.c`. That is
deliberate: the models are small and tightly coupled to each other's wiring, and
splitting them would spread the board layout out without making any of it
clearer.

## Ground rules

**Never edit the firmware to make the emulator work.** The firmware is the
reference. If something does not run, the model is wrong. A fix that changes
firmware source makes every later test meaningless, because you are no longer
testing what the radio runs.

**Register layouts come from the vendor CMSIS header**, not from a datasheet
search and not from inference:

    <firmware>/Drivers/CMSIS/Device/PY32F071/Include/py32f071xB.h

When you need a bit position, read it from there. Several details are
unintuitive — `LL_ADC_FLAG_EOS` is really `ADC_SR_EOC` on this part — and
guessing produces models that look right and hang.

**Find the next thing to model by watching where the firmware stops**, not by
reading the datasheet front to back. Every peripheral here was added because the
firmware demonstrably waited on it:

    tools/where.sh 4          # sample the call stack a few times

A stack that repeats in the same function across samples is a spin loop. Look at
what it reads.

## How to run it

    python3 tools/make_flash.py    # once; builds assets/flash.img
    tools/run.sh                  # GDB stub on :1234, QMP on /tmp/uvk5-qmp.sock

    tools/where.sh                # where execution is
    tools/gpiob_dump.sh           # GPIOB registers
    python3 tools/key.py MENU     # inject a keypress
    python3 tools/screenshot.py --frame-addr 0x200013DC \
        --status-addr 0x2000175C --port 1234 --out screen.png

Screenshot addresses move between firmware builds. Get the current ones with:

    arm-none-eabi-nm firmware.elf | grep -E 'gFrameBuffer|gStatusLine'

Rebuild after editing the machine:

    cd $QEMU/build && ninja qemu-system-arm    # ~10 s incremental

## Things that already went wrong

**GDB breakpoints halt the guest.** A key held across a breakpoint session is
never processed, because the main loop is not running. This produced a whole
round of "the keypress does nothing" that was really "the machine is stopped".
Use `tools/press_and_shot.sh` — it presses, lets the machine run, then reads the
framebuffer, with no breakpoints anywhere.

**Do not write the SysTick counter back when accelerating it.** Two attempts did
that. Each read re-anchored the count, so the value the firmware saw stopped
changing, its `if (cur != prev)` guard never fired, and the delay loop hung
outright — worse than the slowness being fixed. The working approach reports a
value that runs ahead of the real counter and leaves the timer alone.

**Lowering the clock does not speed up delay loops.** The bottleneck is loop
iterations per second, not counter speed. 48 MHz to 200 Hz bought 32x and was
nowhere near enough. Measured, not assumed.

**Unnamed qdev in and out lines share one namespace.** A device with both
unnamed `qdev_init_gpio_in` and `qdev_init_gpio_out` makes `qdev_get_gpio_in()`
ambiguous, and board wiring silently attaches to the wrong line. The GPIO model
uses `"pin-in"` and `"pin-out"` for this reason. Keep it that way.

**Key hold times must be generous.** Guest time runs fast, so 400 ms of wall
clock was too short for the firmware's debounce to complete. `key.py` holds for
2500 ms. If a press seems ignored, lengthen it before suspecting the wiring.

**Verify a tool's own parsing before trusting its output.** `gpio_watch.py`
reported `IDR=0x0000` for several rounds because its regex did not match gdb's
output format at all. The register was fine; the reader was broken. Cross-check
with `tools/gpiob_dump.sh`, which uses a different path.

## Known gap: the keypad

Keypresses reach the firmware but the UI does not react. What is established:

- The keypad model holds the right state (`qom-get press` reads back the key)
- Row lines are driven: TRACE shows `row0 -> 0` while MENU is held
- The firmware's scan sees it: at the IDR read inside `KEYBOARD_Poll`,
  `ODR=033c IDR=7fbf` — column 1 low, row 0 low
- `KEYBOARD_Poll` returns 10, which is `KEY_MENU` in `driver/keyboard.h`

So the matrix works and the scan decodes correctly. Whatever is wrong is
downstream, in how `app.c` debounces or dispatches the returned key. That is
where to look — not at the wiring, which has been checked more than enough.

Useful here: `tools/scan_trace.sh` (what the scan reads), `tools/key_result.sh`
(what Poll returns), `tools/trace_run.sh` (the TRACE points, currently compiled
in).

There is `fprintf(stderr, "TRACE ...")` instrumentation in `qemu/py32f071.c` at
three points. Remove it once the keypad works.

## What this cannot do

It reproduces what the firmware *commanded* — frequency, power step, carrier
keying in time. It does not reproduce the analogue result: keying envelopes,
spurious emissions, sensitivity.

That is not a gap to close later. The BK4819/BK4829 transceiver has no public
datasheet, so its driver is the only specification available, and a driver tells
you which registers were written, never what left the antenna. Those questions
need a real radio and a spectrum analyser. Do not let anyone conclude otherwise
from a passing emulator test.

Timing is also deliberately wrong — see the SysTick section in README.md. Fine
for menus and control flow; useless for signal timing.

## If you add a peripheral

1. Read the register layout from the CMSIS header
2. Model only what the firmware actually touches; the logging catch-all
   (`py32-stub`) shows you what that is
3. Watch for spin loops: any flag the firmware polls must be able to change, and
   write-1-to-start bits (like `ADC_CR2_CAL`) must never be stored set
4. Rebuild, run, and check with `tools/where.sh` that the firmware moved past
   where it used to stop
