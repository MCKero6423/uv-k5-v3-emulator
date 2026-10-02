# Working on this repo

Notes for whoever picks this up next. Focused on what is not obvious from the
code, and on mistakes that already cost time here.

*中文：[AGENTS.zh-CN.md](AGENTS.zh-CN.md) · the two are kept in step; change both.*

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

Boot time is emulation overhead. Measured on this machine: first pixels at ~1.6 s and
a drawn main screen at ~3.6 s after QEMU starts, which is the "~5 s" the README quotes.
A real radio is up in about a second.

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
    python3 tools/uvk5_buffers.py --qmp 127.0.0.1:4444   # this firmware's addresses
    python3 tools/screenshot.py --frame-addr 0x... --status-addr 0x... \
        --port 1234 --out screen.png

Screenshot addresses move between firmware builds, and `screenshot.py` reads guest RAM, so
it needs them. `tools/uvk5_buffers.py` finds them in the running firmware by matching the
display controller's memory against SRAM -- the images built here are program-header-only
ELFs with no symbol table, so `nm` has nothing to read for them (a fully linked ELF does).

Rebuild after editing the machine:

    cd $QEMU/build && ninja qemu-system-arm    # ~10 s incremental

After any change near the keypad or the GPIO wiring, run the regression test. It
boots its own instance on private ports, so it does not disturb a `run.sh`
session:

    python3 tools/keypad_test.py

There is also a browser UI, which is usually the quickest way to poke at the
firmware by hand:

    python3 tools/webui.py            # no addresses: the page draws the panel's memory

Two things about it that matter when working on this repo:

- **It holds the QMP socket for its lifetime**, so `key.py` cannot run at the same
  time. The socket accepts a single client.
- **It reads frames with QMP `memsave`, deliberately.** Not `pmemsave`, which
  takes a *physical* address and silently returns zeros for `gFrameBuffer` --
  a blank screen with no error. And not gdb, which halts the guest on every
  attach: that stutters the stream and perturbs key debounce timing.

Its tests: `tools/test_uvk5_*.py` and `tools/test_webui.py` need no emulator,
`tools/test_webui_e2e.py` boots its own.

A firmware can also be loaded from the page rather than from the command line:
`POST /api/firmware` takes the image as its request body, stores it in
`work/firmware/`, and boots it -- restarting the emulator if it was running. The
image's **shape** is read out of the image (`tools/uvk5_image.py` on the host,
`uvk5_sniff_app_offset()` in the machine): an *application* image is linked for
`0x08002800`, a *full-flash* image starts at `0x08000000`, and address 0 has to alias
the matching base. Getting that wrong is silent -- the image lands 0x2800 bytes off and
the first fetch reads whatever data is there -- which is why it is not a flag and not a
file-name convention. A file that is not an image is refused without disturbing the
running radio.

Two things about that path are worth knowing, both found the hard way:

- **The flash image travels in the environment, not in `-M`.** Through the launcher,
  QEMU rejected `-M uv-k5-v3,flash-image=...` with "unsupported machine type": the
  identical argv started fine when run by hand, `-M help` in the *same* context listed
  the machine, the argv `repr` was clean, and the environment diffed down to nothing
  conclusive. The property still works when it is set, so both are supported; the
  launcher now passes the bare machine name plus `UVK5_FLASH_IMAGE`, which the model
  reads as a fallback. The root cause is unexplained -- do not "clean this up" without
  re-testing a power-on from the page.
- **The screen is read from the display controller, not from guest RAM.** The panel
  model keeps the controller's own display RAM (8 pages of 128 columns), and the web
  page renders that, so the picture is right for *any* firmware -- builds sharing an
  ancestor still differ in their display logic, and the multi-system release keeps its
  image somewhere else entirely. Do not apply the driver's `0xA1` segment reverse on
  top of the data: measured at one instant against the guest's own framebuffer, 8153 of
  8192 pixels agree with no mirroring and 6557 with it. `memsave` of `gFrameBuffer`
  remains the fallback for an emulator built without the panel model.

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

The same trap one layer further out: **a redirect can change the encoding.** Three
probe runs under `qemu ... 2> probe.log` reported zero SPI transfers, zero flash
reads and zero chip-select changes, and "the firmware never touches SPI" was written
down as a finding. PowerShell 5.1 writes `2>` as UTF-16LE, so every ASCII line a
probe printed had a NUL between each character and a `startswith("LCDW")` filter
could never match it. Decoding the same file as UTF-16 showed a complete ST7565 init
sequence and 48 distinct settings reads. Before believing an empty probe, check that
the probe *can* be seen: read the file, count its bytes, or write it from `cmd /c`,
which does not re-encode.

**QMP `pmemsave` is physical, `memsave` is virtual.** The framebuffer symbols are
CPU virtual addresses, so `pmemsave` on `gFrameBuffer` returns a block of zeros
and reports success -- a blank screen with nothing logged anywhere. The web UI was
built on `pmemsave` first because a timing benchmark said it was fast; the
benchmark never checked the *contents*. Measure the thing you actually care
about: the bug surfaced only when a rendered frame came back with 0 lit pixels
where the gdb path reported 1693.

### The page is generated by an f-string, so check the script it serves

The web UI is one f-string. A stray backslash in a JavaScript string literal therefore
produces a page whose **whole** `<script>` fails to parse, and the only symptom is that
the status line sits on "connecting..." forever while every endpoint still answers
`curl` correctly. That shipped once: `.split('\\')` came out as `.split('\')`, an
unterminated string, and the page was dead from a browser's point of view while every
test passed.

`test_webui.TestPageScriptParses` extracts the served script and runs `node --check`
on it now. Test the artifact you ship, not the code that builds it.

### A probe needs to be able to see the thing it is looking for

Three separate rounds of "the firmware never touches the flash" were all the probe's
fault, and each one looked like a finding:

- A probe filtered on `address >= 0x0C0000`, so every frame without an address -- write
  enable, and the sector erase that actually erases -- was dropped. "0 writes" was the
  filter, not the firmware.
- A handshake was given 1.5 s to answer and the firmware needed about 4 s to enter its
  serial mode. "No reply" was the timeout.
- Why a probe can be invisible at all: PowerShell 5.1 writes `2>` as UTF-16LE, so every
  line had a NUL between each character and no filter could ever match.

Before believing an empty probe, make it print something you know is there.

### The flash model wrote the whole image back on every chip-select release

2 MB per release is nothing for a settings save. It is ruinous for the multi-system host
interface, which programs a slot 200 bytes at a time
(`App/app/uart.c`, `0x0724`, 12-byte header plus data): one 114 KB firmware became ~600
full rewrites, on the vCPU thread, and the *guest* -- and every host tool talking to it --
waited for each one. Measured: a single 64-byte slot write took six seconds.

The first fix was a 200 ms time-based throttle, which was wrong: it trades a slow test for
silently losing the last window of writes on a hard kill. The model now tracks the changed
byte range and writes only that, in place, which is both fast and the more faithful
behaviour -- real NOR does not make an interrupted program atomic. The exit notifier still
writes everything.

### The serial link carries the firmware's own screen stream

`K5Viewer` streams the display out of USART1. A host client that reads only while it is
waiting for a reply backs the socket up, and the **guest then blocks** writing to it: a
slot transfer started losing replies partway and a single small write took seconds. The
fix is a reader thread that drains continuously and lets the waiting code look at what has
been reassembled -- on the radio's side the same rule applies to whatever talks to it.

Also on that path: the firmware's receive buffer is 256 bytes
(`App/driver/uart.c: UART_DMA_Buffer[256]`), so a 240-byte chunk plus framing overran it
and every frame was dropped in silence; 200 fits. And the serial *session* times out after
~6 s without a `0x0514` (`gSerialConfigCountDown_500ms = 12`), which a long transfer
crosses -- measured by re-handshaking: the writes resume immediately.

With those four, `tools/uvk5_slots_serial.py` writes a slot through the firmware itself and
the device validates the CRC.

### A serial client that connects after boot misses everything

`-serial tcp:host:port,server=on,wait=off` **discards** what the guest writes until a
client connects. The firmware prints its banner in the first seconds, so a client that
attaches "once QEMU is up" -- four seconds later, say -- sees an empty port and it looks
exactly like a guest that never booted. Four rounds of "the bootloader sends nothing"
were that, not the bootloader.

Connect first, then let the guest run. The same trap applies to the 0x0518 flood a
bootloader emits while waiting for a host: it is continuous, so a late client *does* see
it -- which is why the mistake survived as long as it did, showing up only for the
one-shot startup output.

Two related habits, both learned here:

- **Check that the probe can see something you know is there.** A USART register probe
  reported zero accesses, and the obvious reading was "the bootloader never programs the
  USART". The application, run through the same probe, reported 2239 -- which is what
  said the probe worked and the bootloader really was silent.
- **When a documented observation stops reproducing, treat the note as unverified.** The
  bootloader's Moto-mode flood was written down from a run that is no longer reproducible
  with the current build and image. Re-derive it before relying on it.

### A bare host:port is not a scheme

`uvk5_socket.connect` split its argument on ":" to find a scheme, so the endpoint the
supervisor, the web UI and the README all pass -- a plain `127.0.0.1:4444` -- became
scheme `127.0.0.1`, empty port, and an empty host. The connect then sat there until its
deadline. What that looks like from outside is "the page cannot power the emulator on",
while a QEMU started by hand with the identical command line answers QMP in half a
second, and the guest boots happily in the background the whole time.

Two things made it hard to see: the same helper also accepts `tcp:host:port`, so the
tests that used that form passed, and the failure is a *timeout* rather than an error, so
it reads as a slow or wedged emulator. `test_uvk5_socket` now covers every form that
reaches `connect`, and the lesson generalises: **when a helper accepts several spellings,
test each one** -- the one nobody tests is the one everybody passes.

The other half of the same fault was real and independent: QEMU's stderr had to be
drained from the moment it started. The firmware streams its display down that pipe, 64 KB
fills in about a second, and QEMU blocks writing to it -- which stops its main loop, so
QMP never answers either. Measured both ways: with the pipe drained, QMP accepts in 0.5 s;
with it left unread, never.

### A register you swallow is a hang the next program waits on

The factory bootloader would not start at all: no serial output, and the PC probe sampled
`0x08000f38` on every single sample. That address is inside the bootloader, and the two
instructions there are

    0x0f38: ldr  r2, [r1]      ; r1 = 0x40022000, the flash controller
    0x0f3a: lsls r2, r2, #30
    0x0f3c: lsrs r2, r2, #30   ; r2 = ACR & 3, the LATENCY field
    0x0f3e: cmp  r2, #1        ; waiting for one wait state
    0x0f40: bne  0x0f38

The flash controller model added earlier treated `ACR` and `OPTKEYR` as writes to
ignore -- it returned early, so the generic path never stored them, so `ACR` read back
zero forever and the bootloader spun before it ever configured its UART. Returning `false`
lets the value be stored, and the PC immediately moved into the application (`0x08013ea0`)
and serial output appeared.

Two lessons, both general:

- **A write-only register is still a register.** The application never read `ACR` back, so
  its absence was invisible for as long as only the application ran. The next program to
  touch the same peripheral found it at once.
- **"It used to work" is a bisect instruction.** The bootloader's Moto-mode flood had been
  observed before the flash controller was modelled, and stopped reproducing afterwards.
  The right move was to ask what changed between those two runs, not to distrust the
  earlier note.

### Moto/DFU: the entry is a build flag, not a key

The factory bootloader in the first 10 KB does contain a Moto DFU handler at 38400 baud, and
the emulator runs the bootloader correctly. It is nevertheless unreachable from outside, and
the reason is in the bootloader's own code:

    0x13f2  ldrb r0, [r4, #0]     ; r4 = 0x20000020, a byte in SRAM
    0x13f4  cmp  r0, #1
    0x13f6  beq  ...
    0x13f8  cmp  r0, #2
    0x13fa  beq  ...
    0x13fc  cmp  r0, #3
    0x13fe  bne  ...              ; anything else keeps waiting
    0x140e  bl   0x06f0           ; only mode 3 gets here: the DFU handler

SRAM survives a soft reset and a power cycle does not, so that byte can only be set by a
program that then resets. In the application that is `overlay_FLASH_RebootToBootloader()`,
reached from the serial command `0x05DD` **only when the build defines `ENABLE_OVERLAY`**;
without it the same command is a plain `NVIC_SystemReset()`. Confirmed by sending `0x05DD` to
a running radio: no `0x0518` follows, and the PC never leaves the application.

Four ways in were ruled out by measurement, not by reading: PTT alone (the firmware's own
`BOOT_GetMode()` needs a second key), PTT+SIDE1/SIDE2 and MENU (the application's special
modes), a host byte inside the boot window including the `0x0530` handshake, and `0x05DD`.
Run alone with no valid application the bootloader does not enter DFU either: it stops in one
of the six self-branches at `0x080000dc`, which are hang slots, not a wait for input.

The general lesson: when a firmware's mode is chosen from a byte in RAM, the trigger is not an
input pin -- it is whatever wrote that byte before resetting. Find the writer in the source
(`0x05DD` here) and the `#ifdef` around it, and you have the whole condition.

### Two pixels bugs behind "the other firmware looks shifted"

Both were found by making the page *say where its picture came from*, and both had been
surviving because the wrong output looked plausible.

**The fallback that quietly drew every frame.** `uvk5_stream.py` used `STATUS_BYTES`
without importing it, so the panel branch raised `NameError` on every frame and a bare
`except Exception: pass` swallowed it. Every screen the page drew came from guest RAM at
one firmware build's addresses: right-looking for that build, plausible and offset for any
other. Found by reporting the source and the reason (`/api/panel` answered
`source: framebuffer, note: NameError: name 'STATUS_BYTES' is not defined`). With the panel
path working, the page's `/frame.png` matches the controller's own memory 8192/8192;
before the fix it was 5594/8192 against the same memory. `tools/test_uvk5_stream.py` now
asserts that the panel wins when it is reachable, and that a fallback is announced with its
reason.

**The column counter wrapped at 128 instead of 132.** The controller has 132 column
drivers and the glass shows 128 of them starting at column 4, which is why the model stores
pixels at `col - 4`. The counter was masked with `& 0x7f`, so addresses 128..131 came back
as 0..3, fell outside the `col >= 4` store, and were dropped: **every row lost its last four
pixels**. The battery icon lives in exactly those columns, so the symptom was a battery in
the wrong place and a picture that "looked shifted" on builds that draw to column 127 --
while the localised build, whose rightmost four columns are blank anyway, looked fine. That
is why this read as a firmware-specific problem. Measured, before and after: filling a page
with `0xFF` left columns 124..127 blank; now they light (10/10/12/7 lit across them), and
the same firmware's frame matches the panel memory 8192/8192.

The lesson in both cases is the same one this file keeps repeating: **a path that silently
substitutes a different source turns a hard error into a plausible wrong answer**, and a
byte that is off by four is invisible until something that matters lives in those four
columns. Report the source, and test that the preferred path is actually taken.

### The screen buffers are found, not hardcoded

The flag was `--frame-addr 0x200012BE --status-addr 0x2000163E` -- one build's
addresses, in the launcher, as a default. Pointed at another firmware that reads
somewhere else, the picture is plausible and wrong: measured, the build the user
actually flashed keeps its buffers at `0x2000129E` / `0x2000161E`, exactly 32 bytes
earlier, so every line landed 32 bytes off. That is what "the other firmware looks
shifted" was.

Nothing needs to be assumed. The firmware images here are minimal ELFs -- one program
header, no section headers, no symbol table (tools/bin2elf.py writes them) -- so there
are no `gFrameBuffer` symbols to read, but there is behaviour: the firmware's own
buffers hold the same bytes the controller holds, because that is where the driver
copied them from. `tools/uvk5_buffers.py` slides the controller's memory through SRAM
and keeps the offset that agrees; it reported 1024/1024 bytes and the right pair of
addresses for the exact file the user flashed.

So `--frame-addr` and `--status-addr` are optional now, `work/run-webui.ps1` no
longer passes them (or any machine-specific path), and the page reports what it found:

    buffers: {"frame": 0x2000129E, "status": 0x2000161E, "how": "sram search",
              "score": 1024, "total": 1024}

Two habits from this, both already in this file in other words: **a default that names
one machine's or one build's value is a bug waiting for a second build**, and **when
there are no symbols to read, ask the thing itself** -- the bytes in the buffers are
the answer, and they can be found by matching rather than guessed.

The panel path needs none of this, and is what the page draws from: the controller's
memory is the screen for every firmware. The addresses only serve the guest-RAM
fallback, which is why a failed search is reported and does not stop anything.

### The page must report what the device says it is running

The page knew only the file it had been handed, and those are different questions. A build
called `f4hwn.fusion.bin` can report `EGZUMER+F4HWN v6.0.0.CN` -- that one does -- so
"it still boots the CN version" was the page describing its input, not the radio. Worse,
with the multi-system release a committed external slot plus a valid state marker makes the
factory bootloader reflash the internal flash from that slot on every power-on: the uploaded
image is overwritten before it runs, and the page keeps naming a file that never executed.

The firmware answers the question itself. It prints `UV-K5 Firmware, ...` on USART1, the
machine tags it SERIAL, and `tools/uvk5_banner.py` reads it back. `/api/firmware` now
returns `running: {banner, matches_uploaded, note}` and the page shows `device reports: ...`,
flagging it only when the running version is not in the uploaded image at all -- because a
file name that differs from a banner usually just is a different name, and a hint that cries
wolf gets ignored.

Reading that banner back also exposed a bug of my own. It had stopped reaching the log
entirely: `_start_stderr_pump` was rewritten to read the pipe in 64 KB chunks so QEMU could
not block on it. That kept the deadlock fixed and silently lost the other half -- nothing
arrived until 64 KB had accumulated, and the banner is forty bytes. It reads lines again,
and still starts before anything waits on QEMU. **A rewrite that preserves the property you
were fixing while losing another is the expensive kind**, and this one hid because the log
still "worked" for the binary screen stream.

### Overlay apps are a flash region, and the page can write it

The Labs edition runs small overlay apps (Tetris, Breakout, Plasma, Cube3D, Beam, Beacon,
FoxHunt, BroadcastFM). Upstream installs them from UVStudio over WebSerial; here they are
bytes in the external flash image, which the page already owns -- so no serial protocol and
no browser permission are involved, just the same bytes at the same offsets.

The layout is the firmware's own, read out of the header it compiles rather than inferred
(`App/apps/app_overlay.h`): `APP_REGION_BASE 0x00102000` -- right behind the two state
markers -- `APP_SLOT_STRIDE 0x2000`, `APP_CODE_OFFSET 0x1000`, 16 slots. A slot holds a
64-byte `app_header_t` (magic `FAP1`, zlib CRC-32 over the code, name, version, vma
0x20000280, capabilities) and its code one 4 KiB sector later. `tools/uvk5_apps.py` parses,
validates, lists, installs and erases them; `tools/test_uvk5_apps.py` covers the refusals.

Two things are worth knowing. **The 64-byte header is shared with the multiboot slots**:
`FMB1` means firmware, `FAP1` means app, which is why "install app to slot 1" and "put a
firmware in slot 1" touch the same external flash and the power-on menu lists both. And the
region is found the same way the screen buffers were: from the firmware's own constants,
not from a guess -- the first version of the header here was 60 bytes because a field
(`vma`) was missing, and the real `Beam.app` bytes said so immediately.

The page writes them through three endpoints (`GET /api/apps`, `POST /api/apps/<n>`,
`POST /api/apps/<n>/erase`), all of which edit the flash image the emulator boots from --
so this is page-operable with no WebSerial, no browser permission and no serial protocol. The page shows a table of the 16 slots beside the firmware slots, with a file picker and an Erase button per slot.

Two things measured while wiring it up, both of which changed the code:

* **The region can already hold something.** On a real image every one of the 16 slots read
  back as data that is neither empty nor an app -- the factory resource block of a localised
  build overlaps `0x102000`. Installing there would have destroyed it in silence, so
  `install` now refuses a slot that holds anything other than an app unless it is asked to
  overwrite (`--force` on the tool, `?force=1` on the endpoint). The refusal names what is
  there and where the slot is.
* **The edit does not land in the file you passed.** `_edit_flash` copies the image and
  repoints the slot at the copy, deliberately, so a running emulator cannot have the file
  under it rewritten. A first test asserted the original file had changed, saw zero
  differing bytes and read as "the install did nothing" -- the bytes were in the copy. Check
  `FlashSlot.path`, not the path you handed in.

Upstream's side of this, read out of UVStudio's own `js/flash.js` rather than guessed:
`MSG_APP_INFO 0x0730/0x0731`, `MSG_APP_ERASE 0x0732/0x0733`, `MSG_APP_WRITE 0x0734/0x0735`,
`MSG_APP_VALIDATE 0x0736/0x0737` -- the same framing as the firmware slots, one family further
along -- and the same constants this page uses (`APP_SLOT_COUNT 16`, `APP_IMG_OFFSET 0x1000`,
`APP_HDR_SIZE 64`, `APP_MAGIC 0x31504146`). Two details from there are worth having: UVStudio
uses only slots 0..7 and **labels them 1..8**, and it writes the header **last**, because the
header carries the committed flag and a partial write must never validate. This page writes a
whole slot at once, which has the same property for free.

The firmware confirmed the whole thing itself. The Labs build answers `0x0730` (app info) over
USART, so an installed `Beam.app` was queried on the radio, not just read back from the file:

    0x0730 slot 0 -> status 0
    raw 0000 | 46415031 01000101 4c040000 5860970d 0000 0108 | 4265616d 0000
              FAP1   hdr1 abi1 api1  1100     CRC 0x0d976058  flags 0x0801  "Beam"

That is the header this page installed, echoed by the running firmware, so the region, the
offset, the layout and the bytes are right. Slots 1 and 2 answered `status 2` with unrelated
data, which is the overlap the install guard exists for.

The page can also **ask the radio**. `GET /api/apps/radio` opens the firmware's serial
port and sends `0x0730` for all sixteen slots, so the answer comes from the running firmware
rather than from our reading of the file -- the bytes can be right and the firmware still
refuse a slot. Measured after installing `Beam.app` into slot 0, through the page:

    slot 0     -> Beam 1.0 · 1100 B · crc 0xd976058 · shortcut beam · committed true
    slots 1..3 -> status 2 with unrelated data   (the resource-block overlap the guard refuses)

A button beside the table asks it and shows the answer in its own column.

One thing that cost a round here: the server gives the emulator it starts a serial port
(`--serial-port`, default 4445), and QEMU needs the mingw64 DLLs on `PATH`. Started from
`work/run-webui.ps1` they are added; started by hand they are not, and a missing DLL makes
QEMU exit **before** it opens QMP -- which surfaces only as "QMP socket never appeared", with
nothing else naming the cause. QEMU's option errors go to stdout, which the launcher discards.

**Launching one is `F` then `7`, then MENU to run it.** That is upstream's own wording --
UVStudio's `locales/en.js` says "launch them from the F + 7 menu" -- and it is what
`App/apps/app_menu.c` does: `KEY_MENU` on an installed row calls `APP_LaunchOverlay(sel)`,
which "runs until the app exits"; EXIT leaves the menu. Verified in the emulator, with
`Tetris.app` installed through the page's own endpoint:

    F, 7   -> the app region goes from 0 reads to 8, and a boxed "F4HWN APPS" screen appears
    MENU   -> one read inside slot 0's code, and the game's own screen replaces the radio's
    DOWN   -> the piece moves; the screen is still the game two seconds later

For a while before that I could not find the way in at all: twenty keys short and long, the
whole 79-entry settings menu, and the multiboot menu all left the app region untouched, and
the multiboot menu reads firmware slots rather than apps. **The answer was written down in
the flasher's own translation file**, not in the firmware source I had been reading -- so
when a feature's entry point is missing, the host tool that installs it is the document.

**A blank screen after a firmware upload: check the multiboot state, and roll the image back --
do not edit state by hand.** Measured here: an image whose `FMP3` marker at `0x100000` recorded one
identity (size 120832, CRC `0x4D87CE48`) while `-kernel` loaded a different build makes the firmware
decide the running image is not the one its state expects, so it takes the restore/adopt path and
draws nothing: the panel's whole 1024 bytes stayed zero while the serial banner printed happily.
Replacing the working copy with the untouched dump brought the picture straight back (485 of 1024
bytes lit) and left the three installed apps reinstallable.

Two corrections to what an earlier version of this section claimed. **The marker is not a pending
flag**: decode it and `generation`, `image_size`, `image_crc32`, `firmware_slot`/`slot_inv`,
`config_bank`/`bank_inv` and `state_crc32` all check out (`0x661286F1` over the first 20 bytes) --
it is an ordinary state record. And **clearing the marker sectors is not a fix**: it was tried twice
(a whole 8 KiB, then only the 24-byte headers), after which the firmware took the `MB_MARK_MISSING`
= "fresh radio" path, adopted the running firmware, drew nothing while doing it, and had the marker
written back by its own write-back anyway. Rolling the image back is what worked.

**A stale write-back is still worth suspecting when an edit "does not stick"**: the emulator writes
its in-memory image back on exit, so an edit made while a guest was live can be overwritten by the
copy that guest was holding -- which is also how the marker reappeared after being cleared.

**Building an overlay app without the Arm toolchain: `pip install ziglang`.** There is no
`arm-none-eabi-gcc` and no Docker on the machine this was written on, but Zig ships a C compiler
that cross-compiles to `thumb-freestanding-eabi`, which is enough. What Zig refuses, each measured:
`--defsym`, `-Ttext`, `--section-start` (its linker-argument whitelist) -- so the VMA is resolved
into a copy of `app.ld` with `sed` instead; `-T` *is* forwarded, because a nonexistent script makes
it error; and `--image-base` is accepted but useless here, because it page-aligns the segments
(0x20000280 becomes LOADs at 0x200103C8 and 0x20020C94).

Two traps cost the most time. **lld maps the ELF header and program-header table as a LOAD of its
own** -- 52 + 4x32 = 180 bytes at the image base with no section content -- so `tools/elf2bin.py`
extracts *allocated sections* rather than program headers; following the program headers starts the
image at 0x20000000 and the loader refuses it with APP_ERR_VMA. And **one division pulled in
`__aeabi_uidiv`**, which does not exist in a `-nostdlib` blob: the fix is to remove the divisions
(repeated subtraction; `& 127` with a reject instead of `% 81`), not to link a soft-divide routine
into a 4 KiB overlay.

The entry must be first in the image, because the loader jumps to blob offset 0, so `app_main`
carries the same `__attribute__((section(".text.entry"), used))` that upstream's apps use
(`cube3d_app.c:183`).

Measured with the result installed through the page: the firmware reads the slot header twice and then
**exactly `code_size` bytes from slot + 0x1000** (2408 for Minesweeper's 2408-byte code, and it is
the only read of that size in the whole boot log). So the blob's shape, its header, the CRC, the VMA
and the offset are all accepted. **Whether control then reaches the overlay is still unproven**: the PC
probe samples every 100 ms and saw no overlay address, no `APP ERROR` screen appears, and the app does
not draw. That is where this stands -- the pipeline is verified up to the load, not up to execution.

**No overlay app actually runs in this emulator: the code is copied and control is never transferred.**
Measured by polling the PC through QMP every 30 ms (finer than the model's own 100 ms probe), with the app
installed through the page's own endpoint:

* after launching Tetris, memory at 0x20000280 holds Tetris's own code byte for byte (f0b56b4c85b0036f...),
  so the loader's copy is correct and aligned. The earlier claim in this session that it was shifted by a byte
  was my own parsing dropping the first value, not the model. **172 PC samples over 4.5 s and 64 more over 2 s
  were all inside firmware flash (0x08013260, a wait loop); not one landed in the 4 KiB overlay.**
* a 16-byte app that calls nothing at all -- no display, no keys, no struct member, just a volatile counter in a
  loop -- behaves the same way, so this is not about what an app does once it starts.
* the header is not the reason either: Breakout's header (flags 0x1, capabilities 0x0, entry_off 0, abi 1,
  api_min 1, hdr 1) is field-for-field the shape of ours, and the firmware still copies its code.

So the failure is upstream of the app: the firmware validates and copies, and then does not enter the overlay.
**This makes the earlier "Tetris runs, DOWN moves the piece" note stale -- per this file's own rule, treat it as
unverified until it reproduces.** The screen seen after MENU (the radio's own top line, e.g. "F4 APRS", with the
rest blank) is the main screen: the app menu has gone away without the app ever starting.

Next instruments, in order: whether the firmware waits for a key release before jumping (the PC sits in the same
wait loop at 0x08013260 both before and after MENU), and whether the copy of an upstream app is ever entered when
it is launched from the radio's own menu path rather than through this page.

**Correction, same day: those PC measurements were taken on instances that never drew a screen, and the
page's current firmware has no F+7 app menu at all.**

Two things were wrong with the method above. First, every QEMU instance started by hand for these runs came up
with a blank panel (0 lit pixels) while the page's own instance draws (803) -- the same image, a different
firmware file. So "the PC never entered the overlay" was measured on a radio that never reached its main loop,
and is **inconclusive**, not a finding. Second, driving the page's own /api/key and reading its /api/panel shows
what the user sees: F, 7, DOWN x3 and MENU all answer ok and the screen does not change by a single pixel
(ink 232 before and after every press). The page's firmware.bin is 109.3 KiB; the Labs build that does open the
app menu is 111.9 KiB, i.e. a different build. The radio still answers 0x0730 with four committed apps, so it
does support the app region -- it just has no F+7 entry, which is the Labs/UVStudio path.

So the user-visible symptom ("cannot get into the program") is the key press doing nothing on this build, and
the only way to test an overlay app is to run the Labs build itself. Measure through the page, not through a
hand-started QEMU: it is the instance that draws, and its endpoints are the same ones the browser uses.

**Reproduced on the page's own instance, with its own endpoints: the app menu works, and MENU on a row leaves the
launcher and never runs the app.**

The page was healthy for this run (pristine dump restored: 485 lit bytes, four apps installed and confirmed by
0x0730). Driving /api/key and reading /api/panel:

* F then 7 **does** open the menu -- ink 1810 -> 1976, a boxed title over the slot list.
* DOWN x3 **does** move the selection -- ink 1976 -> 1832, and the list's text changes.
* MENU on the app row drops the screen to the title box alone (ink 1832 -> 210), and from then on MENU, DOWN, UP
  and F all change nothing: the app neither draws nor reads keys.
* EXIT brings the menu back (ink 1832), so the firmware did enter and leave the launcher.

So the wall is between the launcher and the app, and it is not about the app's own code (a 16-byte app that calls
nothing behaves the same) or its header (Breakout's is field-for-field identical in shape). The screen state --
title box only, keys dead, EXIT returns -- is what a launcher that has taken over the screen and then never
reaches the app looks like.

Two measurement notes for whoever continues this. Restore the pristine dump (work/user-flash.img) before testing:
the working copy that carries the adopted FMP3 marker (image_crc32 0x9d27c3db rather than 0x4d87ce48) comes up
with the panel blank or showing only its boot screen, and keys then do nothing -- which is what several earlier
rounds mistook for the app's failure. And never hand-start QEMU for this: the hand-started instances came up
without a picture, while the page's own instance draws.

**The overlay app starts, runs, and dies the moment it calls a firmware service that reads the external flash.**

Measured on the page's own emulator with its probes on (UVK5_PC_PROBE / UVK5_FLASH_PROBE inherited through its own
launcher, so the instance being measured is the one that draws). Three apps, same row, same keys:

* **Spin, 16 bytes, calls nothing**: after MENU it is at 0x20000286 in 42 of 383 PC samples, i.e. it loops in the
  overlay forever. The loader works.
* **Minesweeper, 2408 bytes**: the flash probe shows exactly one big read, 0x109000 len 2408, whose first bytes are
  the blob's own, so the code is loaded; the PC probe catches a single sample inside the overlay before the app is
  gone. It lives well under 100 ms and dies.
* **Phases, 372 bytes**, which draws a bar straight into the framebuffer between calls so the screen reports how far
  it got: the bars for framebuffer-only and for led are on screen, and the bar after delay_ms never appears.

The overlay is the PY25Q16 sector cache -- app_overlay.h says so itself (it copies the code into the 4 KiB overlay,
the PY25Q16 sector cache, shared VMA with the multiboot RAM stub). So the picture is: while the app runs, a service
reads the external flash (the font table lives at 0x1E0000 there), the sector lands on top of the app, and the app
executes flash data. Real hardware cannot behave that way -- upstream's own apps call print_tiny -- so the firmware
must gate the sector cache while an app is loaded, and **that gate is what the model is missing**.

Next: find the gate in the firmware's flash driver (a memory flag, a register, a GPIO) and check what the model
answers for it. This is the first time the failure has a name and a reproducible three-app comparison behind it.

**Correction: the sector-cache explanation is not supported by the log order.**

The flash probe records every transaction in order, and the last one for that launch is the app's own
code load (addr=103000 len=544, first bytes f0b583b0). Nothing follows it before the app is gone, so no
font or resource read lands on top of the running app in that window -- the sector-cache overwrite cannot
be the cause here. What the log does show is 7848 reads in the font region over the session, all of them
part of the firmware's own repainting rather than the app's life.

Where that leaves the search, with everything that is actually measured: the loader copies and jumps (a
sample lands inside the overlay, and the 16-byte app that calls nothing loops there forever); the app then
dies within well under 100 ms (panel reads at t+113 ms still show the menu and at t+193 ms the launcher's
title box alone); an app that spins first survives a bar plus blit plus led and dies around delay_ms; and
one that calls blit immediately is gone before anything of it can be seen. The killer is inside the first
hundred milliseconds and is not a flash read.

Next instrument, and it needs a one-line model change rather than more guessing: the PC probe ticks every
100 ms, which is the same order as the app's whole life. Dropping that interval to a couple of milliseconds
turns it into a real trace and would show where in the overlay the app stops.

**The PC probe's interval is now a knob (UVK5_PC_PROBE_MS), and at 2 ms the overlay app's whole life is one tick.**

100 ms was the same order as the thing being measured: an app that the launcher starts and that is gone again
before the next sample. uvk5_pc_probe_interval_ms() reads UVK5_PC_PROBE_MS and falls back to 100, so every
existing probe run is unchanged. At 2 ms, a launch gives 468 samples in about 0.94 s and **exactly one of them
is inside the 4 KiB overlay** -- the app runs for roughly 2 ms, not 100.

With a real trace in hand, two things are now settled and one is new. Settled: the loader copies and jumps (the
single overlay sample), and a 16-byte app that calls nothing stays there forever. Settled the other way: the
sector-cache overwrite is dead -- the flash log's **last** transaction for the run is the app's own code load
(addr=103000 len=2408) and **nothing follows it**, no font read included. New: after the app dies the firmware
sits in a loop inside its own flash (PCs around 0x08005118/0x08005122/0x08005248) with **zero flash reads and
no key response at all** -- even F then 7 no longer opens the menu, and an explicit MENU down/up changes
nothing. So the launcher (or the fault path it takes) is wedged, not merely waiting for a release.

That is where the next round starts: the app dies within about 2 ms, the firmware never reads the flash after
loading it, and what is left running is a tight loop in the firmware with no display or key activity.

**The app-size ladder points the finger at an unfinished copy: the launcher jumps before the code is all there.**

Sizes and outcomes, all measured on the page's own emulator with a 2 ms PC probe (UVK5_PC_PROBE_MS):

| app | code | result |
| --- | --- | --- |
| Spin | 16 B | loops in the overlay forever |
| Delay | 248 B | **still running 13 s later** (54 of the last 60 samples inside the overlay, at 0x20000348) |
| Phases | 372 B | survives a bar plus blit plus led |
| Ladder | 544 B | dies before it can draw anything |
| Minesweeper | 2408 B, then 2428 B with an opening spin | dead within about 4 ms either way |

An opening spin does not save the big apps, so this is not about how soon the first call is made. It is about
**how much of the app has been copied**: the first few hundred bytes are executable and everything past them is
not there yet when the app runs. That also explains the earlier surprise that Tetris's full 3300 bytes were
present in the overlay when checked by hand afterwards -- the copy does finish, just after the app has been
entered. The pattern is the same family as the four flash bugs already in this file: the loader is told the
transfer is over while the guest is still feeding it, so it jumps early. The suspicion is the SPI/DMA busy and
complete flags, and the check is to watch them during a large read rather than to reason about them.

**The size ladder was a red herring: a 3 KiB app that calls nothing -- and then print_tiny, get_key and
display_clear in turn -- runs fine. Minesweeper is dying on its own code.**

The ladder lumped two variables together: every small test app happened to spin first, and every big one
called the firmware immediately. Separating them, all with the 2 ms PC probe on the page's own emulator:

* a **3028-byte** app whose body is a volatile pad array and a spin loops in the overlay indefinitely (1704
  samples, all inside its first 512 bytes) -- so blob size is not a problem;
* adding a single **print_tiny** to it changes nothing (2039 samples, and 1024 font-region reads appear in the
  flash log, the first at 0x1e0000) -- so the font path, and the sector-cache worry with it, is fine;
* adding **get_key** as well: still running (2029 samples);
* adding **display_clear** too: still running (1978 of 13195 samples, 15%, inside the app).

So the three calls no surviving app had ever made are all harmless, and the surviving apps have covered the
API surface Minesweeper uses. Its failure is in its own code. The next step is therefore an ordinary bisect:
strip pieces out of its draw() until it survives, install each variant through the page and read the same

**One clean A/B: the four calls Minesweeper makes, once, survive; wrapped in a for(;;) they never come back.**

Same file, same call order, one variable -- measured on the page's own emulator with the 2 ms PC probe:

| variant | body | result |
| --- | --- | --- |
| A | display_clear, print_tiny, get_key, delay_ms(40), then a spin | **runs** (2059 samples in the app, 1024 font reads) |
| B | exactly those four, wrapped in for(;;) | **never seen again** (0 samples in the app, and the same 1024 font reads, so it did execute the first pass) |

So the calls themselves are all fine individually and fine in sequence once; repeating them is what ends the app.

**The metric needs fixing before the next round, and that is worth writing down.** "Samples inside the app"
under-counts a *live* app: an app that spends its time inside long firmware functions shows up in the firmware
side of the trace, which is exactly what a dead app looks like too. A loop containing only display_clear()
scored 6, and loops containing only get_key() or only delay_ms() scored 0 -- and those three are not
comparable results, they are one good measurement and two unreadable ones. The fix is to alternate a long
self-contained spin with each call, so a live app is provably in the overlay most of the time and the trace
separates the two cases.

**Round 15: with a validated metric, every individual call survives repetition -- and the killer is inside draw().**

The metric first, because the previous round's was not trustworthy: each variant is for(;;) { long spin; one call; },
so a live app is in the overlay most of the time and a dead one is never there. The control (spin only) scores
27.2%; display_clear 26.9%, get_key 27.1%, delay_ms(40) 26.8%, print_tiny 27.0% -- **so all four calls survive
being repeated**, and the control proves the metric can tell the two cases apart.

That also means variant B of the previous round (four calls in a loop, no spin) was almost certainly not dead: it
spends nearly all its time inside firmware functions, which look exactly like a dead app from the trace. The
lesson is the same one this file keeps repeating -- a measurement that cannot show the thing you are looking for
will happily return zero.

With that out of the way, the bisect has closed on one function. Minesweeper with its whole draw() gutted to
display_clear() + blit_full() **runs** -- 97 samples inside the app, alternating with firmware PCs, and the
firmware still serving it. The full draw() draws nothing at all: over twenty seconds the panel never leaves the
launcher's title box, and ink stays at 210, meaning display_clear() never even ran. So whatever ends the app is
inside draw()'s body -- the eleven print_tiny calls, the framebuffer writes and the cursor invert -- and that is
the last mile.

Next: the same variant harness on the three pieces of draw() separately (the header text, the 81-cell loop, the
cursor), each with the loop spin that the surviving apps have.

**Round 16: draw()'s halves are each fine, and the app is running -- so the next suspect is the blit itself.**

Using the validated shape (for(;;) { long spin; one piece; }) the header block (display_clear + four print_tiny
calls, the long strings and x=122 included) scores 26.1% and the 81-cell loop plus the cursor, written with local
variables only, also scores 26.1% -- both the same as the control, so both are alive.

The full Minesweeper, measured the same way, is running: 116 samples inside it over fifteen seconds, spread over
eight distinct addresses (its spin at 0x200002e8/0x200002ec, plus 0x2000035a, 0x20000400, 0x20000564, 0x20000488
and others), with the firmware still serving it -- 1024 font-region reads appear during the launch. Yet the panel
never leaves the launcher's title box over ninety seconds, and that box is drawn before the app is entered, so
nothing the app paints has ever reached the glass. Removing the opening settle spin changed nothing, which also
retires the earlier idea that the app was simply still inside that spin.

So the app runs, calls into the firmware, and never completes a frame. That leaves two candidates and one of them
has never been checked: the statics (draw() reads g_cursor, g_state, g_mine_left, g_open, g_flag and g_mine, the
only inputs the two surviving variants did not touch), and **whether blit_full from an overlay app reaches the
panel model at all** -- the gutted variant was only ever measured by its PC share, never by what its screen showed.
The second is cheap to settle: an app that does nothing but display_clear plus blit_full should visibly erase the
launcher's title box.

**Round 17: the game paints. What was missing was a per-frame delay, and the display path was never broken.**

Three measurements settled it. A clear-and-blit app left the panel byte-identical, and filling the whole
framebuffer through api->fb and blitting turned it from 43 non-zero bytes to 596 -- so an overlay app's
framebuffer writes and blit_full do reach the panel, and the earlier reading of "the panel never changes"
was about content, not plumbing. What actually blocked the frame was Minesweeper's own delay_ms(40) on the
invalid-key path: in this emulator 40 ms of guest time is seconds of wall time, and a frame already spends
roughly twenty seconds inside the firmware's font reads, so the frames were minutes apart. Removing that
delay -- the loop redraws every pass anyway -- produced a complete frame within 24 s: the panel went from 43
to 390 non-zero bytes, ink 1275, with the title, the counters and the field all legible in the dump.

**Input is the remaining gap.** Pressing MENU changed nothing (596 -> 596) and pressing DOWN sent the app out
of its loop altogether: the panel went back to the launcher's 43-byte title box, which happens only on the
path that treats a key as EXIT. So get_key() is not returning the APP_KEY_* values this app compares against,
or the keys are not reaching it as sent. The cheap way to find out is to have the app draw the raw key code
it receives and read it off the panel, rather than guessing at the mapping.

**Round 18-19: the raw-key probe never ran, and the contrast narrows input to one call.**

Two font-free probes were built to draw the raw key code they receive (a marker bar on row 2, five bits on
row 20) and both left the panel byte-identical across seven key presses -- 26 lit pixels on row 2 and a
constant pattern on row 20, which is the launcher's own frame. So neither probe painted at all.

The useful part is the contrast: an app of the same shape that fills the framebuffer through api->fb and
blits (FillFb, round 17) does paint -- 43 non-zero bytes to 596 -- and the only substantive difference
between it and these probes is that the probes call api->get_key() every pass. Put beside the round-17
result, where the full Minesweeper painted a complete frame and then vanished the moment a key was pressed
(the panel returned to the launcher's title box, which happens on the path that treats a key as EXIT), the
picture is that the app runs and draws, and the key path is what ends it.

That is where input stands, and it is one call wide: get_key() in an overlay app. The sector-cache question
comes back with it -- the key path is a plausible place for the firmware to reach the external flash -- but
the font reads that print_tiny provokes do not kill the app, so this is specific to the key path rather
than to flash reads in general.

**Round 20: the key-path conclusion is contradicted, and the probes' own failure is unexplained.**

Round 15 measured get_key() repeated in an app loop at 27.1% -- the same as the control -- so calling it
every pass is not what kills an overlay app, and the previous section's reading of the two key probes as
evidence for that is wrong. Four probe builds (with and without an opening spin, with a short and with a
2M-iteration per-pass spin) all left the panel byte-identical: 26 lit pixels on row 2 and a constant row-20
pattern, which is the launcher's screen, drawn before the app is entered. So the probes did not run, and why
is not established.

What that failure is not: it is not the shape that other apps survived in, it is not get_key, and it is not
the framebuffer writes -- an app in the same slot that fills every framebuffer byte through api->fb and
blits does paint (43 -> 596 non-zero bytes). The probes differ from it in that they zero the framebuffer and
then draw with a helper of their own before blitting, which is exactly the sort of difference that has to be
isolated one change at a time rather than reasoned about, which is the discipline this file keeps preaching
and which the probes ignored.

Where the goal stands: the blank screen is fixed and understood; the Labs firmware displays; the app builds,
installs through the page, runs, and paints a complete frame (title, counters and field all legible); the
docs are bilingual and every step is committed and pushed. Input is the one item left, and it is not yet
narrowed to a call -- a key press ends the running game, which is the part that is measured, and the probes
meant to read the raw key codes never painted.

**Round 21: the launch needs a press before MENU, and that explains the whole "the probes never ran" saga.**

The panel reading that looked like a paint was the app menu itself: rows 0-6 are the title box and rows 18-22
the slot list, with 26 lit pixels on row 2 where the probe's own marker bar would be 120. So MENU on the
initially opened menu did not launch anything -- it selected a row. Round 17's Minesweeper run worked because
it pressed DOWN three times first, which left a row selected, and only then pressed MENU.

That single procedural detail accounts for every unexplained probe failure in the last three rounds, and it
rehabilitates both the probes and the game: with a press first, an app that clears the framebuffer, draws
through a helper and blits does run (499 on the panel, 10.9% of the PC samples inside the overlay). The
lesson is the one this file already carries in another form -- when a sequence of inputs is involved, check
what the first press does before concluding anything about the code under test.

So the input question reopens on solid ground: the game does run and paint when it is actually launched, and
the remaining unknown is what the keys do inside the running app, with the launch sequence now known to be
F, 7, DOWN (to select), MENU (to run).

**Round 22-23: the image is restored and healthy; the launch itself will not reproduce today.**

After a day of app installs the working copy was no longer the image that worked in round 17, so the pristine
dump was put back with the emulator powered off first (exit writes the in-memory image back over the file).
The page now comes up on its own main screen -- 485 non-zero bytes, the healthy case -- and Minesweeper is
installed in slot 3 at 2444 bytes, which is the same artifact that painted a complete frame in round 17.

What will not reproduce is the launch. F, 7, DOWN, MENU -- and a second MENU -- leave the radio on the app
menu: it is drawn completely (title box, six rows, a highlighted bar, ink 1799) and it stops responding to
keys altogether, with the same reading before and after every press. Round 17 measured the game's own frame
from this same app, this same slot and a press sequence of F, 7, DOWN x3, MENU, so the app is not in
question; what differs is the launcher's own key handling on the way in.

That is the state the keypad section already describes in another form -- a launcher that has taken over the
screen and then does not reach the app -- and it is now the single open item. Note it in the same breath as
the deliberate correction above it: the earlier attempt at this round blamed the app, and the A/B that
removed the instrumentation and rebuilt the round-17 source did not bring the frame back, which is what moved
the suspicion to the launcher and the image rather than the app.

**Round 24: the launch sequence, key by key, and proof that the app really is loaded and entered.**

Reading the panel after each single press from a fresh power-on gives the sequence the launcher actually
wants, and it is not the one this file said before: F, then 7, then **DOWN opens the app menu** (the panel
goes 489 -> 552), then DOWN again moves the selection (552 -> 526, ink 1799 -> 1677), then MENU hands the
screen over (ink 210, the launcher's own box). Earlier notes that say F then 7 opens the menu are wrong,
and that error is what made several rounds of MENU presses look inert: on the freshly opened menu the first
press only sets the selection, exactly as the round-21 entry says.

With the handover reached, the flash probe shows the load itself as the session's **last two transactions**:

    addr=108000 len=64   first=46415031 01000101   <- the FAP1 header of slot 3
    addr=109000 len=2444 first=f0b599b0 fd490860   <- the app's code, exactly code_size bytes

and the PC probe (2 ms) catches four samples inside the 4 KiB overlay in a three-second window, so the
loader copies and the CPU really does enter the app. It then disappears, and the launcher's box stays on
the glass -- the same shape round 8 described, and the opposite of what round 17 measured from this very
same artifact.

So the open question is now precise: with the launcher reached and the app entered, what ends it at the
handover. The next instrument is the 2 ms PC trace across the handover rather than after it, sampled from
the moment MENU is pressed.

**Round 25: the discriminator could not run, because the launch did not reproduce.**

The plan was to separate the two possible faults with a 20-byte app that calls nothing: if it loops in the
overlay the handover is fine and the game's problem is its own code; if it also vanishes the handover itself
is broken. It was installed in slot 3 -- the slot round 24 saw loaded and entered -- and driven with the same
sequence (F, 7, DOWN, DOWN, MENU). The menu came up at 511 non-zero bytes (the same family as the 552 and 526
seen before, the ink moving with the selection), and MENU changed nothing at all: the screen stayed at 511 for
twenty seconds and the PC probe recorded zero samples inside the overlay, out of 7316.

So the experiment answered nothing, and the honest reading is that the launch is not reliably reproducible
through the page's synthetic key events: the same sequence that produced a handover in round 24 (ink 210,
the launcher's box) produced no visible reaction here, with the installed app being the only difference.
That is a statement about the entry point, not about the app -- round 24 measured the load (the FAP1 header
and exactly code_size bytes) and the entry (four PC samples inside the 4 KiB overlay), and round 17 measured
a complete painted frame, both from this same artifact.

Minesweeper was put back into slot 3 afterwards, so the radio is left with the game installed rather than
the test stub. Next: drive the launch with longer gaps and read the panel after every single press, since
the presses that do land are the only ones that can be measured, and the failed ones have to be recognised
as failed rather than counted.

**Round 26: the launch is a question of which row is selected, and the handover works when it holds an app.**

The menu lists sixteen slots, and after the pristine image was restored only slot 3 held a real app -- slots
0 to 2 read as factory data, which the page calls "unknown". Every failed launch in the last rounds had the
cursor left on one of those, which is why MENU sometimes did nothing and sometimes produced the launcher's box
with the keys dead. Round 17's F, 7, DOWN x3, MENU worked because three presses happened to land on slot 3.

The test that settles it: install the game into slots 0, 1 and 2 as well, so every row holds it, then open the
menu (ink 617, more rows) and press MENU once. The screen drops to 43 -- the launcher's title box, i.e. it has
handed over -- and at t+40 s it reaches 484, the radio's own main screen, so the app ran and then returned.
Both halves are now visible: the handover happens, and the app comes back out.

The radio is deliberately left with the game in slots 0 through 3, because that makes the launch reliable for
whoever clicks through the page: whatever row the cursor lands on, MENU starts the game. Two menu entries --
launching the game and getting out of it -- are now the things to measure next, along with why the app returns
instead of staying up (round 17 saw it paint a full frame from this same artifact).

**Round 27: an app's first action, placed correctly, still does not run -- and the pattern that fits is code size.**

The user's screenshot is the state this file already describes: the app menu's title (drawn in the 8-bit font,
so F4HWN reads as F4HHH) over one blank white box, which is the launcher's own frame with nothing of the app
on the glass.

To separate "the app is not entered" from "the app draws nothing", the game's app_main was given an
unmistakable first act: fill every framebuffer byte with 0xFF and blit. The first attempt inserted it before
`A = api;`, so it dereferenced an unset pointer and faulted -- my own bug, and it invalidated that run. Moved
after the assignment (source +199 bytes, app 2508 bytes) the panel still never leaves 43 over 48 s, so the
probe's blit never happens. A 300-byte app doing exactly that fill-and-blit does paint (596 non-zero bytes),
and the loader has been seen reading the header and exactly code_size bytes.

Taking every measurement together, the variable that fits is the app's **code** size rather than its blob
size: a 3028-byte blob whose code is a few dozen bytes runs and can call print_tiny, display_clear and
get_key; Phases at 372 bytes of code runs; Ladder at 544 bytes dies; Minesweeper at 2400+ bytes dies with
its first instruction having no effect. Round 8's ladder said the same thing and was set aside because a
big blob that calls nothing survived -- which only shows that blob size alone is not the limit.

So the next round has a quantitative target: build the same trivial app with a code body of 400, 512, 600,
800 and 1200 bytes, drive each through the handover, and find where the panel stops changing. That is a
ladder in the one variable that has not been swept.

**Round 28: the ladder is built, and the entry point -- not the app -- is what blocks it.**

The ladder in the unswept variable is ready. A trivial app does exactly two things: it calls a chain of
no-op functions, then fills every framebuffer byte with 0xFF and blits, so the panel either goes all lit
(the app ran) or stays as it was (it never did). The chain costs about 5.2 bytes per function: 20 functions
give a 256-byte app, 70 give 620 bytes, so the rungs can be placed where they are wanted.

Neither rung could be measured, because the launch did not reach the handover. With the 256-byte app in all
four slots the sequence read: main screen 485, after F and 7 460, after DOWN 537 (the menu is open), and then
582 for twelve consecutive readings across three MENU presses -- the first press moved something, the rest did
nothing. That is the inert-launcher state this file already describes, and it is the same wall rounds 22 to 26
ran into from the other side.

So the honest summary of the last stretch is that the app pipeline is measured (the header and exactly
code_size bytes are read; the PC probe sees the overlay; one complete frame was painted in round 17) while
the entry point -- the synthetic key sequence that is supposed to run an app -- is not reliably reproducible
from the page. Until it is, no ladder can be walked and verified playable cannot be claimed. The next round
should therefore treat the launcher's own key handling as the subject: the same presses, read back after each
one, and the model's keypad path instrumented rather than the app's.

**Round 29: press timing decides whether the sequence is coherent at all, and the launcher refuses some apps before running anything.**

Two things were measured. First, the page's key endpoint defaults to TAP_MS = 60 with no gap when a request
omits hold_ms -- which is what every curl in the last rounds did -- while tools/key.py, the one the repo's own
keypad test validates, uses 200 ms and then waits GAP_MS = 400 for the release to be debounced. Driving the
sequence at the page default gave readings that did not cohere (F and 7 both around 460, sometimes no menu at
all); driving it with key.py's timing gave 485 -> 484 (F) -> 580 (7) -> 617 (DOWN) -> 43 (MENU), i.e. a menu
and then a handover. Any sequence that matters should therefore pass hold_ms = 200 and leave 400 ms after each
key.

Second, and more useful: with the handover reachable, the same sequence was run with the real Minesweeper and
with a 256-byte fill-and-blit toy. Minesweeper reaches 43 -- the launcher's box, i.e. it handed over -- where
the toy never leaves the menu (582). The launcher therefore distinguishes the two before either runs, so the
question is not how large an app can be before it dies but which apps the launcher accepts and launches at all.
That also retires the code-size framing of rounds 27 and 28: the toy is smaller and is the one that is not
launched.

Next: read the launcher's own acceptance test out of the firmware instead of guessing at it. The header fields
are the obvious first place -- caps, api_min, abi, entry_off, flags -- and the difference between the header

**Round 30: the app runs in the overlay and never calls the firmware once -- so it never reaches draw().**

Two measurements settle where the failure is. The firmware itself was asked about both apps over 0x0730
through the page's own endpoint, and it answers status 0 for all eight installed slots, the 2444-byte game and
the 256-byte toy alike. So round 29's reading of the launcher as refusing some apps is wrong: it accepts both,
and the refusal claim is withdrawn.

Then the launch was measured with the instruments instead of the screen. With the key.py timing (hold_ms 200
plus a 400 ms gap, which round 29 showed is what makes the sequence coherent), MENU drops the panel to 43 --
the launcher's box -- within one second and it stays there. The 2 ms PC probe records 51 samples inside the
4 KiB overlay out of 11439, spread over about ten distinct addresses in the first 1.5 KB of the app, so the
app is executing its own code. The flash probe says something sharper: the app's code load (addr=105000
len=2444) is the last transaction of the whole session, with zero flash transactions after it, and therefore
no font reads at all. print_tiny is only ever called from draw(), so draw() is never reached: the app runs,
stays in its own code, and never calls the firmware.

The one loop on that path which can spin without touching the firmware is place_mines(), which draws random
cells with a reject and no upper bound. It is now capped at 4000 attempts with a deterministic fallback that
fills from the start of the board, so a board that cannot be drawn randomly is still a board -- and, more to
the point, a hang there can no longer masquerade as an app that never started.

**Round 31: the app's very first API call has no effect, and the struct layout is the prime suspect.**

Three things were exonerated by measurement. The header: parsed against four real upstream apps (Tetris,
Breakout, Cube3D, Beam), every field lines up -- entry_off 0, flags 0x0001 for Breakout exactly as for mine,
name at offset 20, version at 36, link_vma 0x20000280 byte-for-byte identical -- so pack_app.py is right and
the refusal theory is dead. The entry point: the assembly listing's first function is app_main, its prologue
push.w {r4-r11, lr} matches the blob's first bytes f0 b5 exactly, and the linker script pins .text.entry to

**Round 32: the header is byte-identical to the firmware's own, and the font-read inference is suspect.**

The working copy's app_api.h and the firmware's own, fetched from armel/uv-k1-k5v3-firmware-custom over
HTTPS, are the same 14403 bytes with the same sha256 -- so round 31's prime suspect, that the app was
compiled against a different layout, is wrong and is withdrawn. Reading the offsets back out of the
assembly confirms the calls are right as well: the member at offset 28 is print_tiny and the one at 8 is
display_clear, exactly where this app's source expects them.

**Round 33: the fill-and-blit probe does not reproduce, and one of my own readings has been lying to me.**

The plan was to replicate round 17 exactly -- the 332-byte fill-every-byte-and-blit app in slot 0, launched
with the page's default taps -- and read the panel before and after in one run. It did not reproduce. The
readings went 485 (main screen) -> 580 after F and 7 -> 627 after MENU, and 627 then held for twenty seconds:
the screen never left the app menu, so the app was never entered and this run says nothing about the app.

The useful part is what that exposed. The values I have been reading as "the app painted" -- 537, 549, 552,

**Round 34: the app side is fully exonerated, and the firmware's own header names the mechanism.**

Both preconditions from round 33 were met and the result is unambiguous. Driven at key.py's timing the
sequence is coherent and reproducible: 485 -> 484 (F) -> 580 (7) -> 617 (DOWN) -> 43 on MENU, the launcher's
box, i.e. the handover. And the oracle was a toggling app -- fill 0xFF and blit, fill 0x00 and blit, forever --
whose panel signature no menu can produce. Fifty readings over twenty-five seconds were all exactly 43, none
above 900, none blank: the app's blits never reach the glass.

**Round 35: the model exonerates the sector cache, the panel path looks right, and the instrument exists but needs a page restart.**

Three things came out of reading qemu/py32f071.c rather than running anything, and one of them withdraws
the hypothesis this file recorded one round ago.

The panel is on SPI1 and the flash on SPI2 -- the model says so in as many words ("SPI2, not SPI1:
App/driver/py25q16.c uses SPI2 and st7565.c uses SPI1") and wires st7565_xfer to s->soc.spi[0]. A blit
therefore cannot touch the external flash at all, so round 34's sector-cache-overwrite mechanism is dead

**Round 36: the panel probe is in the binary but not in the environment, and I killed the page by mistake.**

The page had to be replaced to put UVK5_PANEL_PROBE into the emulator's environment, and the way I went
about it cost the user their running page: the port owner of 8080 was the managed launcher job, so killing
it took the page down. It was restored immediately as a managed background job and verified healthy -- main
screen 485 -- and this time it is a job, so it will not be lost the same way.

The instrument itself is fine. The running qemu-system-arm.exe contains UVK5_PANEL_PROBE and the literal

**Round 37: the panel probe works -- the page's environment is what drops the variable -- and two recorded notes were wrong.**

Run by hand with the variable definitely in its environment, the model wrote 29299 probe lines in forty-five
seconds, 28416 of them pixel data, so the panel path is being driven exactly as the firmware intends and the
instrument is sound. Comparing the two cases puts the fault in one narrow place: uvk5_supervisor.py builds
QEMU's environment as dict(os.environ), which does inherit, yet the page's own emulator never sees the
variable, so it is lost between the shell that starts the page and the server process it becomes. The fix is

**Round 38: a hand-run can carry the whole measurement, and the panel probe has a cap of its own.**

Round 37's discovery pays off: a hand-started emulator against the page's working image boots and draws, so
the measurement no longer needs the page at all. This run used a copy of the working image and left the page
untouched and healthy (485). It also needed no key tooling beyond tools/key.py and its QMP client.

Two of my own call signatures were wrong and are worth writing down. uvk5_apps.install takes (image: bytearray,
slot: int, blob: bytes, force) -- it edits the image in memory and reads the blob itself -- so passing a path

**Round 39: an overlay app's blits DO reach the panel -- the probe's own cap was hiding them.**

With the cap raised (40000 transfers was reached about forty seconds after boot, so everything after a launch
was invisible) and QEMU rebuilt, a hand-run on my own copy of the working image answered the question. The
toggling app -- fill 0xFF and blit, fill 0x00 and blit, forever -- was installed into slot 0 through
uvk5_apps.install with its real signature (image: bytearray, slot, blob, force), the keys F, 7, DOWN, MENU were
pressed over QMP, and the panel probe grew from 22568 lines to 139404.

**Round 40: Minesweeper's own frame content does reach the panel, and what dominates the log is its per-frame clear.**

Same pipeline as round 39 (hand-run on a copy of the working image, panel probe on, keys F, 7, DOWN, MENU over QMP),
now with the real Minesweeper: 103370 pixel bytes after the keys, dominated by 00 (100418 transfers) with a
longest run of 97640 -- about 95 full-screen clears -- and underneath it the app's own drawing bytes, 7f 287,
41 234, 08 192, 40 174 and so on, which are its text and box characters. Since the app calls display_clear()
once per frame, a full-screen clear followed by a few hundred bytes of glyphs is exactly the expected shape.

**Round 41: the panel's own memory, read over QMP and rendered as text, shows exactly what the user sees.**

The model exposes the controller's display RAM as the QOM property gram on /machine/panel, and the keypad as a
press property on /machine/keypad -- so one QMP connection can press F, 7, DOWN, MENU and then read the actual
picture. That is what the page draws from, and it needs neither the page nor a probe.

Before the keys: 485 non-zero bytes, the radio's own main screen, with F4HWN and the big digits legible in the
render. After MENU: 43 non-zero bytes, and the render is a single boxed title about 42 columns wide and seven

**Round 42: the panel's memory is frozen at the launcher's box, and that contradicts the panel probe.**

Read the controller's display RAM eight times over thirty-five seconds on one QMP connection, with no probe on:
485 non-zero before the keys, 43 at t+2 s, and then the same 43 -- identical md5, not one byte different -- at
t+5, t+10, t+15, t+20, t+25, t+30 and t+35 s. The app's screen never appears, and nothing repaints the box.

That cannot be reconciled with rounds 39 and 40, where the panel probe recorded 114028 pixel-data transfers
after the keys, 105 full-screen writes from the toggling app and Minesweeper's glyph bytes among them. Both

**Round 43: with the probe fixed, the two instruments agree exactly -- and the app never runs.**

The panel probe no longer opens, writes and closes a file once per byte; it accumulates in memory and writes a
buffer at a time, with the tail dumped at exit. Nothing else changed. The transfer volume went from 28416
pixel-data lines to 1930061 -- sixty-eight times more -- which is the measure of how much the old probe was
suppressing the guest. So it was perturbing what it watched, and its readings about volume were not usable.

With that fixed, the pictures match. Replaying the model's own store rule over the whole transfer log (a0=1,

**Rounds 44-45: the loader is read line by line, the overlay really is loaded, and every gate before the call passes.**

Three instruments agreed on the same picture. A memsave of 0x20000280 taken twice after MENU holds the app's own
code, 2738 of 2744 bytes identical, so the copy is complete and stays. The PC probe's 14505 samples contain not
one inside that 4 KiB -- the CPU never enters the app -- and the samples sit at 0x08005118/0x08005122. The flash
probe's last transaction is the app's code read (addr=105000 len=2744, first bytes f0b59bb0...), with nothing after it.

0x08005118 decodes as a PY32 SPI byte transfer: movs r2,#2; ldr r3,[pc] (=0x40013000); poll [r3+8] bit 1 (TXE);

**Round 46: the app's first instruction never executes -- entry() is never reached, and the hang is in RADIO_SetupRegisters.**

A 24-byte app whose first instruction stores 0xDEADBEEF at a fixed address settles the question with no inference at
all. Installed in slot 0 (code 24 B, entry_off 0, vma 0x20000280) and launched with F, 7, DOWN, MENU, the address
reads back as all 0xFF at t+2, +5, +10 and +15 s and never as 0xDEADBEEF. The app's entry point is therefore
never executed: APP_LaunchOverlay does not reach its entry(&app_api) call at line 1162.

That closes the bracket. Between the copy at line 1113 and the call at line 1162 the function's only return is the

**Round 47: RADIO_SetupRegisters is exonerated, round 46's narrowing is withdrawn, and the marker test is verified sound.**

The function turned out to live in App/radio.c, not App/app/radio.c, and read from there it is 179 lines with
exactly one loop: while (1) { if ((BK4819_ReadRegister(BK4819_REG_0C) & 1u) == 0) break; write(REG_02, 0); delay(1); }.
The model keeps that register at 0x0000 -- its own comment says REG_0C bit 0 must stay clear because two places in
app.c spin on it with no timeout, and it was fixed once already -- and reading the running emulator's reg0c over
QOM confirms 0x0000 with bit 0 clear at every sample, before and after the launch. So that loop exits immediately

**Round 48: the root cause, proved by arithmetic -- the overlay's CRC does not match, so the launcher returns APP_ERR_CRC.**

Two numbers settle it. The header says code_crc32 0x3c12630d and the app's own code hashes to exactly that, so the
app is not at fault. But the 2744 bytes that were actually sitting in the overlay -- read out of the running emulator
with memsave -- hash to 0x1312d98c, which is not the header value. APP_LaunchOverlay computes MB_Crc32Bytes over
that buffer and compares it with the header, so it takes the APP_ERR_CRC branch at line 1117 and returns before
ever reaching entry(&app_api). That is why the app never runs, and why the code is nevertheless sitting in the

**Rounds 49-50: the app runs -- and two separate causes fall out, both now proved.**

Installing the 24-byte marker app into slot 1 instead of slot 0 changed everything. After F, 7, DOWN, MENU the
fixed address reads efbeadde -- the app's first instruction really executed -- the overlay starts with the app's own
bytes 81b0034803490160, and the panel holds 615 non-zero bytes instead of the launcher's 43. So entry() is reached,
the app runs, and it paints. The display path, the loader, the header and the CRC were never the problem.

Two causes, and they are independent. First, the app menu's selected row is not slot 0: every launch in this

**Round 51-52: the corruption moves with code_size -- the model's flash read loses its last ~124 bytes.**

Two measurements, one app at two sizes, settle it. At 2744 bytes the six corrupted bytes sit at offsets 2620,
2621, 2622, 2623, 2627 and 2650; at 2568 bytes -- the same app after trimming 176 bytes out of it -- they sit at
2444, 2445, 2446, 2447, 2451 and 2474. Both are exactly 124 bytes from the end. The corruption is therefore not a
fixed address being clobbered: it is the last 124 bytes of whatever was read that come out wrong.

That also fits what already worked: the 24-byte marker app passed verification and ran, because 24 is shorter than

**Round 53: the DMA run that loads the app is exactly right -- so the wrong bytes come from the flash model's response.**

A diagnostic added to the model (UVK5_DMA_PROBE) prints every DMA run longer than 512 bytes. For the app load
there is exactly one, and it is correct in every field:

    DMA tx=4 rx=3 count=2568 tx_addr=0x20001338 rx_addr=0x20000280 tx_inc=0 rx_inc=1
        tx_cndtr=2568 rx_cndtr=2568

2568 bytes, destination exactly the overlay at 0x20000280, receive address incrementing, both channels agreeing

**Round 56: the transfer carries the correct bytes end to end -- the six bytes are written afterwards.**

A window probe on the DMA loop, logging the byte the transfer actually carried at a few fixed indices, prints
this at the end, for the run whose rx_addr is the overlay:

    idx 0: transfer f0, app code f0  (correct)
    idx 1: transfer b5, app code b5  (correct)
    idx 2444: transfer 00, app code 00  (correct)
    idx 2445: transfer 00, app code 00  (correct)

Earlier lines in the same log are other, shorter runs -- many of them carrying 0xff or the FAP1 header bytes --
so the load is not one clean transfer but a sequence, and the one that lands in the overlay carries the right
bytes everywhere, including the window that ends up wrong. The DMA and flash paths are therefore exonerated for
the last time, and whatever writes those six bytes does so after the transfer.

That also opens the possibility this file should have considered earlier: if the overlay is correct at transfer
time, the CRC check may pass, the app may actually run, and the six bytes may be a post-mortem symptom rather
than the cause. The next measurement is to read the overlay back within a fraction of a second of MENU and
hash it: correct then means the load and the verification passed.

My own mistake this round is the mirror image of one this file already documents. The window probe was meant to
write four lines and wrote 2088, because a condition on the loop index alone also matches every short transfer
that happens to start there. The file warns about capping a diagnostic before knowing the shape of the data;
this was the other direction -- flooding it -- and the same rule covers both.

**Round 54: the flash model's own data is correct, and so is the DMA -- so the six bytes are written after the transfer.**

The read path in the model is three lines and returns s->data[(s->addr++) % PY25Q16_SIZE], and the model keeps that
2 MB in RAM and writes it back on exit. So the file a run leaves behind is what the model believed the flash held,
and comparing it with the app settles the flash side: flash-r52.img and flash-r53.img both hash to the app's own
60234c72 with zero differing bytes at slot 1's code offset. The flash model hands back the right bytes.

Round 53 already showed the DMA run is right: count=2568, rx_addr=0x20000280 (the overlay), rx_inc=1, both counts
equal. So the transfer wrote the correct 2568 bytes to the correct place.

Yet the guest's overlay ends up with 40 d6 01 08 28 0a at offsets 2444..2474 where the app has zeros -- and at
code_size - 124 for both sizes measured. A value that is a firmware flash address, at a position that only the
loader and the DMA know about, is what a staged write or an interrupt handler's frame would look like. The next
instrument is to have the DMA probe dump the bytes it actually wrote in that window, which separates "the
transfer wrote them" from "something wrote them afterwards" in one run.

Worth stating plainly, because it has cost several rounds: three separate instruments now agree that both the
source bytes and the transfer are correct, so every earlier reading of this as a flash or DMA fault is retired.
on the count. So the DMA delivers everything to the right place.

The overlay nevertheless ends up with six wrong bytes, at offsets 2444, 2445, 2446, 2447, 2451 and 2474 -- and the
position is exactly code_size - 124, which held for both sizes measured (2744 -> 2620 and 2568 -> 2444). So the
corruption is the last 124 bytes of the read, at a position that only the loader and the DMA know about.

Since the DMA addresses and counts are right, the bytes themselves must be wrong: they are whatever the flash
model returned through py25q16_xfer for those positions. The firmware's own flash probe records the command and
length (addr=105000 len=2568) but not the bytes, which is why this looked like a memory corruption for so long.
The next instrument is to log the bytes the flash model hands back near the end of a long read and compare them
with the image.

Everything else this round is unchanged: the trimmed app still fails with APP_ERR_CRC and the panel stays on the
launcher's box (43), so the app still does not run.
the damaged tail, while Minesweeper at 2568 and 2744 bytes always fails its CRC with APP_ERR_CRC and never runs.
It is the same signature as the four DMA and flash faults this file already documents, and it retires the size
ladder for good: short apps ran and long ones died, and the reason was never the size as such -- it was the tail of
the read.

The evidence that it is the model and not the image is direct: the flash image at slot 1's code offset hashes to
the header's 0x3c12630d with zero differing bytes, and the guest's overlay over the same span comes out
0x1312d98c. The app is right on disk and wrong in RAM.

Trimming the app was still worth doing and is recorded: removing the three temporary marker probes, shortening the
header string and dropping the cursor readout took Minesweeper from 2744 to 2568 bytes, compiling with no
warnings -- but a tail bug cannot be dodged by shrinking, because the tail shrinks with the app.

Next: the model's flash read path. The firmware uses SPI_ReadBuf, so this is the DMA-driven read in py32f071.c --
the same place the four documented bugs lived -- and the thing to look for is where the final partial burst is
counted or addressed.
session installed into slot 0 and started a different row, which the overlay's contents gave away -- it held
Minesweeper's code (f0b59bb0fa490860c5690024...) when Mark had been installed in slot 0. Second, the overlay's tail
is clobbered before the CRC is checked: six bytes at offsets 2620..2650 (0x20000CBC onwards) that the app's code
holds as zero, and the overlay holds as a firmware pointer (0x0801D640).

That second cause explains the size ladder that cost several rounds: the clobbered offset is fixed, so an app
smaller than it passes verification and runs, and an app that reaches past it -- Minesweeper is 2744 bytes, only
about 124 bytes past the first corrupted byte -- fails with APP_ERR_CRC and never runs. Small apps ran, big ones
died, and the reason was never the size as such.

Two ways forward, both small. Shrink the app under the clobber point and it should launch as it is; or find what
writes at SectorCache+2620. That region is PY25Q16_OverlayBuffer(), and py25q16.c shows any cached sector read
does ReadBufferRaw(SecAddr, SectorCache, SECTOR_SIZE) into it -- so the writer is very likely a firmware path that
caches a sector while the app is loading, which the model may be provoking.
overlay: the copy at line 1113 happened, the verification after it did not pass.

Six bytes differ, at overlay offsets 2620, 2621, 2622, 2623, 2627 and 2650 -- address 0x20000CBC onwards. The app's
code holds zero at every one of them; the overlay holds 40 d6 01 08 (a firmware address, 0x0801D640) and two
single bytes. So something wrote a pointer into the tail of the overlay between the copy and the check, and a
pointer that sits where the app had zeros is exactly what a stack frame or a global written by other code looks like.

This also fits the warning the firmware's own app_api.h carries: the app executes from the RAM buffer that is also
the PY25Q16 sector cache, and other things in the firmware use that RAM. The next step is to find what writes at
0x20000CBC -- most likely an interrupt handler, which would make this a timing-dependent clobber rather than a
deterministic one, and would explain why the size ladder once looked like a copy that finished late.
and RADIO_SetupRegisters cannot be where the firmware stops. Round 46's conclusion is withdrawn.

The marker test itself holds up. Decoding the blob shows sub sp, #4; ldr r0,[pc,#12]; ldr r1,[pc,#12]; str r1,[r0]
with the literals 0x20003F00 and 0xDEADBEEF in the pool, so an app that runs really does write the magic, and it
never appears. entry() is not reached. What is not settled is why: with the copy verified present in the overlay and
the CRC matching on paper, the remaining candidates are the two early returns before the call (the VMA check and the
CRC check) and whether the copy the memsave saw came from this launch at all.

Two method notes. My Thumb bit-field decode was wrong twice in this round (Rn and Rt are the low six bits, and the
halfwords are read little-endian), which made a correct instruction look like a different one -- the hex bytes were
right and my reading of them was not, which is the same class of mistake this file records elsewhere. And the BK4819
register file is readable over QOM as /machine/bk4819: reg0c, which is a cheap way to check what a polling program
actually sees without a probe of any kind.
CRC check at line 1115 -- and the CRC is right (MB_Crc32Bytes is an ordinary CRC-32 and the host tool's value is
exactly what it computes). So the function cannot be returning early: it is stuck in between, and the only
hardware-touching statement there is RADIO_SetupRegisters(true) at line 1159. The PC sitting in the PY32 SPI
routine at 0x08005118, and the panel memory frozen at the launcher's box, both agree.

One honest note about the method: the address I picked, 0x20003F00, is not free -- it sits just under the top of
SRAM and the firmware was using it (it read as a pointer and some flags before the launch, and as stack bytes
after). The marker test is unaffected, because a magic word that never appears is proof the instruction never
ran, but the free-address assumption behind it was mine and was wrong.

Also this round: the FillFF variant from round 40 now builds. Its build script had been rewriting the compiler's
own arguments; the documented command line is used directly instead, with the include path and a copy of
app-resolved.ld, and both new apps compile with no warnings.

Next: fetch RADIO_SetupRegisters and follow what it touches, one call at a time, against the model.
strb to [r3+12] (DR); poll bit 0 (RXNE); read DR. The model's own comment confirms the layout (CR1 0x00, SR 0x08,
DR 0x0C) and keeps TXE asserted, so this is not a hang on a missing flag -- the firmware is doing transfers, which
matches the 1.93 million of them. So it is looping in firmware, not stuck.

The launcher itself was then read from the firmware's own source (App/apps/app_overlay.c, fetched). APP_LaunchOverlay
validates the slot, requires h.link_vma == the overlay buffer, invalidates the sector cache, memsets the overlay,
reads exactly code_size bytes in, checks the CRC, and only then calls entry(&app_api) at line 1162. Every one of
those gates passes for this app: magic FAP1, hdr 1, abi 1, api_min 1, committed set, capabilities 0, code_size 2744
within APP_OVERLAY_MAX, entry_off 0, link_vma 0x20000280 -- and MB_Crc32Bytes is an ordinary CRC-32 (init
0xFFFFFFFF, reflected 0xEDB88320, final xor), so the host tool's 0x3c12630d is exactly what the firmware expects.

So the copy happens, the checks pass on paper, and the call still does not take. Between the copy and the call the
only hardware-touching statement is RADIO_SetupRegisters(true) at line 1159 -- and the PC is sitting in a PY32 SPI
routine. That is where the next round starts, and it is one function wide.
col in 4..131, gram[page & 7][col - 4] = byte) and then counting non-zero bytes gives 43 -- exactly what the
gram property reported, and its md5 agrees with the frozen frame read in round 42. Every one of the 1.93 million
pixel bytes is accounted for, and the writes cover all eight pages and all 128 columns, so nothing is dropped
and no page is left stale.

That settles what the box is. The firmware is not failing to draw: it is redrawing the same screen -- 43 lit
bytes, the launcher's own title box -- about nineteen thousand times. Which is also why the zeros dominate the
stream (that screen is almost all zeros) and why the glyphs that round 40 read as Minesweeper's frame are
41, 40 and 7f: the launcher's own characters, sent over and over.

So rounds 39 and 40 were reading the launcher and calling it the app, and rounds 32 to 35 were right: after the
handover the app does not run. The instrument is honest now, the model is exonerated, and what is left is the
one thing this file has been circling: the launcher takes over the screen and never reaches the app.
instruments cannot be right. The probe is the suspect, and for a concrete reason: it opens, writes and closes
its file once per byte, on the vCPU thread, so it is slow enough to change the timing of the thing it watches --
and this file already carries the lesson in another form (a probe can create the behaviour it measures; and a
probe that cannot show the event it looks for will report zero). The transfer-count conclusion is therefore
withdrawn until the count is taken without per-byte file I/O.

What is not in doubt is what the glass shows, because it comes from the controller's own memory and needs no
probe at all: after the launcher hands over, the screen is the launcher's title box (43 non-zero bytes, the
signature this file recorded for that box) and it stays that way. So the app does not paint, which agrees with
what rounds 32 to 35 concluded from other evidence before round 39 overturned it.

Next: make the panel probe accumulate in memory and write once at exit, then re-run the launch. That is the
only way to compare the two instruments honestly, and it decides whether the app is drawing at all.
rows tall near the top with text inside it, and fifty-seven empty rows below. That is precisely the picture the
user described -- a title label over one blank box -- so the symptom is now reproduced from the glass itself
rather than inferred from transfer counts.

Put beside rounds 39 and 40, both things are true at once: the app does run and blit (105 consecutive
full-screen writes from the toggling app, and Minesweeper's own glyph bytes 7f, 41, 08, 40 in the panel
stream), and the app then dies, after which the launcher repaints its own title box. What ends up on the
glass is the launcher's frame. The remaining question is therefore the app's lifetime, not its drawing.

Two method notes. The QMP socket takes a single client, so pressing keys through one connection and reading
the panel through another times out -- as this round hit twice; do both on one connection. And the JSON is a
minimal PNG writer plus a reader, so the captures can be re-rendered as text without any image library: the
ASCII form is what makes this readable, since the model writing these notes has no image input.
So the app paints: the earlier "blank screen" is not a broken path.

Also worth recording: the FillFF variant (fill 0xFF only, never fill zero) failed to build and the failure was
mine. The build script is textual, and rewriting bigapp to fillff also rewrote the compiler's own -o argument
and source names, so the compile produced nothing and elf2bin then reported a missing fillff.elf. Fix the script
to substitute only the names it means to, or write the variant's script by hand.

Next, and it needs no guessing: the model exposes the panel's own display RAM as the QOM property gram (see
st7565_get_gram), so on a hand-run QMP can read the actual picture instead of inferring it from transfers.
That answers "is the game on the glass" directly, and it is the last step between here and calling it playable.

The reading is unambiguous. Of the 116836 lines after the keys, 114028 are pixel data, and the longest
consecutive run of byte 00 is 108273 transfers -- about 105 full-screen writes in a row, with a byte histogram
of 111061 zeros against 68 for the longest run of 0xFF. A hundred consecutive full-screen fills is not a
launcher repainting once; it is a loop, i.e. the app running and blitting.

That retires the conclusion this file has carried since round 32, that the app's calls do nothing. They do
something, and what hid it was the 40000-transfer cap: the instrument went blind forty seconds after boot and
the blindness was read as an app that never acted. It is the third instance in this file of capping a
diagnostic before knowing the shape of the data, and the first one where the cap was inside the model rather
than in a host tool.

One thing is still open and is the next measurement: the 0xFF phase does not appear (68 bytes at most), only
the zeros. Either the app never gets to fill 0xFF, or the firmware repaints over it immediately. An app that
fills 0xFF only -- no zeros at all -- separates those two in one run.
string is read as a blob and fails with "only 53 bytes" or "the image is too small for slot 0" depending on
which argument landed where. And key.py's Qmp wants a bare host:port: with the tcp: prefix getaddrinfo fails,
which is the very trap this file records in the socket section, hit again by its own author.

The useful find is about the instrument. The panel probe stops after 40000 transfers (panel_probe_n < 40000 in
st7565_xfer), and a hand-run reaches that within about forty seconds of boot: the log ended at exactly 40000
lines while keys were still going in, with the last eight thousand almost all byte 00. So a launch measured
after that point would have looked like an app that sends nothing at all -- the same shape of mistake this file
already records twice under capping a diagnostic before knowing the shape of the data. The cap has to go up (or
become a knob) before the app measurement can be believed.

Next: raise the cap, rebuild qemu-system-arm (stopping the page's job first, since it holds the binary), restart
the page, and run the launch again on a hand-copy with the probe watching.
to pass it the way the other launcher options are passed rather than relying on inheritance.

Two corrections to this file, both of which have been costing time. The probe prints s->selected, not the raw
chip-select level, so its cs=1 means the panel IS selected and its bytes ARE stored -- reading that field as a
level and concluding the panel ignores everything would have been wrong. And the old note that a hand-started
emulator never draws is not true of a hand-run against the same working image the page uses: this run booted,
drew, and streamed pixel data continuously. That note has been steering measurement through the page for many
rounds; it should be re-derived rather than believed.
"PANEL a0=", and it was built after the source was last modified, so the model's panel probe is in the
binary that is executing. No panel.log appears anywhere in the repo or the build tree, which means the
variable is simply not in that process's environment: run-webui.ps1 passes only --qemu, --elf and --flash,
and tools/webui.py has no env= dict in the paths looked at, so the spawn site that builds QEMU's environment
is still unread -- most likely tools/uvk5_supervisor.py.

Two process-inspection traps are worth recording, both hit here: Get-CimInstance and Get-Process return
nothing in this sandbox (no python and no qemu process were listed while the page was plainly answering on
8080), while netstat -ano and taskkill work; so the port owner came from netstat. And a second page instance
started while the first is running dies without a word, because the port is taken -- which is exactly how two
earlier restart attempts failed silently.
and is withdrawn. That also agrees with the flash probe, which saw no transactions after the app's code
load: there was nothing there to see.

The panel model itself reads correctly: it stores a pixel byte only when cs is low and a0 is high, it keeps
the controller's own gram, and its column counter is the 132-column one with the col-4 store -- including
the four-column bug this file documents, which is fixed. Nothing in that path looks like a reason for an
app's blit to be dropped.

What is missing is the measurement, and the instrument for it already exists: UVK5_PANEL_PROBE logs every
byte sent to the panel with a0, cs, page and col, which is exactly what separates "the app's blit never
reaches the driver" from "the driver's bytes never land". It needs the page restarted with that variable
in its environment, and two attempts to replace the running page failed: a second instance dies because
port 8080 is occupied, and the port-owner lookup came back empty. The page itself stayed healthy throughout
(main screen 485), so nothing was left broken for the user. Next: find the page's pid another way -- netstat
-ano, or Get-Process python -- stop it, start it with UVK5_PANEL_PROBE, and read that log around a launch.

Then the offsets were checked properly, and the earlier reading of them was my own arithmetic error: the struct
begins with uint8_t abi_major, uint8_t api_level and uint16_t api_size -- four bytes in total, not twelve -- so
fb is at 4, display_clear at 8, and print_tiny at 28. The app's calls land exactly there. Header identical to
the firmware's, entry pinned at offset 0, offsets correct, firmware answering status 0, handover confirmed:
every part of the app side now has evidence behind it.

What is left is between the app's call and the glass, and the firmware's own app_api.h states the mechanism:
the app executes from the RAM buffer that is also the PY25Q16 sector cache, so no API callback may touch
external flash while app_main() runs. blit_full talks to the panel over the same SPI bus the flash sits on,
so a blit can reload the sector cache and overwrite the executing app -- which would look exactly like this:
a call that returns no error, changes nothing, and leaves the app gone. The next step is to instrument the
model's sector-cache path around a blit issued from an app, rather than the app.
580, 596, 617, 627 -- are one family: the app menu, whose ink moves with the highlighted row. A genuine
all-lit panel would be about 1024 non-zero bytes and has never been observed, and the game's own frame in
round 17 was a different signature entirely (390 non-zero bytes with ink 1275, legible in the dump). So at
least one earlier "it painted" reading was a menu row, and every such claim in these notes should be read
again against what the value actually is.

What that leaves is narrower and better defined: the launch through synthetic keys is not reliable enough to
carry a measurement, and the panel ink alone cannot tell a menu from an app. Both have to be fixed before the
next attempt at the app itself: drive the launch with key.py's timing and verify the handover by the 43-byte
launcher box, then judge the app only by a signature no menu can produce.

That leaves the measurement those conclusions rested on in doubt. The claim was that draw() is never
reached because the flash probe records no font read after the app's code load. But the session shows
1446 reads of the font region early on, which is what caching the font in RAM looks like -- and if the
font is cached, print_tiny never touches the external flash at all, so "no font read" says nothing about
whether draw() ran. The instrument, not the app, is what was measured.

So the next oracle has to be one that cannot be confused this way: something visible in the panel and in
nothing else. The sharpest unexplained fact is still that fill-every-byte-and-blit as the app's very first
statement leaves the panel untouched, while the same act from a 320-byte app lights it; that contrast needs
re-measuring with the app doing nothing else at all, and with the panel read before and after in one run.
offset 0. And the acceptance: the firmware answers status 0 for the app over 0x0730.

What is left is sharper than anything so far. The listing shows the app doing exactly what it was written to
do: load A, load the api member at offset 28, call it (the first print_tiny), then run new_game() inline and
settle into the main loop, whose spin is the hottest offset in the PC histogram. The app is running -- 417800
samples land inside the overlay, clustered at offsets 0x18/0x8a/0x8c/0x9c/0xae, i.e. its own first 180 bytes.
And the calls at offsets 8 and 28 -- display_clear() and print_tiny() as this source understands them -- do
nothing observable: the panel never changes and the flash probe records no font read after the code load.

The reading that fits every observation is that the app_api_t layout this app was compiled against is not the
one the running firmware uses, so the app calls whatever function really sits at those offsets and neither
touches the display nor the external flash. That would explain a program that runs, burns CPU in its own code
and has no visible effect at all. The next step is to verify the struct against the firmware's own header --
its member order and offsets -- rather than against the copy in this repo.
this page writes and the one an upstream app carries is the thing to diff.

That is where the next round starts: the same alternating-spin shape, one call at a time.
trace. That is a much better place to be than the emulator mystery this started as.

One measurement note: the flash probe's per-read lines were also the way the font read was spotted (1024 of
them, none of which landed on the app), which is a useful control to keep running.

Workaround, for now and marked as one: a small app is a working app. The opening spin added to Minesweeper is
kept out of the repository until the real cause is fixed, because it does not work anyway.

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
- **Settings do persist now, which changes how to test.** The PY25Q16 model loads
  the image at realize time, keeps it in RAM, and writes it back over a temp file when
  CS is released or the process exits, so *every session leaves `assets/flash.img`
  changed*. Measured after one real session: the settings block at `0x00A000`, which
  starts life as all `0xFF`, held the guest's settings, `0x8000..0x8800` had moved,
  and the file differed from the pre-session copy in 2239 bytes. Diff against
  `assets/pristine/` (or a copy you kept) instead of assuming a fresh image, and power
  the emulator off before restoring it. On Windows this silently did nothing until
  `rename()` was replaced by `g_rename()` -- see the portability section.

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

### Keeping the docs honest

    python3 tools/check_docs.py     # also runs as part of run_tests.sh -q

Documentation rots quietly, and reading it does not find that. Translating everything
into Chinese turned up four claims that had already drifted: the endpoint table was
missing three routes, the modelled-peripheral list omitted TIM2, the audit table still
called TIM a stub after TIM2 was modelled, and neither README listed several library
modules. All four were found by comparing against the source, none by proofreading.

So the comparison is mechanical now. It checks that every tool a README names exists,
that every test in `run_tests.sh` is documented in both languages, that internal `.md`
links resolve, that the translation pairs have matching heading structure, that the
memory-map addresses match the model's `#define`s, that every long flag a doc passes to
a tool actually exists in it, and that documented firmware `file:line` references still
point at what the prose claims.

Two things the checker itself needed before it could run anywhere but the author's
machine: every read is `encoding="utf-8"` (the default is the locale codec, and on
Windows that is GBK, which cannot decode the Chinese docs at all), and the firmware
tree path comes from `UVK5_FW_DIR` rather than being hardcoded, so the `file:line`
checks can be pointed at whatever tree you have.

The flag check earned its own lesson. Its first version matched only to the end of the
line, so on a wrapped command like

    python3 tools/uvk5_buffers.py --qmp 127.0.0.1:4444   # this firmware's addresses
    python3 tools/screenshot.py --frame-addr 0x... --status-addr 0x... \
        --port 1234 --out screen.png

it saw `--frame-addr` and nothing else -- 4 of 9 flags, and it reported a clean run.
**A check that silently covers a quarter of what it claims is worse than no check**,
because the clean result is believed. Continuations are joined before matching now.

One caution, from writing it. An early version compared firmware constants with a regex
that took the first number on the line, so `key_debounce_10ms = 20 / 10` read as 20 and
the checker declared the docs wrong for saying 2. **The docs were right and the checker
was broken.** A checker that cries wolf gets ignored, so anything it cannot verify
unambiguously is left out rather than guessed at.

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

### What is actually reproduced, and what only answers reads

Written after a fair criticism: progress reports kept saying what *runs* rather than
what is genuinely reproduced. Those are different, and the gap is easy to hide.

Counted from the firmware's own call sites:

| peripheral | call sites | state |
|---|---|---|
| GPIO | 55 | modelled |
| DMA | 59 | modelled, over the CPU's address space |
| SPI | 33 | modelled, with the flash |
| TIM | 23 | TIM2 modelled since `fdcbe80`; the rest stubbed (backlight PWM) |
| ADC | 19 | modelled; result settable since `e46cae2` |
| USART | 11 | modelled both directions |
| RTC, IWDG, WWDG, I2C, USB, CRC, EXTI, PWR | 0 | stub, and the firmware never uses them |

Plus, outside the SoC: the keypad, the BK4819 register interface, and the audio enable
line.

**A stub accepts writes and returns the last value.** That is enough not to hang and
nothing more. The distinction matters because it is invisible from above: the ADC was
*modelled*, and still returned a hardcoded 2200 forever, so `gBatteryDisplayLevel`,
`gLowBattery` and the low-battery popup were unreachable. Answering reads is not the
same as being reproduced.

The honest summary is that **the digital side the firmware depends on is reproduced, and
the analogue side is not and cannot be**. Frequency, flash, keypad, serial, register
programming, battery — all real. Audio samples and RF behaviour — no data exists to
model, in the MCU's address space or in any public datasheet.

`millis()`/TIM2 and the settable ADC closed the two gaps that mattered. What is left,
and why:

**Backlight PWM — deliberately not modelled.** `backlight.c` drives intermediate
brightness with TIM7 triggering DMA channel 7 to rewrite GPIOA `BSRR` from a 32-entry
duty-cycle table, at `PWM_FREQ * DUTY_CYCLE_LEVELS` = 128 kHz. Modelling it means
128,000 GPIO writes and DMA transfers per emulated second, and **nothing observable
changes**: backlight is physical LED brightness and does not touch the framebuffer, so
`frame.png` is byte-identical either way. The two endpoints that do have observable
behaviour — brightness 0 and full — bypass the timer entirely and call
`GPIO_TurnOffBacklight`/`TurnOnBacklight`, which already work. Cost is high, benefit is
zero.

**EXTI** — zero call sites today. Any interrupt-driven rework would need it first.

### Audio: there is nothing to model, and that is the finding

"Add a speaker and a microphone, then grant the browser audio permission" is the
obvious request, and it cannot be done — not for lack of effort but because neither
device is on the MCU. Receive audio is demodulated inside the BK4819 and leaves as
analogue on its AF pin; transmit audio goes from the microphone into the chip's own ADC.
The firmware touches only:

    PA8       amplifier enable  (GPIO_EnableAudioPath, driver/gpio.h:34)
    REG_47    which AF source the chip routes
    REG_64    a level it displays

**No audio samples exist anywhere in the MCU's address space.** There is nothing to
capture, nothing to play, and nothing for a browser permission to carry. Generating
sound would be inventing data the firmware never produced — the same line as the
analogue RF limit.

What is real is the *intent*. `TYPE_UVK5_AUDIO` watches PA8 and exposes read-only
`speaker-on`; the UI shows a speaker glyph and `/api/status` reports `speaker`.
Read-only on purpose: a writable one would only let a test lie to itself. A unit test
also asserts the page never asks for audio permission — no `getUserMedia`, no
`AudioContext`, no `<audio>` — because prompting the user to approve something that
cannot happen is worse than not offering it.

### A stub that is more forgiving than the real client is worse than no stub

`QmpClient.command` returns the **unwrapped** value and raises on error. The test stub
returned `{"return": ...}`. So `webui.py` was written to unwrap a second time, all 88
tests passed, and the live UI returned 500 with

    TypeError: argument of type 'bool' is not iterable

Two lessons, both of which cost time here. The stub is now pinned to the real contract
by an explicit test. And the failure was originally swallowed by a bare
`except: return None`, which made a broken call indistinguishable from a radio that was
simply silent — and sent me hunting a stale process that did not exist. Log the reason.

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
5. When you add a stub to `py32_stubs[]`, **bump `PY32_NUM_STUB`**. Forgetting used
   to be silent: the device was never realized, the address stayed unmapped, and the
   only symptom was that nothing changed. A `QEMU_BUILD_BUG_ON(ARRAY_SIZE(...) !=
   PY32_NUM_STUB)` next to the table makes it a build error now. Two holes were found
   that way, both fatal to the multi-system release (see the portability section):
   `0x40007400` = `DAC1_BASE` and `0x1FFF3000` = `UID_BASE`, neither of which any
   firmware-visible list mentioned. That is why the whole APB/AHB peripheral space now
   has a **low-priority catch-all** behind the named devices: an unnamed register
   answers and logs instead of aborting, and a data abort on real hardware that
   answers is a model bug, not a discovery.

## Finding the display buffers in a new firmware

`gFrameBuffer` and `gStatusLine` move between builds and **neither is 128-byte
aligned**, so an aligned guess renders a picture that is wrong in a way that looks
like a font or a font-loading problem: 0x3E bytes off and every row becomes "tail of
the previous row + head of this one", which splits glyphs at a fixed column and hides
the status line behind frame content. Do not eyeball it -- the firmware source says
exactly where they are.

1. Dump SRAM (QMP `memsave`, or `python work/qmp.py dump 0x20000000 0x4000 out.bin`).
2. Pick bitmaps whose contents *and* placement are both known from the source
   (`App/bitmaps.c` with `App/ui/status.c` and `App/driver/st7565.c`):
   `gFontPowerSave` is copied to status +0, `gFontDWR` to +18, `gFontPttClassic` to
   +54, `BITMAP_BatteryLevel1` to +111 (`LCD_WIDTH - 17`); `BITMAP_VFO_Default` is
   `memcpy`'d to offset 0 of a **frame** line.
3. Search SRAM for those byte strings. Only one base makes all four status offsets
   agree at once, and the VFO arrow's address *is* the frame buffer. They must then
   differ by exactly `FRAME_LINES * LCD_WIDTH` = 896, which is what says the search
   converged. For the 5.9.0.CN build: frame `0x200012BE`, status `0x2000163E`.

Self-check once you have them: frame line 3 is the middle separator the UI memsets, so
it should be entirely zero; and with both VFOs on one frequency, frame lines 0/1 equal
lines 4/5 while lines 2 and 6 differ, because only the active VFO's info line has
content.

## Portability: what Windows actually broke

The machine and the tools are portable C and Python; the *packaging* was Linux-only.
Four failures, each invisible until something depended on it:

* **`rename()` does not replace an existing file on Windows.** The flash write-back
  writes a temp file and renames it over the image, so every settings save failed with
  `cannot replace`, settings never reached disk, and the stderr storm held the main
  loop long enough that QMP never sent its greeting -- which surfaced only as "power
  on failed: timed out". `g_rename()` (needs `<glib/gstdio.h>`) gives the POSIX
  behaviour on both platforms.
* **A Windows QEMU cannot create a unix socket**, so QMP has to travel as
  `tcp:host:port`; `uvk5_qmp.py`, `key.py` and the supervisor's launcher accept both
  forms now.
* **`qemu/py32f071.c` does not compile against a stock QEMU 7.2** without
  `#include "qapi/visitor.h"` for `visit_type_uint64`; `qom/object.h` does not pull it
  in transitively.
* **`-kernel foo.bin` loads in the wrong place.** `armv7m_load_kernel()` puts a raw
  binary at the base it is handed, which on this machine is the flash *alias*, so the
  image lands 0x2800 bytes high and the first fetch faults. `tools/bin2elf.py` wraps
  the release `.bin` in an ELF32/ARM header with the right program header.

**The external flash is partitioned, and the main firmware reads it.** Only
`0x00A0xx` showed up in a 26 s capture once, which looked like "the firmware does
not use the flash at all" -- wrong twice over: the first run was defeated by a
PowerShell UTF-16 redirect, the second by capping the probe at 80 reads. With the cap
lifted (4000) and a menu opened so Chinese text is drawn, one boot produces 3168
reads: the settings block, individual glyphs in the user font packs at `0x0A0000`
and `0x0E0000`, and a **1024-step walk of a 32 KB font table at `0x1E0000`**, 32
bytes per step. The layout, derived from the tooling at
`gitee.com/oldlicn/betula-multi-system-tool` rather than from its partition-map
image:

| offset | size | contents |
| --- | --- | --- |
| 0x000000 | 128 KB | bootloader + settings (`0x00A0xx`) + calibration (`0x010000`) |
| 0x020000 | 4 x 128 KB | firmware slots (the tool ships "clear 0x20000-0x40000" through "0x80000-0xA0000") |
| 0x0A0000 | 256 KB | user font pack, 16x16 |
| 0x0E0000 | 64 KB | user font pack, 8x8 |
| 0x100000 | 1 MB | factory resource block, including the 32 KB table at `0x1E0000` |

Sixteen official 128 KB restore files reassemble into a real 2 MB image. Adding its
`0x100000-0x200000` region to `assets/flash.img` **changes what the firmware
renders**, so that data is live, not decoration. Which source supplies which text is
still open: the 16-pixel glyphs on screen match neither the pack at `0xA0000` (2 of
24 cells) nor the table at `0x1E0000` (0 of 8) byte for byte.

Panel settings are a fifth, different case: contrast and inversion are not in the
framebuffer at all, so nothing that renders `gFrameBuffer` can show them.
`TYPE_ST7565` models the controller's own registers and `tools/uvk5_lcd.py` applies
the inversion to the picture; see README.md.

**Round 57: the overlay is already wrong a quarter of a second after MENU, and never changes again.**

Hashing the overlay every 250 ms from MENU onwards, with no probes on: before MENU it holds the previous
content (crc 169b5c51); at +0.25 s it is 4f23f6f3, which is not the app's 60234c72; and every later sample is
that same 4f23f6f3. So the load finishes, the overlay ends up wrong, and nothing touches it afterwards.

Put beside round 56 -- where the window probe showed the transfer that lands in the overlay carries the correct
bytes at indices 0, 1, 2444 and 2445, and no other run in that log has an rx address inside the overlay -- the
writer is not the DMA. It is a CPU store, inside the quarter second after MENU, and it lands at
overlay + code_size - 124.

The instrument that would name it is a write watchpoint on that word: the gdbstub is already listening on the
gdb port and supports Z2, so a short client can set one, let the launch run, and read the PC when it fires. That
halt is the measurement rather than a perturbation of it, which is the one case where the advice against
attaching a debugger does not apply.

Also this round: the Chinese note for round 56 is in, appended at the end of the file because the anchor the
append script picks keeps landing on a fenced code block -- the same failure recorded in round 54. The heading
counts still match, and the parity check passes.

**Round 58: a write watchpoint names the writer, and it is the launcher's own memset -- 1200 times over.**

Setting a hardware write watchpoint on 0x20000C0C -- overlay + 2444, the first byte that ends up wrong for the
2568-byte app -- and reading the registers at every hit gives exactly one distinct writer:

    PC=0x0801ae7a  LR=0x0801701c  fill=0x00  x1200
      r0=0x20000280  r2=0x20001280  r3=0x20000c0c

r0 is the overlay's base, r2 its end, and the instruction is a byte fill, so this is memset(ws, 0, 0x1000) --
APP_LaunchOverlay's own zeroing of the 4 KiB overlay -- and it is called twelve hundred times inside the
window. The launch path is being retried, over and over.

That the only writer to that word is the zeroing is itself the important part, because the word ends up holding
40 d6 01 08. Something therefore leaves it non-zero without any store the watchpoint saw, and the one way that
happens is if the copy never covered it: the memset zeroes all 4096 bytes, the read fills code_size bytes, and if
the read comes up about 124 bytes short the tail keeps its zeros and the CRC fails.

That also reconciles round 56. The window probe there looked at one transfer -- the one that landed -- and that
one carried the right bytes at 0, 1, 2444 and 2445. There are many transfers; the probe did not measure whether
every attempt carries code_size bytes.

So the next measurement is small and precise: log the count of every DMA run whose destination is the overlay,
and see whether some of them are 124 bytes short of code_size. If they are, the whole picture closes -- short
read, CRC failure, retry, and the app never runs -- and the fault is in whatever decides that count.

**Round 59: the read is not short -- the overlay is simply zeroed over and over, about a hundred times a second.**

Logging every DMA run longer than 512 bytes with its destination and count, for twelve seconds after MENU, gives
exactly one run whose destination is the overlay:

    DMA tx=4 rx=3 count=2568 tx_addr=0x20001338 rx_addr=0x20000280 tx_inc=0 rx_inc=1

2568 is code_size exactly, so round 58's short-read conclusion is withdrawn: the copy is complete and correct,
as rounds 53 and 56 already showed from the byte side.

The comparison that matters is with round 58's watchpoint. That word is written 1200 times in the same sort of
window, every one of them by memset(0x20000280, 0, 0x1000), while the DMA writes the overlay only once. So the
code is loaded once and the 4 KiB overlay is zeroed roughly a hundred times a second.

That fits everything that was otherwise puzzling. The 24-byte marker app ran because its CRC window is 24 bytes
and the check happens immediately after the load, so it passes; Minesweeper's window is 2568 bytes and is far
more likely to be hit by one of those zeroings before the check. It also explains the size ladder that cost
several rounds: short apps survive the race, long ones do not, and the reason was never the size as such.

Next: identify what zeroes the overlay a hundred times a second. The memset itself is at 0x0801ae7a and its
caller returns to 0x0801701c, so the caller is one function away from being named.

**Round 60: the memset's caller is a routine near 0x08016ff0, and it passes a computed pointer, not a literal.**

Decoding around the return address 0x0801701c puts the call inside a function whose prologue is at 0x08016ff0
(push, then sub sp, #0x6c). The literal pool near it holds 0x20001b40 through 0x20002811 and 0x2000000d/0x2000000e
-- RAM addresses in the settings and EEPROM area -- and 0x20000280, the overlay base, appears nowhere in it. So
the pointer handed to memset is computed rather than loaded, which is what PY25Q16_OverlayBuffer() looks like.

My own disassembler misaligned badly here: it printed nonsense branch targets like 0x8741e36, which is the tell
that the halfwords are not being paired the way the Thumb-2 encoding requires. The two facts above survive that
because they rest on the prologue shape and on the literal values themselves, but no instruction-level claim from
this round should be trusted until the decoder is fixed or a real ARM disassembler is available.

What round 59 established still stands and is the part that matters: the overlay is zeroed about a hundred times
a second while the code is loaded once, so nothing the loader writes can survive. That is consistent with the
24-byte marker app running and Minesweeper not, and it means the retry loop, not the copy, is what to explain.

The next measurement needs no disassembler: at the 1200 hits the stack pointer is 0x20003c50, so reading the
words above it gives the whole return-address chain and therefore who calls the routine that calls memset. One
run of that names the retry loop.
