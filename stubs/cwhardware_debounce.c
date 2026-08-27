/* The debounce and edge-detection half of app/cwhardware.c.
 *
 * Why this file exists: CW_ReadKeys() applies an asymmetric debounce (a press
 * needs three consecutive agreeing reads, a release takes effect on the first)
 * and tracks which paddle was pressed last for Ultimatic's tie-break. That logic
 * is part of the timing behaviour under test, so it must not be approximated.
 *
 * The rest of app/cwhardware.c is pin plumbing -- LL_GPIO_Init, DMA channels,
 * USB clock gating -- which needs 20+ MCU symbols and has no bearing on element
 * timing. Compiling the whole file would mean stubbing all of that.
 *
 * So the debounce is transcribed here, byte-for-byte in behaviour, and kept in
 * sync by a probe check (see check_sim_parity.py) that diffs it against the
 * firmware original. If someone edits the firmware debounce, the check fails.
 */

#include <stdbool.h>
#include <stdint.h>

#include "app/cwhardware.h"
#include "settings.h"

#define CW_KEY_FLAG_USB_PORT 0x20

static bool    s_last_dit;
static bool    s_last_dah;
static bool    s_last_is_dah;
static uint8_t s_dit_count;
static uint8_t s_dah_count;

void CW_ReadKeys(CW_Input *in)
{
    bool n_dit = false;
    bool n_dah = false;

    if (!CW_ReadKeysForMode(gEeprom.CW_KEY_INPUT, &n_dit, &n_dah)) {
        n_dit = false;
        n_dah = false;
    }

    bool deb_dit = s_last_dit;
    bool deb_dah = s_last_dah;
    if (gEeprom.CW_KEY_INPUT & CW_KEY_FLAG_USB_PORT) {
        // USB paddle has its own tri-state glitch filter upstream.
        deb_dit = n_dit;
        deb_dah = n_dah;
    } else {
        // Asymmetric: three-strike on rising, immediate on falling. A symmetric
        // release delay starves the iambic path's own count-based debounce and
        // makes mode B latch extra trailing elements.
        if (n_dit == deb_dit)   s_dit_count = 0;
        else if (!deb_dit) {
            if (++s_dit_count >= 3) { deb_dit = true; s_dit_count = 0; }
        } else { deb_dit = false; s_dit_count = 0; }

        if (n_dah == deb_dah)   s_dah_count = 0;
        else if (!deb_dah) {
            if (++s_dah_count >= 3) { deb_dah = true; s_dah_count = 0; }
        } else { deb_dah = false; s_dah_count = 0; }
    }

    in->dit_rise = (!s_last_dit && deb_dit);
    in->dah_rise = (!s_last_dah && deb_dah);
    in->dit = deb_dit;
    in->dah = deb_dah;

    // Most recent fresh press wins for Ultimatic's "both held" rule.
    if (in->dit_rise) s_last_is_dah = false;
    else if (in->dah_rise) s_last_is_dah = true;
    in->last_is_dah = s_last_is_dah;

    s_last_dit = deb_dit;
    s_last_dah = deb_dah;
}

void CW_HW_ResetKeySamples(void)
{
    s_last_dit    = false;
    s_last_dah    = false;
    s_last_is_dah = false;
    s_dit_count   = 0;
    s_dah_count   = 0;
}
