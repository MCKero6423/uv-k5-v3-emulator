/* Firmware globals the CW chain reads, plus the few functions it calls that
 * belong to subsystems outside the timing path.
 *
 * Kept separate from driver_stubs.c so the two seams stay legible: that file is
 * "the driver layer", this one is "the rest of the firmware".
 */

#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "harness/sim_record.h"
#include "misc.h"
#include "py32f071_ll_gpio.h"
#include "settings.h"

// Backing storage for the fake GPIO ports. driver/gpio.h encodes a port as a
// numeric address inside an enum and casts it back with GPIO_PORT(), so the shim
// hands those addresses here rather than dereferencing them.
#define SIM_GPIO_PORT_COUNT 4
static GPIO_TypeDef s_gpio_ports[SIM_GPIO_PORT_COUNT];

GPIO_TypeDef *SIM_GpioPort(void *fake_address)
{
    // Ports are spaced 0x100 apart by the shim (A=0x000, B=0x100, C=0x200,
    // F=0x300). Anything unexpected lands on port 0 rather than faulting.
    const uintptr_t index = ((uintptr_t)fake_address >> 8) & 0x3u;
    return &s_gpio_ports[index];
}

// The real definition lives in misc.c / settings.c, which pull in most of the
// firmware. The CW chain only touches these fields.
EEPROM_Config_t gEeprom;

volatile CW_State_t gCW_State           = CW_INACTIVE;
volatile bool       gCW_KeyerUsingSD1   = false;
volatile bool       gCW_KeyerManagesPtt = false;
volatile bool       gCW_CrossMode       = false;
// Types must match misc.h exactly, including qualifiers.
volatile uint32_t   gCW_SuspendCounter_1ms;
volatile uint16_t   gCW_TxDisplayHoldoff_10ms;

// gCW_Recording, the playback flags, gCW_TX_Display and the CW_*TxDisplay
// functions are all defined by app/cwmacro.c, which is compiled in as-is.
bool gCW_FlashlightSending;
bool gCW_CpoActive;
volatile bool gCW_PlayIndicatorOn;  // owned by cwkeyer.c's playback path
bool    gUpdateDisplay;
uint8_t gUpdateStatus;  // uint8_t in misc.h, not bool

// The firmware uses a bundled printf implementation; the host's is fine here.
// Only reached from CW_KEYER_DEBUG tracing.
int sprintf_(char *buffer, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    const int n = vsprintf(buffer, format, args);
    va_end(args);
    return n;
}

// Decoded characters are captured by wrapping the real CW_AddToTxDisplay --
// see harness/sim_capture.c -- rather than replacing it, so cwmacro.c's own
// buffer management still runs.
