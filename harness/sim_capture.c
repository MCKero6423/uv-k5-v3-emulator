/* Captures decoded characters without replacing firmware functions.
 *
 * app/cwmacro.c owns CW_AddToTxDisplay and gCW_TX_Display, and it is compiled in
 * unmodified, so the simulator cannot intercept the call. Instead the tick loop
 * samples the firmware's own display buffer once per virtual millisecond and
 * records whatever is newly appended.
 *
 * Sampling rather than intercepting has a side benefit: it observes exactly what
 * the radio would show on its centre line, including the buffer's own shifting
 * and truncation behaviour.
 */

#include <string.h>

#include "sim_record.h"

extern char gCW_TX_Display[24];

static char     s_seen[sizeof(gCW_TX_Display)];
static unsigned s_seen_len;

void SIM_CaptureReset(void)
{
    memset(s_seen, 0, sizeof(s_seen));
    s_seen_len = 0;
}

void SIM_CapturePoll(void)
{
    const unsigned len = (unsigned)strnlen(gCW_TX_Display, sizeof(gCW_TX_Display));

    if (len == s_seen_len && memcmp(s_seen, gCW_TX_Display, len) == 0)
        return;  // unchanged

    // The buffer scrolls once full, so match the longest common prefix and treat
    // the remainder as new. A scroll shortens the prefix, which is still handled
    // correctly: the shifted-in characters get recorded once.
    unsigned common = 0;
    while (common < len && common < s_seen_len && s_seen[common] == gCW_TX_Display[common])
        common++;

    for (unsigned i = common; i < len; i++)
        SIM_RecordChar(gCW_TX_Display[i]);

    memcpy(s_seen, gCW_TX_Display, len);
    s_seen[len] = '\0';
    s_seen_len  = len;
}
