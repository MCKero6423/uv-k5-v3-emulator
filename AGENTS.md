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

## The keypad: two separate problems, one fixed

The old note here said "keys reach the firmware but the UI does not react" and
pointed at the machine model. The model was not the problem. There were two
independent causes, and only the first is fixed.

### Fixed: key.py held keys far too long

`tools/key.py` was holding every key for 2500 ms.

The two SysTick mechanisms are separate, and conflating them caused this:

- SysTick **interrupts** fire at close to real time. `SysTick_Handler` sets
  `gNextTimeslice`, which gates `APP_TimeSlice10ms` -> `CheckKeys`. So the
  debounce thresholds in `App/misc.c` apply in wall clock as written:
  `key_debounce_10ms = 2` (20 ms to register), `key_repeat_delay_10ms = 40`
  (400 ms counts as *held*).
- The `poll-boost` property accelerates SysTick counter **reads**, so
  `SYSTICK_DelayUs` converges. It does not speed up interrupt delivery.

A 2500 ms hold is ~250 ticks, six times past the long-press threshold. Every
press was dispatched as a hold, and the handlers act on a short release:
`MAIN_Key_MENU` returns early at the `if (bKeyHeld)` branch and never opens the
menu. Confirmed by reading `gDebounceCounter` mid-hold — it stood at 317 after a
3 s hold, which both proves the timeslice is running and shows the hold was far
too long.

Current values in `key.py`: `HOLD_MS = 200`, `LONG_HOLD_MS = 900`. Verified end
to end — `key.py MENU DOWN DOWN` moves the menu from 01/79 to 03/79, and
`key.py UP` moves it back to 02/79.

If a press seems ignored, do not lengthen the hold. Check whether the handler
wanted a short press, and check `gEeprom.KEY_LOCK` (the LCD draws a padlock when
the keypad is locked, and ignoring keys is then correct behaviour).

### Driving the menus: send a sequence as one burst

Three things will make a key sequence land somewhere you did not intend. All
three cost time here.

**gdb between presses halts the guest.** Every `gdb-multiarch -batch` attach
stops the machine for its duration. Inspecting `gMenuCursor` after each press
stretches a six-press sequence past the 20 s menu timeout
(`menu_timeout_500ms` in `App/misc.c`), so the UI silently falls back to the main
screen and the rest of the presses tune the VFO instead of navigating. Send the
whole sequence in one Python burst over QMP, then read state once at the end.

**UP/DOWN are inverted inside a submenu.** `MENU_Key_UP_DOWN` flips `Direction`
when `gIsInSubMenu` and `!gEeprom.SET_NAV` (`app/menu.c:2311`). In the list DOWN
moves down; editing a value, UP *decreases* it. Values also clamp at
`MENU_GetLimits` rather than wrapping, so overshooting sticks at the limit.

**MENU toggles rather than only entering.** On the main screen a short MENU opens
the menu; in the list it enters the submenu; in a submenu it commits
(`gFlagAcceptSetting = true`) and steps back out. Two MENU presses in a row from
the list therefore enter and immediately leave, which looks like nothing
happened.

Numeric jump: typing a menu number in the list jumps straight to it, which beats
counting DOWN presses. Single digits are reliable. Two-digit entry needs both
presses inside the same input-box window, and `MENU_Key_0_to_9` jumps and returns
as soon as the first digit is a valid index (`app/menu.c:1826`), so `3` then `0`
lands on 3 rather than 30. Pre-positioning `gMenuCursor` with gdb, in one attach
right after opening the menu, is the reliable way to reach a distant entry.

Verified this way: menu opens, DOWN/UP move the list, MENU enters a submenu, and
a digit selects a value. Screenshots confirmed BatSav at 30/79 showing OFF.

### Known limitation: power save stops the keypad scan

The keypad works, but only while the radio is awake. Measured on a fresh boot:
`gCurrentFunction` is 0 (`FUNCTION_FOREGROUND`) until about 6 s, then becomes 5
(`FUNCTION_POWER_SAVE`) and the scan stops:

    gRxIdleMode=1  gCurrentFunction=5

From then on `KEYBOARD_Poll` returns `KEY_INVALID` however long or often a key is
held — eight consecutive `key.py MENU` presses left `gKeyReading0` at 19 and
`gScreenToDisplay` at 0. A real radio wakes on a keypress, so this is a gap in
the model rather than firmware behaviour. The suspect is the sleep/wake path in
`HandlePowerSave`, which calls `BK4819_Sleep()` and spins on `BK4819_REG_0C`
bit 0 over the bit-banged bus. Note that bus shares GPIOB with the keypad (bus on
PB8/PB9, columns PB3-PB6, rows PB12-PB15), and the model idles PB9 low as a
deliberate hack so those reads return zero — worth a look when someone picks this
up.

Working around it is easy and does not need the gap closed: any keypad activity
keeps the radio awake, and being in the menu blocks power save outright
(`gScreenToDisplay != DISPLAY_MAIN` guards the entry at `app/app.c:1377`). So
open the menu within the first few seconds of boot and a session stays usable
indefinitely. Turning BatSav off through the UI works too, though the setting
does not survive a restart — see below.

Two dead ends, both confirmed by experiment, so nobody repeats them:

- **Patching battery save in `assets/flash.img` does nothing.**
  `SETTINGS_InitEEPROM` compares a version string at flash `0x00A160`, finds a
  mismatch on a fresh image, and writes the settings sector.
  `PY25Q16_WriteBuffer` erases the whole 4 KB sector before reprogramming, so a
  byte planted at `0x00A00B` is gone before the read at `settings.c:169` sees it.
- **Guest-side settings changes do not persist.** The emulated PY25Q16 loads the
  image into RAM at realize time and never writes back, so anything the firmware
  saves is lost on restart. Adding a flush would be the real fix if persistent
  settings are ever wanted.

Useful here: `tools/scan_trace.sh` (what the scan reads), `tools/key_result.sh`
(what Poll returns), `tools/trace_run.sh` (the TRACE points).

The three `fprintf(stderr, "TRACE ...")` probes that used to sit in
`qemu/py32f071.c` are gone -- they fired on every keypad poll and buried the
console. If you need them back while chasing the power save gap, they went in
`py32_gpio_set_input`, `keypad_update_rows` and `keypad_col_changed`; `git log -p
-- qemu/py32f071.c` has the exact lines. `grep -c 'keypad row0 -> 0'` on the
captured stderr was how the matrix got confirmed working, and it is still the
quickest check that a press reaches the model.

Note the ELF at `uvk5-sat/build/CW/nr7y.cw.elf` carries no DWARF, so gdb reports
`'gEeprom' has unknown type`. Scalars work if you cast through their address
(`*(unsigned short*)&gDebounceCounter`); struct fields need manual offsets.

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
