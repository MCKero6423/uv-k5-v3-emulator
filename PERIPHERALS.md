# Peripherals a whole-machine simulation has to answer for

Derived from the firmware's own boot path (`App/main.c` → `BOARD_Init`) rather
than from the datasheet, so the list is what this firmware actually touches.

Order matters: the boot sequence stops at the first peripheral that does not
answer plausibly, so they have to be brought up roughly in this order.

## Tier 1 — required to reach the main loop

| Peripheral | Firmware entry | What the model must do | Notes |
| --- | --- | --- | --- |
| Cortex-M0+ core, NVIC, SysTick | `Core/startup_py32f071xx.s`, `SYSTICK_Init` | execute Thumb, deliver SysTick | 53 vectors in the table |
| RCC (clock tree) | `BOARD_Init` | report clocks ready, accept enables | firmware polls ready flags |
| FLASH controller | `FLASH_Init` | accept latency/prefetch writes | reads come from the ELF image |
| GPIO A/B/C/F | `GPIO_Init` | hold direction/pull state, report input levels | keypad and PTT live here |

Milestone: firmware reaches `while (true)` without faulting.

## Tier 2 — required to see anything

| Peripheral | Firmware entry | What the model must do | Notes |
| --- | --- | --- | --- |
| SPI → ST7565 LCD | `driver/st7565.c` | decode page/column addressing into a 128×64 framebuffer | 12 LL calls; the display is the main observable |
| Keypad matrix | `driver/keyboard.c` | drive rows, report the pressed column | injected from the front end |
| SPI → PY25Q16 flash | `driver/py25q16.c` | commands 0x03/0x02/0x20/0x9F over a 2 MB file | 66 LL calls — the heaviest driver |
| ADC | `ADC_Init`, `helper/battery.c` | return a plausible battery reading | a flat value is enough at first |

Milestone: boot logo appears, keys navigate the menu.

Note on the flash model: calibration data lives in it. Without a real dump the
firmware takes error branches in the frequency and power paths, so a dump
exported by UV Studio should be loaded into the image.

## Tier 3 — radio behaviour

| Peripheral | Firmware entry | What the model must do | Notes |
| --- | --- | --- | --- |
| BK4829 (SPI) | `driver/bk4829.c` | track frequency, bandwidth, modulation, power, CTCSS/DCS, and carrier keying; allow RSSI injection | no public datasheet — the driver *is* the specification |
| BK1080 (FM RX) | `BK1080_Init` | accept register writes, report a tuned state | only needed for the FM broadcast feature |

Milestone: scanning, Fox Hunt and the CW timing chain run end to end.

What this tier can and cannot give: it reproduces *what the firmware commanded*
— frequency, power step, keying envelope in time — which is enough to catch
wrong-VFO transmissions, missing carrier releases and bad key timing. It does
not reproduce the analogue result: modulation quality, spurious emissions,
receiver sensitivity. Those need a spectrum analyser on real hardware.

## Tier 4 — host connectivity

| Peripheral | Firmware entry | What the model must do | Notes |
| --- | --- | --- | --- |
| UART | `UART_Init` | expose a PTY | debug tracing |
| USB CDC | `VCP_Init`, `App/usb/` | expose a virtual serial device | this is the payoff — see below |

The USB CDC path is the cheapest route to a web front end. The Fusion build
already streams its screen to UV Studio's K5Viewer over USB serial and accepts
remote key presses, so pointing the emulated CDC endpoint at a PTY lets the
existing UV Studio page act as the simulator's UI. Screen mirroring and the
virtual keypad are already written; they do not need reimplementing.

## Memory map (from `Core/py32f071xb.ld`)

    FLASH  0x08002800  118 KB   application (0x08000000..0x08002800 is the bootloader)
    RAM    0x20000000   16 KB

The non-zero flash origin matters: loading the application at 0x08000000 puts
the vector table in the wrong place and the machine faults immediately.

## Bootloader

DFU lives in the first 10 KB and is a separate image. Deciding to emulate it too
is worthwhile — flashing is the operation most likely to brick real hardware, and
V3 enters DFU with PTT + side key 2 + power — but it is a distinct target from
the application.
