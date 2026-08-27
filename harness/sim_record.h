/* Recorder for observable firmware output.
 *
 * Deliberately records low-level intent (carrier on/off with a timestamp, the
 * decoded character stream, debug text) rather than a summarised "a dit
 * happened". Stage B swaps the stubs for real peripheral models but keeps these
 * scenarios, so the assertions have to sit at a level both can produce.
 */

#ifndef SIM_RECORD_H
#define SIM_RECORD_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    SIM_EV_CARRIER_ON,
    SIM_EV_CARRIER_OFF,
    SIM_EV_CHAR,        // a character was decoded into the TX display / macro
} SIM_EventKind_t;

typedef struct {
    SIM_EventKind_t kind;
    uint32_t        at_ms;
    char            ch;  // valid for SIM_EV_CHAR
} SIM_Event_t;

void SIM_RecordReset(void);

void SIM_RecordCarrier(bool on);
void SIM_RecordChar(char ch);
void SIM_RecordDebug(const char *text, unsigned int size);

// Decoded characters in order, NUL-terminated. This is the main assertion
// surface: feed a paddle timeline, expect "CQ".
const char *SIM_RecordedText(void);

// Element durations in milliseconds, derived from carrier on/off pairs. Lets a
// test check the dit/dah ratio and inter-element spacing rather than only the
// resulting text.
unsigned int SIM_RecordedElementCount(void);
uint32_t     SIM_RecordedElementMs(unsigned int index);
uint32_t     SIM_RecordedGapMs(unsigned int index);

unsigned int      SIM_RecordedEventCount(void);
const SIM_Event_t *SIM_RecordedEvent(unsigned int index);

// Enables echoing events and debug text to stderr as they happen.
void SIM_RecordSetVerbose(bool verbose);

#endif
