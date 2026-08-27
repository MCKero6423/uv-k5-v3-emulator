/* Empty stand-in for an MCU header.
 *
 * app/cwkeyer.c includes several LL headers but uses no LL symbol from them
 * (verified: zero LL_* references). Providing empty headers lets the firmware
 * file compile unchanged on the host -- no edits to firmware source, so the
 * simulator cannot drift from what the radio actually runs.
 */

#ifndef PY32F0XX_H_SHIM
#define PY32F0XX_H_SHIM
#endif
