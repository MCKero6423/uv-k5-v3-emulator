#include "sim_paddle.h"

#include "sim_clock.h"

#define SIM_PADDLE_MAX_STEPS 512

typedef struct {
    uint32_t contacts;
    uint32_t until_ms;  // absolute virtual time this step ends
} Step_t;

static Step_t   s_steps[SIM_PADDLE_MAX_STEPS];
static uint32_t s_count;
static uint32_t s_end_ms;

void SIM_PaddleReset(void)
{
    s_count  = 0;
    s_end_ms = 0;
}

void SIM_PaddleHold(uint32_t contacts, uint32_t duration_ms)
{
    if (s_count >= SIM_PADDLE_MAX_STEPS)
        return;

    s_end_ms += duration_ms;
    s_steps[s_count].contacts = contacts;
    s_steps[s_count].until_ms = s_end_ms;
    s_count++;
}

void SIM_PaddleTap(uint32_t contacts, uint32_t hold_ms, uint32_t gap_ms)
{
    SIM_PaddleHold(contacts, hold_ms);
    if (gap_ms > 0)
        SIM_PaddleHold(SIM_CONTACT_NONE, gap_ms);
}

uint32_t SIM_PaddleState(void)
{
    const uint32_t now = SIM_ClockNow();

    for (uint32_t i = 0; i < s_count; i++) {
        if (now < s_steps[i].until_ms)
            return s_steps[i].contacts;
    }
    // Past the end of the script: everything released.
    return SIM_CONTACT_NONE;
}

bool SIM_PaddleDrained(void)
{
    return SIM_ClockNow() >= s_end_ms;
}

uint32_t SIM_PaddleTotalMs(void)
{
    return s_end_ms;
}
