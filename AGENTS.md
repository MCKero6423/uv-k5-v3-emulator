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

After any change near the keypad or the GPIO wiring, run the regression test. It
boots its own instance on private ports, so it does not disturb a `run.sh`
session:

    python3 tools/keypad_test.py

There is also a browser UI, which is usually the quickest way to poke at the
firmware by hand:

    python3 tools/webui.py --frame-addr 0x200013DC \
        --status-addr 0x2000175C     # then open http://127.0.0.1:8080/

Two things about it that matter when working on this repo:

- **It holds the QMP socket for its lifetime**, so `key.py` cannot run at the same
  time. The socket accepts a single client.
- **It reads frames with QMP `memsave`, deliberately.** Not `pmemsave`, which
  takes a *physical* address and silently returns zeros for `gFrameBuffer` --
  a blank screen with no error. And not gdb, which halts the guest on every
  attach: that stutters the stream and perturbs key debounce timing.

Its tests: `tools/test_uvk5_*.py` and `tools/test_webui.py` need no emulator,
`tools/test_webui_e2e.py` boots its own.

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

**Key hold times must be SHORT, not generous.** This entry used to say the
opposite -- that guest time runs fast so a press needs a long hold, and that
`key.py` should hold for 2500 ms. That was wrong and it broke the keypad tooling
for a long time. 2500 ms is ~250 firmware ticks, six times past the long-press
threshold, so every press was dispatched as a *hold* and handlers that act on a
short release did nothing. See the keypad section below; `key.py` now holds 200 ms.

**Verify a tool's own parsing before trusting its output.** `gpio_watch.py`
reported `IDR=0x0000` for several rounds because its regex did not match gdb's
output format at all. The register was fine; the reader was broken. Cross-check
with `tools/gpiob_dump.sh`, which uses a different path.

**QMP `pmemsave` is physical, `memsave` is virtual.** The framebuffer symbols are
CPU virtual addresses, so `pmemsave` on `gFrameBuffer` returns a block of zeros
and reports success -- a blank screen with nothing logged anywhere. The web UI was
built on `pmemsave` first because a timing benchmark said it was fast; the
benchmark never checked the *contents*. Measure the thing you actually care
about: the bug surfaced only when a rendered frame came back with 0 lit pixels
where the gdb path reported 1693.

## The keypad: two real bugs, both fixed

The old note here said "keys reach the firmware but the UI does not react" and
blamed the machine model. There turned out to be two independent causes, in this
order:

1. **`tools/key.py` held every key for 2500 ms** — a tooling bug, covered
   immediately below.
2. **`row_out` was not `volatile`, so GCC deleted the row-driving code** — a real
   model bug, introduced later while removing debug prints. See
   [row_out must stay volatile](#row_out-must-stay-volatile-or-gcc-deletes-the-keypad).

Both are fixed and `tools/keypad_test.py` guards against regressions in either.

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
a digit selects a value. Screenshots confirmed Step at 01/79, RxDCS at 03/79
after two DOWN presses, and BatSav at 30/79 showing OFF.

### row_out must stay volatile or GCC deletes the keypad

`UVK5KeypadState::row_out` is declared `qemu_irq volatile`. Drop the `volatile`
and the keypad stops working entirely: no press reaches the UI, awake or in power
save, and nothing warns you. `tools/keypad_test.py` covers it.

The reason is visible in the object code. `qdev_init_gpio_out_named()` is
inlinable and only records the array; the lines are filled in later by
`qdev_connect_gpio_out_named()` from the board, which GCC cannot see. Left plain,
GCC at -O2 proves every element is still NULL, sees that `qemu_set_irq()` returns
immediately on a NULL irq, and deletes the body of `keypad_update_rows()` along
with **all five calls to it**:

    callers reaching keypad_update_rows
      plain     {}                     <- none; the calls are gone
      volatile  {keypad_key_changed, keypad_col_changed, keypad_set_press,
                 keypad_reset, uvk5_machine_init}

`keypad_col_changed` compiles to a store and a `ret` with no call at all. With
`volatile` it ends in `jmp keypad_update_rows`. So no row line is ever driven,
the firmware's scan reads all-high, and the model looks broken.

Getting here took three wrong diagnoses, all worth knowing about:

1. **"Power save stops the keypad scan."** Written up here as a model gap. It was
   not: the breakage was present awake too.
2. **"It needs settling time."** Three `fprintf(stderr, "TRACE ...")` probes had
   been removed as cleanup, and restoring the one in `keypad_update_rows` fixed
   it, as did a busy loop in the same place. That looked like a timing
   dependency. It was not — the fprintf and the loop were just side effects GCC
   could not discard, which kept the loop alive.
3. **"It is a compiler ordering problem."** A zero-cost
   `__asm__ __volatile__("" ::: "memory")` also fixed it, 8/8. Same reason: a
   barrier is an unknown side effect, so the loop survives.

What settled it was comparing the two object files instead of the behaviour. The
standalone `keypad_update_rows` symbol is instruction-identical either way, which
is why an early diff of just that function found nothing — the function is
inlined into its callers, and the difference is there.

Measurements, 3+ trials each, no debugger near the press:

| variant | result |
| --- | --- |
| plain `row_out` | 0/12 |
| `(void)r;` added — inert, no side effect | 0/6 |
| identical rebuild (stability control) | 0/6 |
| busy loop, 1 to 4000 iterations | 3/3 |
| `__asm__ ... "memory"` barrier | 12/12 |
| **`volatile row_out`** (the actual fix) | **10/10** |

Scope, checked rather than assumed: the other out-GPIO array in this file,
`PY32GpioState::out`, is **not** affected. Marking it volatile as well produces a
byte-identical object file, because the function that drives those lines
(`py32_gpio_write`) is only reachable through a `MemoryRegionOps` function-pointer
table, so GCC cannot do the whole-function reasoning that killed the keypad path.
Leave it plain.

The general shape to watch for: a device whose out-GPIO lines are only ever
connected from board code, driven from a function GCC can see all callers of. If a
model's outputs mysteriously do nothing, check the object code for the call before
assuming the logic is wrong:

    objdump -dr build/libqemu-arm-softmmu.fa.p/hw_arm_py32f071.c.o \
        | grep -c qemu_set_irq

Two measurement mistakes made this much harder than it needed to be, both worth
avoiding:

- **Reading key state after releasing the key.** `gKeyReading0` is always
  `KEY_INVALID` once the key is up, so it "proves" the press was never seen. Read
  mid-hold instead.
- **Trusting a gdb breakpoint on `KEYBOARD_Poll`.** With the guest stopped the
  scan's delays cost no guest time, so `Poll` returns `KEY_MENU` under a
  breakpoint on a build where it returns `KEY_INVALID` when running free. That
  single observation sent this in the wrong direction for a long time.

Two related facts, both confirmed by experiment, so nobody spends time on them:

- **Patching battery save in `assets/flash.img` does nothing.**
  `SETTINGS_InitEEPROM` compares a version string at flash `0x00A160`, finds a
  mismatch on a fresh image, and writes the settings sector.
  `PY25Q16_WriteBuffer` erases the whole 4 KB sector before reprogramming, so a
  byte planted at `0x00A00B` is gone before the read at `settings.c:169` sees it.
- **Guest-side settings changes do not persist.** The emulated PY25Q16 loads the
  image into RAM at realize time and never writes back, so anything the firmware
  saves is lost on restart. Adding a flush would be the fix if persistent
  settings are ever wanted. Nothing needs it today.

Useful here: `tools/scan_trace.sh` (what the scan reads), `tools/key_result.sh`
(what Poll returns), `tools/trace_run.sh` (the TRACE points).

The three `fprintf(stderr, "TRACE ...")` probes that used to sit in
`qemu/py32f071.c` are gone -- they fired on every keypad poll and buried the
console. They went in `py32_gpio_set_input`, `keypad_update_rows` and
`keypad_col_changed`; `git log -p -- qemu/py32f071.c` has the exact lines, and
they are still the quickest way to see whether a press reaches the model
(`grep -c 'keypad row0 -> 0'` on the captured stderr).

Redirect that stderr to a file rather than a pipe, and be aware that the
`keypad_update_rows` one changes timing enough to matter -- see the settle-loop
note above.

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
