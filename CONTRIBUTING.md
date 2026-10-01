# Contributing

Short version: build it, run the tests, and do not commit firmware or radio data.

## Get it running

    QEMU_SRC=~/src/qemu-7.2 bash tools/setup_qemu.sh   # patch a QEMU tree and build it
    python3 tools/fetch_firmware.py                    # a release image to run
    python3 tools/make_flash.py                        # the flash image it reads settings from
    pip install -r requirements-dev.txt                # flask, for the web UI tests
    bash tools/run_tests.sh -q                         # fast; no emulator needed
    bash tools/run_tests.sh                            # everything; needs the tree above

`tools/run_tests.sh` checks the build first and stops if it failed. That matters: `ninja`
leaves the previous binary in place, so a suite run against a broken build reports
results for code that was never compiled. It has happened here twice.

The emulator tests boot their own QEMU on private ports, so they do not disturb a
`run-webui.ps1` or `run.sh` session. A missing QEMU, firmware or gdb makes a test
**skip** with a message, never fail -- a missing prerequisite is not a regression, and
a suite that fails on a fresh checkout teaches people to ignore it.

Two helpers keep that honest: `tools/uvk5_socket.py` gives every test an endpoint that
works where the platform has unix sockets and TCP where it does not, and
`tools/uvk5_testenv.py` finds a QEMU, a firmware and a gdb, skipping with a reason when
one is absent. Use them rather than hardcoding a path or a socket family.

## What runs automatically

`.github/workflows/unit.yml` runs `tools/run_tests.sh -q` on every push and pull request --
the unit tests and the documentation check, which need nothing but Python. If you add a
test that needs an emulator, it belongs in the slow half of the runner, not here; if you
add one that needs nothing, make sure it is picked up by the `-q` path so CI covers it.

`Dockerfile` gives you the same environment locally.

## What not to commit


- **Firmware of any kind**, including released images, localised builds and bootloader
  dumps. `tools/fetch_firmware.py` fetches what a test needs into `assets/firmware/`,
  which is ignored.
- **Anything from a real radio.** `work/data.bin` is an EEPROM dump with settings and
  calibration; the tests build their own images from `assets/pristine/`.
- **Scratch under `work/`** -- images, logs, captures. The four `.ps1` scripts there are
  tracked on purpose.

If you find any of that already in the history, say so before pushing: removing it in a
new commit does not remove the objects.

## House rules

These come from mistakes that already cost time, and [AGENTS.md](AGENTS.md) has the long
version of each. The ones worth repeating:

- **Never edit the firmware to make the emulator work.** The firmware is the reference;
  if something does not run, the model is wrong. A fix in firmware source makes every
  later test meaningless.
- **Instrument the model, not the guest.** A breakpoint stops the machine and changes what
  you are measuring. Put a probe in `qemu/py32f071.c` and read the output.
- **A probe has to be able to see what it looks for.** Three rounds of "the firmware never
  touches the flash" were a probe filtering on an address field that write-enable and
  erase frames do not have.
- **Check the build succeeded before believing a test.** See above.
- **Document what you got wrong**, in `AGENTS.md` and `AGENTS.zh-CN.md` together. The two
  are kept in step; `tools/check_docs.py` checks the heading structure and the tool names.
- **Never invent data.** Audio and RF are not modelled because the MCU never sees them,
  not because nobody got round to it.

## Documentation

Every document has a Chinese pair (`README.md` / `README.zh-CN.md`, `AGENTS.md` /
`AGENTS.zh-CN.md`) with the same heading structure. `python3 tools/check_docs.py` checks
that the tools and flags a document names exist, that the heading pairs match, that the
memory-map addresses match the model, and that firmware `file:line` references still point
at what the prose claims. It runs as part of `run_tests.sh -q`.
