/* Driver-layer replacements for the host build.
 *
 * Everything the CW timing chain reaches outside its own two files. The count is
 * small on purpose: app/cwkeyer.c and app/cwmacro.c contain no register access,
 * so this is the whole seam.
 *
 * Time comes from the virtual clock, not the host clock. That is what makes the
 * tests deterministic and fast.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "harness/sim_clock.h"
#include "harness/sim_paddle.h"
#include "harness/sim_record.h"

// ---------------------------------------------------------------- timing

uint32_t millis(void)
{
    return SIM_ClockNow();
}

uint32_t millis_since(uint32_t start)
{
    // Same unsigned wrap arithmetic as the firmware.
    return SIM_ClockNow() - start;
}

void SYSTEM_DelayMs(uint32_t ms)
{
    // The firmware blocks here, so the keyer is not polled: advance the clock
    // without running ticks. Modelling this faithfully matters -- the startup
    // stuck-key check delays 50 ms and must not see paddle activity.
    SIM_ClockAdvanceRaw(ms);
}

// ---------------------------------------------------------------- inputs

bool GPIO_IsPttPressed(void)
{
    // PTT is the dit paddle in Buttons mode and the straight key in handkey
    // modes, so it reads from the same scripted timeline as TIP.
    return (SIM_PaddleState() & SIM_CONTACT_TIP) != 0;
}

// ---------------------------------------------------------------- recorded

bool AUDIO_IsAudioPathOn(void)
{
    // The keyer adds a settling delay when the audio path was off. Report it as
    // already on so element timing is not skewed by that one-shot allowance;
    // tests that care drive it explicitly through the recorder.
    return true;
}

void BACKLIGHT_TurnOn(void) { }

void UART_Send(const void *data, unsigned int size)
{
    // Debug tracing only (CW_KEYER_DEBUG). Route it to the recorder so a test
    // can assert on it, and to stderr when verbose.
    SIM_RecordDebug((const char *)data, size);
}

// ---------------------------------------------------------------- storage

#define SIM_EEPROM_SIZE 0x2000
static uint8_t s_eeprom[SIM_EEPROM_SIZE];
static bool    s_eeprom_ready;

static void eeprom_init_once(void)
{
    if (!s_eeprom_ready) {
        // Erased flash reads as 0xFF; the firmware's validity checks depend on
        // that, so start from it rather than zeros.
        memset(s_eeprom, 0xFF, sizeof(s_eeprom));
        s_eeprom_ready = true;
    }
}

void EEPROM_ReadBuffer(uint16_t address, void *buffer, uint8_t size)
{
    eeprom_init_once();
    if ((uint32_t)address + size > SIM_EEPROM_SIZE) {
        memset(buffer, 0xFF, size);
        return;
    }
    memcpy(buffer, s_eeprom + address, size);
}

void EEPROM_WriteBuffer(uint16_t address, const void *buffer)
{
    // The firmware always writes 8 bytes through this entry point.
    eeprom_init_once();
    if ((uint32_t)address + 8 > SIM_EEPROM_SIZE)
        return;
    memcpy(s_eeprom + address, buffer, 8);
}

void SIM_EepromReset(void)
{
    s_eeprom_ready = false;
    eeprom_init_once();
}
