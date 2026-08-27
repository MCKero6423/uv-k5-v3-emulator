#include "sim_clock.h"

#include <stddef.h>

static uint32_t   s_now_ms;
static SIM_TickFn s_tick;

void SIM_ClockReset(void)
{
    s_now_ms = 0;
    // The tick callback is deliberately left alone: tests register it once and
    // reset the clock between scenarios.
}

uint32_t SIM_ClockNow(void)
{
    return s_now_ms;
}

void SIM_ClockAdvanceRaw(uint32_t ms)
{
    s_now_ms += ms;
}

void SIM_ClockSetTick(SIM_TickFn fn)
{
    s_tick = fn;
}

void SIM_ClockRun(uint32_t ms)
{
    for (uint32_t i = 0; i < ms; i++) {
        s_now_ms++;
        if (s_tick != NULL)
            s_tick();
    }
}
