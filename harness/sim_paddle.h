/* Scripted paddle / straight-key input.
 *
 * Replaces the GPIO reads in app/cwhardware.c. A scenario is written as a list
 * of "hold these contacts for N ms" steps, which is how an operator's hand
 * actually looks to the keyer, and the harness plays it against the virtual
 * clock.
 *
 * Contacts are named after the hardware: TIP is dit by default, RING is dah,
 * and the keyer's REVERSED flag swaps them. PTT doubles as the dit paddle in
 * Buttons mode and as the straight key in handkey modes, so it is tracked
 * separately rather than folded into TIP.
 */

#ifndef SIM_PADDLE_H
#define SIM_PADDLE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    SIM_CONTACT_NONE = 0,
    SIM_CONTACT_TIP  = 1u << 0,  // dit paddle (PTT in Buttons mode)
    SIM_CONTACT_RING = 1u << 1,  // dah paddle (SIDE1 in Buttons mode)
} SIM_Contact_t;

void SIM_PaddleReset(void);

// Queues "hold this contact set for duration_ms". Steps play in order; the
// timeline holds the last state once exhausted (i.e. keys released).
void SIM_PaddleHold(uint32_t contacts, uint32_t duration_ms);

// Convenience for the common "press, then release" pair.
void SIM_PaddleTap(uint32_t contacts, uint32_t hold_ms, uint32_t gap_ms);

// Current contact state, resolved against the virtual clock. The cwhardware
// stubs call this; tests normally use the queueing functions above.
uint32_t SIM_PaddleState(void);

// True when every queued step has played out.
bool SIM_PaddleDrained(void);

// Total queued duration, so a test can run the clock exactly long enough.
uint32_t SIM_PaddleTotalMs(void);

#endif
