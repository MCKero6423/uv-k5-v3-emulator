# UV-K5 V3 emulator

Runs Quansheng UV-K5 V3 / UV-K1 firmware on a PC. The radio uses a Puya
PY32F071 (Cortex-M0+), which QEMU has no machine for, so this adds one.

The firmware boots to its main loop in about five seconds and the LCD contents
are readable. Keypresses reach the firmware's scan but are not yet acted on --
see [Status](#status).

## What it is for

Editing firmware and reflashing a radio to test one line is slow, and some bugs
are invisible from the outside. A recent example: CW macro recording appeared to
do nothing, and the cause was three layers down -- the keyer was being torn down
by a later call that recomputed its state from the wrong VFO. On hardware you see
"nothing happens"; here you can read the actual variables.

What it does **not** do is model radio behaviour. It reproduces what the firmware
*commanded* -- frequency, power step, carrier keying in time -- not the analogue
result. Keying envelopes, spurious emissions and sensitivity need a real radio and
a spectrum analyser. That is not a gap to be closed later; the transceiver chip
has no public datasheet, so its driver is the only specification available.

## Status

| Area | State |
| --- | --- |
| Boot to main loop | works, ~5 s |
| LCD contents | readable via `tools/screenshot.py` |
| SPI flash, settings, calibration | works |
| Keypad matrix | rows reach the firmware's scan (`KEYBOARD_Poll` returns the right key code) but the UI does not react — under investigation |
| Timing accuracy | deliberately wrong, see [Timing](#timing) |
| Radio/RF behaviour | not modelled |

## Layout

    qemu/                    QEMU sources to be copied into a QEMU tree
      py32f071.c             the SoC and machine (the bulk of the work)
      armv7m_systick.*.patched  SysTick with the poll-boost property added
    assets/
      calibration.bin        512-byte dump from a real radio
    tools/                   run, screenshot, inject keys, probe state
    harness/, stubs/, shim/, tests/   host build of the CW timing chain (stage A)

## Building

Needs a QEMU 7.2 source tree, `meson`, `ninja`, `libfdt-dev`, `libglib2.0-dev`,
`libpixman-1-dev`.

    # 1. Drop the sources into a QEMU tree
    cp qemu/py32f071.c                   $QEMU/hw/arm/
    cp qemu/armv7m_systick.c.patched     $QEMU/hw/timer/armv7m_systick.c
    cp qemu/armv7m_systick.h.patched     $QEMU/include/hw/timer/armv7m_systick.h

    # 2. Register the machine. In $QEMU/hw/arm/Kconfig:
    #      config UVK5_V3
    #          bool
    #          default y
    #          depends on TCG && ARM
    #          select PY32F071_SOC
    #      config PY32F071_SOC
    #          bool
    #          select ARM_V7M
    #          select UNIMP
    #    In $QEMU/hw/arm/meson.build:
    #      arm_ss.add(when: 'CONFIG_UVK5_V3', if_true: files('py32f071.c'))

    # 3. Build just the ARM target
    cd $QEMU
    ./configure --target-list=arm-softmmu --disable-docs --disable-tools
    cd build && ninja qemu-system-arm

## Running

    python3 tools/make_flash.py     # once, builds assets/flash.img
    tools/run.sh                    # starts the machine

    tools/where.sh                  # where the firmware is executing
    python3 tools/screenshot.py --frame-addr 0x200013DC \
        --status-addr 0x2000175C --port 1234 --out screen.png
    python3 tools/key.py MENU       # inject a keypress
    tools/gpiob_dump.sh             # GPIOB registers

The machine exposes a GDB stub on port 1234 and a QMP socket at
`/tmp/uvk5-qmp.sock`. It is headless: the screen is read out of guest memory
rather than drawn, so no display backend is needed.

Screenshots need the addresses of `gFrameBuffer` and `gStatusLine`, which move
between builds. Find them with:

    arm-none-eabi-nm firmware.elf | grep -E 'gFrameBuffer|gStatusLine'

## How the machine is put together

Register layouts come from the vendor CMSIS header shipped with the firmware
(`Drivers/CMSIS/Device/PY32F071/Include/py32f071xB.h`), not from guesswork.

    FLASH  0x08000000  128 KB   application at +0x2800, bootloader below it
    SRAM   0x20000000   16 KB
    RCC    0x40021000
    GPIO   0x50000000   ports A, B, C, F at 0x400 intervals
    SPI1   0x40013000   display
    SPI2   0x40003800   flash
    ADC1   0x40012400

Modelled: RCC, GPIO, ADC, both SPI controllers, DMA1, and the PY25Q16 flash.
Everything else answers through a logging catch-all — the log is how the next
thing worth modelling gets identified.

Seven things had to be right before the firmware would boot, each found by
watching where it stopped:

- **Flash alias at the application offset.** The core fetches its vector table
  from address 0, and the image loads at 0x08002800, so 0 has to alias there and
  not at the flash base.
- **Clock ready bits.** `BOARD_Init` polls them; each enable bit is mirrored into
  its ready bit.
- **ADC calibration.** `CR2.CAL` is write-1-to-start and hardware-cleared, so it
  must never be stored set or the wait loop never exits.
- **SPI flags.** Transfers complete inside the register write, so TXE stays
  asserted and RXNE is raised by the write.
- **DMA.** The flash driver never touches the SPI data register — it arms
  channels 4 and 5, enables the transfer-complete interrupt and spins on a flag
  its ISR sets.
- **SysTick.** See below.
- **Transceiver data line.** `RADIO_SetupRegisters` waits for bit 0 of the
  BK4819 REG_0C to clear. The bus is bit-banged over GPIO, so PB9 idles low until
  that bus has a real model, making reads return zero.

## Timing

`SYSTICK_DelayUs` polls the SysTick counter and accumulates differences. On
hardware each loop iteration advances the counter by tens of ticks; under
emulation a register read costs far more relative to guest time, so the counter
barely moves per read. Measured: a 120 ms delay advanced 832 of 5,760,000
required ticks in four seconds — about 7.7 hours to complete.

Lowering the clock does not help, which is worth knowing before trying it: the
bottleneck is loop iterations per second, not counter speed. Dropping 48 MHz to
200 Hz gained only 32x.

What works is reporting a counter value that runs ahead of the real one, growing
with every read. The `poll-boost` property on SysTick does that. Two earlier
attempts wrote the value back into the timer instead, which made each read
re-anchor the count — the reported value stopped changing, the firmware's
`if (cur != prev)` guard never fired, and the loop hung outright.

The consequence is that guest time runs fast during any delay. Fine for
exercising menus and control flow; wrong for judging signal timing.

## Stage A: the CW timing chain on the host

`harness/`, `stubs/`, `shim/` and `tests/` compile `app/cwkeyer.c` and
`app/cwmacro.c` unmodified against stub drivers, with a virtual clock and
scripted paddle input. Feed a timeline of contact closures, assert on the decoded
characters and element durations.

Firmware sources are compiled as-is on purpose. Editing them to make them build
on a host would let the tests drift from what the radio runs. The debounce in
`CW_ReadKeys` is transcribed rather than stubbed, because its asymmetry (three
consecutive reads to register a press, immediate release) is part of the timing
behaviour under test.

## Credits

Base firmware: [armel/uv-k1-k5v3-firmware-custom](https://github.com/armel/uv-k1-k5v3-firmware-custom).
Register definitions from the vendor CMSIS headers.
