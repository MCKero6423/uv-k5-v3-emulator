/* Virtual millisecond clock.
 *
 * The keyer is a timing machine polled once per millisecond by the real main
 * loop. Tests drive that loop explicitly instead of sleeping, so a 30-second
 * exchange completes in microseconds and behaves identically every run.
 */

#ifndef SIM_CLOCK_H
#define SIM_CLOCK_H

#include <stdint.h>

void     SIM_ClockReset(void);
uint32_t SIM_ClockNow(void);

// Advances the clock without running anything. Used by SYSTEM_DelayMs, where
// the firmware blocks and the keyer is not polled.
void SIM_ClockAdvanceRaw(uint32_t ms);

// Advances one millisecond at a time, invoking the registered tick callback
// after each step. This is the simulator's stand-in for the main loop.
void SIM_ClockRun(uint32_t ms);

// Called once per virtual millisecond by SIM_ClockRun. Set this to the function
// under test (CW_AppUpdate on hardware, or CW_HandleState directly).
typedef void (*SIM_TickFn)(void);
void SIM_ClockSetTick(SIM_TickFn fn);

#endif
