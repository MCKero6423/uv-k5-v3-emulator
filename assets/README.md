# Simulator assets

## calibration.bin — 512 bytes

A calibration dump from the user's own radio. Loaded into the virtual SPI flash
at physical `0x010000`, which is where `App/driver/eeprom_compat.c` maps the
512-byte calibration block (`_MK_MAPPING(0x010000, 0x00B000, 0x00B200)`).

Without it the firmware takes error branches in the frequency and power paths,
so the machine would boot into a state that does not represent the real radio.

### Verified contents

Checked against the offsets `SETTINGS_LoadCalibration()` actually reads:

| Offset | Field | Value | Sanity |
| --- | --- | --- | --- |
| 0x000–0x0BF | per-band TX power curves | 10 ascending bytes per row, 0xFF padding | plausible power steps |
| 0x0C0 | `gEEPROM_RSSI_CALIB[3]` | 110, 120, 130, 140 | ascending |
| 0x0C8 | `gEEPROM_RSSI_CALIB[0]` | 180, 190, 200, 210 | ascending |
| 0x140 | `gBatteryCalibration` (6×u16) | 1426, 1978, 2125, 2155, 2271, 2600 | strictly ascending, matches a Li-ion curve |

47% of the file is 0xFF, consistent with a real dump: each power row uses 10 of
its 16 bytes and the remainder is erased flash.

The file name the user supplied said "not necessarily accurate"; the structure
above is self-consistent, so it is being treated as usable. If the emulated radio
later shows implausible power or battery readings, this is the first thing to
re-dump.

### How to refresh

Export from a real radio with [UV Studio](https://armel.github.io/uvtools2/?mode=dump)
(Dump Calib), then replace this file.
