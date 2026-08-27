#include "sim_keyer.h"

#include "app/cwkeyer.h"
#include "app/cwmacro.h"
#include "misc.h"
#include "settings.h"
#include "sim_clock.h"
#include "sim_paddle.h"
#include "sim_record.h"

void SIM_EepromReset(void);   // stubs/driver_stubs.c
void SIM_CaptureReset(void);  // harness/sim_capture.c
void SIM_CapturePoll(void);

// The firmware's own constants (cwkeyer.c): 1200 ms / WPM is one dit.
#define SIM_TICKS_PER_MINUTE 60000U
#define SIM_DITS_PER_WORD    50U

static uint8_t s_wpm = 18;

// Translates one poll of the keyer into recorded carrier events. Mirrors the
// RF-path switch in app/cwapp.c: HOLD_ON means "already keyed, stay keyed", so
// only the ON/OFF transitions are edges.
static void tick(void)
{
    switch (CW_HandleState()) {
    case CW_ACTION_CARRIER_ON:
        SIM_RecordCarrier(true);
        break;
    case CW_ACTION_CARRIER_OFF:
        SIM_RecordCarrier(false);
        break;
    default:
        break;
    }

    // Sample the firmware's display buffer for newly decoded characters.
    SIM_CapturePoll();
}

void SIM_KeyerBegin(uint8_t key_input, uint8_t keyer_mode, uint8_t wpm)
{
    SIM_ClockReset();
    SIM_PaddleReset();
    SIM_RecordReset();
    SIM_EepromReset();
    SIM_CaptureReset();

    s_wpm = wpm;

    gEeprom.CW_KEY_INPUT  = key_input;
    gEeprom.CW_KEY_WPM    = wpm;
    gEeprom.CW_KEYER_MODE = (CW_IambicMode_t)keyer_mode;

    CW_KeyerResetRuntime();
    CW_KeyerReconfigure(true);

    SIM_ClockSetTick(&tick);

    // CW_HandleState defers its pending init until it sees an idle word gap, so
    // give it that before the scripted timeline starts. Without this the first
    // element of every scenario would be swallowed by the configuration apply.
    SIM_ClockRun(8U * SIM_KeyerDitMs());
    SIM_RecordReset();
}

void SIM_KeyerRun(uint32_t tail_ms)
{
    SIM_ClockRun(SIM_PaddleTotalMs() - SIM_ClockNow() + tail_ms);
}

uint32_t SIM_KeyerDitMs(void)
{
    return SIM_TICKS_PER_MINUTE / ((uint32_t)s_wpm * SIM_DITS_PER_WORD);
}
