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

## How it boots

Worth reading before debugging anything that looks like a startup problem. There is
no bootloader, no kernel, no partition table and no filesystem -- the firmware is
the only code on the machine and it owns the CPU outright.

**The hardware knows two numbers.** A Cortex-M0+ coming out of reset does not run
any boot logic. It loads SP from the first word of the vector table and PC from the
second, and starts executing. That is the whole handoff.

    .isr_vector  0x08002800  (readelf -SW, size 0xc0)
      +0x00      0x20004000  initial SP, i.e. the top of the 16 KB SRAM
      +0x04      0x08002d49  Reset_Handler, and the ELF entry point

Read it straight off the image when in doubt -- the bytes are little-endian, so
`00400020 492d0008` is SP 0x20004000 followed by PC 0x08002d49:

    objdump -s -j .isr_vector firmware.elf | head -5

The odd address is not a typo: bit 0 flags Thumb state and the hardware masks it
off when fetching.

**`PY32_APP_OFFSET` 0x2800 is load-bearing.** Flash starts at `0x08000000` but the
first 10 KB is the factory bootloader region, so the application sits after it.
`armv7m_load_kernel()` is passed that offset for exactly this reason -- load at
`0x08000000` instead and the vector table lands in the wrong place, so the very
first fetch faults.

**Startup is 31 lines of assembly**, in the firmware's
`Core/startup_py32f071xx.s`:

    set SP from _estack
    bl SystemInit
    copy .data from flash (_sidata) into RAM (_sdata .. _edata)
    zero .bss (_sbss .. _ebss)
    bl __libc_init_array
    bl main
    LoopForever: b LoopForever      @ main never returns

The copy and the zero-fill are the interesting part. Initialised globals live in
flash but have to be writable, so they are copied word by word into RAM;
uninitialised globals must read as zero per the C standard, so `.bss` is cleared.
On a hosted OS the kernel and the loader do this for you. Here nobody does, so if
either loop is wrong you get globals that are silently garbage.

**Then the application:**

    main()                  Core/Src/main.c -- clock config only, then Main()
      Main()                App/main.c -- the actual firmware
        SYSTICK_Init()      the 10 ms tick everything is timed against
        BOARD_Init()        GPIO, SPI, LCD, keypad matrix
        UART_Init()         where the SERIAL banner in the log comes from
        SETTINGS_InitEEPROM()   reads settings over SPI from the flash image
        while (1) { ... }   main loop, never exits

**There is no filesystem.** The nearest thing to "mounting a partition" is
`SETTINGS_InitEEPROM()` reading fixed byte offsets over SPI: `0xA008` for the power
save byte, `0x0E70` for the VFO indices, and so on. No metadata, no directory, no
checksum -- just an address that the code and the data both have to agree on. When
a setting reads back wrong, suspect the offset before suspecting the transport.

The ~15 s to reach the main loop is emulation overhead. A real radio is up in about
a second.

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

## The flash bugs: four faults, one symptom

"The frequency will not change" and "flash forgets everything after power off"
looked like two complaints. They were one root cause plus three real bugs found on
the way, all in this file. Worth reading before touching SPI, DMA or the flash
model, because each was invisible from the layer above.

1. **DMA used the wrong address space** — the actual cause. It moved bytes through
   `address_space_memory`, which cannot decode this SoC's memory at all: the
   container region is handed only to the ARMv7M core and never registered with
   global system memory. Reads returned `MEMTX_DECODE_ERROR` and zeros; writes went
   nowhere. DMA now runs over an `AddressSpace` built on the container.
2. **Page program did not wrap.** Real SPI NOR latches only the low address bits,
   so a burst past the 256-byte page boundary continues at the start of the same
   page. The model walked straight through, and a 512-byte burst at 0x008F00 (which
   the firmware really does send in one CS assertion) spilled into 0x009000.
3. **DMA started too early.** Transfers ran when a channel was enabled, but on
   hardware they start when the peripheral raises its request. The driver arms both
   channels, then enables SPI, then sets TXDMAEN — so firing at arm time clocked
   the bus before the read command had been sent.
4. **DMA channels ran one after another.** SPI is duplex and the driver pairs a
   dummy-feeding TX channel with a data-collecting RX channel over one transfer.
   Running them in sequence let TX finish before RX ever sampled the bus.

Any one of them zeroed the sector holding per-band VFO frequencies.
`RADIO_ConfigureChannel` substitutes a band's lower limit only for `0xFFFFFFFF`, so
a stored zero was taken literally and clamped to `BX4819_band1_lower` — 18 MHz.
That is the whole explanation for a typed frequency always reverting.

`tools/test_freq_entry.py` and the `MUST_NOT_CHANGE` guard in
`tools/test_flash_persist.py` exist to catch a regression in any of the four.

### What made this hard to find, and what to do instead

**Instrument the model, not the guest.** The frequency input box times out after
`key_input_timeout_500ms / 3`, about 2.5 s, and a gdb attach takes roughly 3 s. So
probing between digits clears the box, and the run reports a failure that the
measurement caused. This produced at least three confident wrong conclusions,
including "the firmware saved band 0" when the box had simply emptied. Add an
`fprintf` to `qemu/py32f071.c` and read stderr instead — the guest never stops.

**Never cap a diagnostic log before you know the shape of the data.** A probe
limited to the first six transactions showed only `0xFF` payloads, which supported
exactly the wrong conclusion. Without the cap, the writes that mattered were
obvious.

**Check that the build succeeded before believing a test.** A failed `ninja` leaves
the previous binary in place and the test still runs, so a stale build silently
answers the question. Two rounds of results were meaningless this way. Grep the
build output for `FAILED` and `error:` and stop if either appears.

**Reset the flash image between runs.** `assets/flash.img` is written by every
session. A test that starts from it may find its work already done — which shows up
as "the image is byte-identical", indistinguishable from broken persistence. Start
from `assets/pristine/`, and power the emulator off *before* restoring, since
shutdown flushes the old in-memory image back over the file.

**Do not hand-compute struct offsets.** The ELF has no DWARF and the structs
contain enums whose size cannot be assumed. Offsets computed by hand produced
`KEY_LOCK=4` and `TX_VFO=11`, neither of which is a possible value. Either use a
symbol that `nm` reports and whose type is unambiguous (`gInputBoxIndex` is a plain
`uint8_t`), or locate a field by behaviour — toggling the keypad lock with a long
`F` press and diffing the region found `KEY_LOCK` at `gEeprom+0x12` in one step.

**Read your own probe output carefully.** One probe printed `phase` before it was
incremented, which made a correct address decoder look off by one byte. Replaying
the logic in Python cleared it up; without that, a working implementation would
have been "fixed".

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

## The BK4819, and where modelling it stops

The register interface is modelled (`TYPE_UVK5_BK4819`): the bit-banged three-wire
bus is decoded, registers read back what the firmware wrote, and the ones it reads
without writing return plausible values. Wiring is CS on PF9, SCL PB8, SDA PB9 with
both directions connected. `tools/test_bk4819.py` inspects the register file over QOM.

This is what it fixed: RSSI used to read hard zero at 18 call sites — -160 dBm — so
the S-meter showed empty and squelch and scan logic evaluated a dead band. The main
screen now comes up on 400 MHz rather than the 18 MHz floor, because band setup is no
longer reading zeros.

Two constraints are not negotiable, both from untimed spin loops in the firmware:

- **REG_0C bit 0 must stay clear.** `app/app.c:910` and `:1417` are
  `while (BK4819_ReadRegister(BK4819_REG_0C) & 1u)` with no timeout at all. A stuck
  bit hangs the guest; it does not degrade.
- **A soft reset must re-seed the measurement registers.** `REG_00` bit 15, which
  `BK4819_Init` issues first, would otherwise leave them zero — real hardware keeps
  measuring. Not hypothetical: the first test run decoded 48 registers correctly and
  still reported RSSI as 0 for precisely this reason.

### Running the tests

    bash tools/run_tests.sh        # everything
    bash tools/run_tests.sh -q     # unit tests only, no emulator, ~15 s

Use the runner rather than pasting individual commands. It checks the build first and
**stops** on failure, which matters more than it sounds: `ninja` leaves the previous
binary in place when it fails, so tests run happily against code that was never
compiled. That produced two rounds of entirely meaningless results before the habit
stuck.

It also rebuilds only when `qemu/py32f071.c` differs from the copy in the QEMU tree, so
a plain test run does not pay for a rebuild it does not need.

The runner checks *itself* first, via `tools/test_run_tests.sh`. Its first version wrote

    if "$@" 2>&1 | sed 's/^/    /'; then

which tests **sed's** exit status, not the test's — so every test would have counted as
passing whatever broke. Hence `PIPESTATUS[0]`, and a self-check that asserts a failing
test really is counted and named. A runner that cannot fail is worse than none, because
it gets trusted. Test output also goes through `tr -cd` first: gdb-driven tests emit
stray bytes that make the log a "binary file" to grep, which swallows the summary.

Emulator tests boot their own QEMU on private ports and take 20-30 s each, so they do
not disturb a running `run.sh` or web UI session.

### Counting distinct frames proves less than it looks

Worth knowing before writing any test that watches the screen.

Once the receiver reports a varying RSSI, the meter and its dBm readout redraw
constantly. So "are consecutive frames different" returns yes on a **completely parked
radio**. A first attempt at checking that scanning still worked scored 8/8 distinct
frames and established nothing at all.

Compare the rows that answer the actual question instead. The framebuffer is 128x64 as
8 pages of 128 bytes, page *p* covering rows 8p..8p+7:

    page 0      status line
    pages 1-2   upper VFO, large frequency digits
    page 3      upper VFO sub-line
    pages 5-7   lower VFO

`tools/test_scan.py` compares pages 1-2, which only change when the radio retunes: 6
distinct tunings over 6 samples. That matters because an always-busy receiver is a
plausible way to stall a scan, and the S-meter work made the receiver always busy.

Page 4 is *not* the meter row, incidentally — it stayed byte-identical across all six
samples while the frequency changed.

### PTT, and the transmit level bar

PTT is not a matrix key. `GPIO_IsPttPressed` reads PB10 directly
(`driver/gpio.h:31`, active low), so the model gives it its own GPIO line rather than a
column/row intersection, exposed as a boolean `ptt` property on the keypad device.

That is what makes the transmit level bar reachable. `app/app.c:1700` draws it only
while `gCurrentFunction == FUNCTION_TRANSMIT` and `gSetting_mic_bar` is set — the
latter is `Data[7]` bit 4 at flash `0xA0A8` (`settings.c:423`), and blank flash reads
`0xFF`, so it is already on. The level itself comes from `REG_64` via
`BK4819_GetVoiceAmplitudeOut`.

**Treat the release as the important half.** A stuck PTT leaves the emulated radio
keyed, and every later test then runs against a transmitting radio. The web UI releases
on `pointerleave`, `pointercancel` and `pagehide`; `/api/release-all` clears PTT
explicitly, because an empty `press` does not touch it; and the endpoint rejects
non-boolean bodies so `{"held": "false"}` cannot key the transmitter by truthiness.
`tools/test_ptt.py` asserts the release, not just the press.

One trap worth knowing if you add another non-key button: the browser wired handlers
over `.key`, which matched the PTT button as well, and it has no `data-key` — so it
would have sent the key `"undefined"`. Use `.key[data-key]`.

### Reads were shifted one bit, and it hid everything else

Fixed in `ad88ee1`, but worth reading because of how long it stayed invisible.

Register reads arrived shifted one place left: seed `REG_0C` with `0x1248` and the
firmware received `0x2490`. Each firmware bit is read/raise/lower, so the eighth
command bit is followed by a falling edge before the data phase — and the model was
treating that edge as a data clock, shifting bit 15 away before the guest sampled it.

Why nobody noticed: **writes were always fine**, 52 registers held exactly what the
firmware wrote, and the register the firmware polls hardest was legitimately `0`.
Reading zero and getting zero looks like success. Verifying a read path requires a
register with a known *non-zero* value — `REG_3F` is `0x0C0C`, `REG_78` is `0x2F5B`.

`tools/test_bk4819_readback.sh` guards it now: seeds `REG_0C` (read ~1700 times per
30 s, so a sample is guaranteed) with a value carrying bits in both halves, and names
the shift direction on failure. Bit 0 is left clear deliberately — with it set the
firmware enters an untimed acknowledge loop, and that test is about alignment only.

This also invalidated four earlier diagnoses. Attempts at the squelch interrupt had
the model raising `REG_0C` bit 0 while the firmware received bit 1, so

    while (BK4819_ReadRegister(BK4819_REG_0C) & 1u)

was never true and 1719 polls saw a flag the guest could not act on. Every one of
those rounds was blamed on timing or gating. **When several independent attempts fail
the same way, suspect the shared transport, not the logic on top of it.**

### The squelch interrupt and the S-meter: five attempts, then it worked

**This works now** (`e6cebed`) — skip to the end for the conclusion. The four failed
attempts are kept because each produced a confident wrong diagnosis, and the pattern
of how they failed is the useful part.

Scanning worked early on: long-press `*` and the frequency really does step, 6 distinct
frames over 7 seconds. The S-meter did not, because `ui/main.c:2370` only draws it when
`FUNCTION_IsRx()`, and that needs `gCurrentFunction` in a receiving state — which takes
the chip reporting a squelch opening, not just a healthy RSSI.

The mechanism looked clear: `REG_0C` bit 0 says an interrupt is pending, the firmware
writes `REG_02` to acknowledge and reads it back for the flags, and `sqlFound` is bit 3
(the bitfield is at `app/app.c:915`). Both the bit choice and that reading of the
mechanism turned out to be wrong.

I implemented it — raise `sqlFound` once when the firmware enables interrupts — and
**backed it out**. The guest kept running, but `REG_0C` bit 0 was still set afterwards:
the firmware had not collected the interrupt. That is a latent hang, because
`app/app.c:910` and `:1417` spin on that bit with no timeout, so any path that reaches
them with the bit stuck never returns. Shipping a model that leaves a hang armed is
worse than shipping one without an S-meter.

**Second attempt, and the actual reason.** Tried again, this time evaluating squelch
when the firmware *polls* `REG_0C` rather than when it configures the chip — which
fixed the original mistake, since the startup sequence writes `REG_3F` as `0x0000`
then `0x0C0C` three times over, so a flag raised on the enabling write was disabled
again before anyone read it. Also corrected the threshold field: the RSSI open level
is `REG_78` bits 15:8 at 0.5 dB/step against `REG_67`'s 0.25 dB/step, not anything in
`REG_4E` (those low bits are the *glitch* threshold, and using them meant squelch
never opened at all).

With that right, everything on the chip side lines up — measured `en=0x0C0C`,
`rssi=0x01E0`, threshold 94, and `REG_0C` correctly returning 1. The firmware still
never acknowledged. The reason is not on the chip side at all:

    gCurrentFunction=5 (FUNCTION_POWER_SAVE), gRxIdleMode=1

and the gate is `app/app.c:1697`:

    if (gCurrentFunction != FUNCTION_POWER_SAVE || !gRxIdleMode)
        CheckRadioInterrupts();

Both halves are false in that state, which looked like the answer: no
`CheckRadioInterrupts`, so nothing to collect the flag.

**That explanation is wrong, and the test that disproves it is worth keeping.**
`app/app.c:1374` refuses power save outright when `BATTERY_SAVE == 0`, and the byte
lives at flash `0xA00B` (blank flash reads 0xFF, which `settings.c` clamps to 4 — the
deepest setting, which is why the emulator idles there). Patch that byte to 0 and:

    BATTERY_SAVE=4:  fn=5 idle=1   polls=2161  acks=0
    BATTERY_SAVE=0:  fn=0 idle=0   polls=2161  acks=0

The gate now passes and the acknowledge count is still zero. A gdb backtrace on
`BK4819_ReadRegister` confirms the loop really is running —
`CheckRadioInterrupts` is inlined into `APP_TimeSlice10ms`, and that is the caller:

    #0  BK4819_ReadRegister
    #1  APP_TimeSlice10ms
    #2  Main

So the firmware reads `REG_0C`, gets 1, and does not write `REG_02`. Whatever
suppresses that is inside the inlined loop, past the gate. Gating the model on
`REG_30` (zeroed by `BK4819_Sleep`) does not help either — the chip is awake when the
model is asked while the firmware still reports `gRxIdleMode=1`.

**Resolved in `e6cebed`.** The meter reads: `-53` dBm, `+40` over S9, nine of thirteen
segments, `MONI`, and a running receive timer. The numbers agree — S9 is −93 dBm on
UHF, so −53 really is S9+40.

Three things had to be right, and the order they were found in was the difficult part.

*The flag is `SQUELCH_LOST`, bit 2.* Per `app/app.c:1027`, "squelch lost" is what sets
`g_SquelchLost = true`, meaning a signal is present. `SQUELCH_FOUND` reads like "found
a signal" and means the opposite. Bit definitions are in
`App/driver/bk4819-regs.h:290`.

*Announcing has to be rate-limited* — here every 64th poll. Announce once and the
firmware collects it during startup, before the flag leads anywhere. Announce on every
poll and the request bit is re-armed inside the firmware's own collection loop, which
uses `REG_0C` as its condition and has no timeout, so it never exits. Periodic
satisfies both: the loop always drains, and the news repeats until it matters.

*The way in is not the interrupt at all.* The radio idles in power save and does not
act on squelch there — which is why a breakpoint on `BK4819_GetRSSI` never fired.
`ACTION_Monitor` skips squelch entirely: `app/app.c:482` picks `FUNCTION_MONITOR` over
`FUNCTION_RECEIVE` whenever `gMonitor` is set, and `settings.c:263` defaults an
out-of-range stored action to `ACTION_OPT_MONITOR` — which blank flash (`0xFF`) is. So
**SIDE1 short-press engages monitor on a pristine image**:

    before:  fn=5 idle=1 monitor=0      (FUNCTION_POWER_SAVE)
    after:   fn=2 idle=0 monitor=1

Gate on `RX_DSP` (`REG_30` bit 0) rather than the whole register being zero: TX and
tone paths leave other bits set with `RX_DSP` clear and would otherwise look like a
live receiver.

`tools/test_smeter.py` covers the path end to end and compares lit-pixel counts rather
than matching pixels, so an unrelated UI change cannot produce a mysterious failure.

Four measurement mistakes made this take far longer than the code involved. All four
produced a confident, wrong conclusion:

- **Sampling PC at the `REG_0C` read** lands in `BK4819_WriteU8`, the bit-banging
  helper, not the caller. Sampling LR is no better: `BK4819_ReadRegister` calls
  `BK4819_ReadU16`, so LR points back inside the reader. Use a breakpoint and a
  backtrace.
- **A probe printing `shift_out` before the assignment** reported `0000` for a value
  about to be sent as `0001`. Nearly became "the model sends the wrong value".
- **`BK4819_ReadRegister` returning 0x0 for REG_0C** looked like a broken read path,
  and I changed the bit timing on the strength of it. But REG_0C legitimately holds 0
  in the committed build — there is nothing to raise it. A register read returning the
  register's actual contents is not evidence of anything. Check against a register the
  firmware demonstrably wrote (`REG_3F` is `0x0C0C`, `REG_78` is `0x2F5B`).
- **`nexti` after a breakpoint** landed somewhere unrelated and reported `r0 = 0`,
  which fed the same wrong conclusion. `finish` gives the real return value.

Also note `gdb` cannot call guest functions on this target (`print
BK4819_ReadRegister(0x3f)` errors out), and there is no `gCurrentRSSI` global to read
— RSSI is used and discarded. Breakpoint plus `finish` is the only way to see what the
firmware actually received.

**Where it stops.** This models the register interface, not the radio. It reproduces
what the firmware *commanded* — frequency, power step, carrier keying in time — never
the analogue result: keying envelopes, spurious emissions, sensitivity.

That is not a gap to close later. The chip has no public datasheet, so its driver is
the only specification available, and a driver tells you which registers were
written, never what left the antenna. Those questions need a real radio and a
spectrum analyser. Do not let anyone conclude otherwise from a passing emulator test,
including the one added here.

Timing is also deliberately wrong — see the SysTick section in README.md. Fine
for menus and control flow; useless for signal timing.

## Serial, both directions

Works, and `tools/test_serial_rx.py` proves it by speaking the real protocol:
`0x0514` hello gets a `0x0515` ack, and `0x051B` returns the requested EEPROM bytes.
Attach with `-serial unix:/path/to.sock` or any other chardev; it defaults to
`serial0`.

Three things had to line up, and each failed silently on its own:

- **USART1 needs a chardev.** It is otherwise a register stub with nowhere for
  incoming bytes to come from.
- **DMA has to service USART, decrementing `CNDTR`.** `driver/uart.c` never reads
  DR. It receives over a circular channel and locates new data with
  `sizeof(UART_DMA_Buffer) - LL_DMA_GetDataLength(...)`, so a count that never moves
  means a buffer that always looks empty, no matter how many bytes arrived. The
  service runs on a `CNDTR` read, which is exactly where the driver looks — no timer
  needed, and nothing can be delivered before the guest asks for it.
- **DR writes must also reach the chardev.** They used to go only to stderr. A host
  tool would send a command, the firmware would answer, and the answer went
  somewhere the tool could not see. That is indistinguishable from being ignored,
  and it cost a debugging round: the first run of the new test reported "no reply at
  all" alongside *zero* bytes of boot output, which looked like broken receive when
  in fact transmit was fine and simply invisible.

Channels also record the length they were programmed with, because `CNDTR` counts
down and the write offset has to come from the difference.

## If you add a peripheral

1. Read the register layout from the CMSIS header
2. Model only what the firmware actually touches; the logging catch-all
   (`py32-stub`) shows you what that is
3. Watch for spin loops: any flag the firmware polls must be able to change, and
   write-1-to-start bits (like `ADC_CR2_CAL`) must never be stored set
4. Rebuild, run, and check with `tools/where.sh` that the firmware moved past
   where it used to stop
