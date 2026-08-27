/* Hardware seam for the CW timing chain.
 *
 * Only the lowest layer is replaced: CW_ReadKeysForMode (raw pin state) and the
 * pin-configuration calls. The debounce and edge detection in CW_ReadKeys stay
 * compiled from the real app/cwhardware.c, because that debounce is part of the
 * timing behaviour under test -- reimplementing it here would test the
 * reimplementation instead of the firmware.
 */

#include <stdbool.h>
#include <stdint.h>

#include "app/cwhardware.h"
#include "harness/sim_paddle.h"
#include "settings.h"

// Mirrors the flag layout in settings.h.
#define CW_KEY_FLAG_REVERSED    0x01
#define CW_KEY_FLAG_PORT_RING   0x02
#define CW_KEY_FLAG_SIDE1       0x04
#define CW_KEY_FLAG_NO_KEYER    0x08
#define CW_KEY_FLAG_PORT_GROUND 0x10
#define CW_KEY_FLAG_USB_PORT    0x20

bool CW_ReadKeysForMode(uint8_t mode, bool *dit_out, bool *dah_out)
{
    // Same early-out as the real driver: handkey families have no timing
    // engine, so the iambic path must not see paddle state from them.
    if ((mode & CW_KEY_FLAG_NO_KEYER) && !(mode & CW_KEY_FLAG_PORT_GROUND)) {
        return false;
    }

    const uint32_t contacts = SIM_PaddleState();
    const bool     hw_tip   = (contacts & SIM_CONTACT_TIP) != 0;
    const bool     hw_ring  = (contacts & SIM_CONTACT_RING) != 0;
    const bool     reverse  = (mode & CW_KEY_FLAG_REVERSED) != 0;

    *dit_out = reverse ? hw_ring : hw_tip;
    *dah_out = reverse ? hw_tip : hw_ring;
    return true;
}

void CW_ReadUSBPaddleRaw(bool *tip_out, bool *ring_out)
{
    const uint32_t contacts = SIM_PaddleState();
    *tip_out  = (contacts & SIM_CONTACT_TIP) != 0;
    *ring_out = (contacts & SIM_CONTACT_RING) != 0;
}

// Pin plumbing has no meaning off-target.
void CW_ConfigurePortGround(bool enable)     { (void)enable; }
void CW_ConfigurePortRing(bool enable)       { (void)enable; }
void CW_ConfigureUsbPaddlePins(bool enable)  { (void)enable; }
