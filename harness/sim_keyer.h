/* Test-facing driver for the CW keyer.
 *
 * Wraps the firmware's CW_HandleState() in the polling loop the real main loop
 * provides, and translates its returned action into recorded carrier events.
 * A scenario is: configure, script a paddle timeline, run, assert.
 */

#ifndef SIM_KEYER_H
#define SIM_KEYER_H

#include <stdbool.h>
#include <stdint.h>

// Resets clock, paddle script, recorder, keyer state and EEPROM, then applies
// the given configuration. `key_input` is a CW_KEY_INPUT_* bitmap value (see
// settings.h); `keyer_mode` is a CW_IambicMode_t.
void SIM_KeyerBegin(uint8_t key_input, uint8_t keyer_mode, uint8_t wpm);

// Runs the polling loop for the whole queued paddle timeline, plus `tail_ms` of
// idle time so trailing character/word gaps can be detected.
void SIM_KeyerRun(uint32_t tail_ms);

// One dit at the configured speed, in milliseconds. The reference for asserting
// element durations: a dah is three of these.
uint32_t SIM_KeyerDitMs(void);

#endif
