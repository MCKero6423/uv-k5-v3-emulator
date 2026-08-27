#include "sim_record.h"

#include <stdio.h>
#include <string.h>

#include "sim_clock.h"

#define SIM_MAX_EVENTS 4096
#define SIM_MAX_TEXT   1024

static SIM_Event_t s_events[SIM_MAX_EVENTS];
static unsigned    s_event_count;
static char        s_text[SIM_MAX_TEXT];
static unsigned    s_text_len;
static bool        s_verbose;

void SIM_RecordReset(void)
{
    s_event_count = 0;
    s_text_len    = 0;
    s_text[0]     = '\0';
}

void SIM_RecordSetVerbose(bool verbose)
{
    s_verbose = verbose;
}

static void push(SIM_EventKind_t kind, char ch)
{
    if (s_event_count >= SIM_MAX_EVENTS)
        return;

    s_events[s_event_count].kind  = kind;
    s_events[s_event_count].at_ms = SIM_ClockNow();
    s_events[s_event_count].ch    = ch;
    s_event_count++;

    if (s_verbose) {
        const char *name = kind == SIM_EV_CARRIER_ON  ? "carrier on"
                         : kind == SIM_EV_CARRIER_OFF ? "carrier off"
                                                      : "char";
        if (kind == SIM_EV_CHAR)
            fprintf(stderr, "%6u ms  %s '%c'\n", SIM_ClockNow(), name, ch);
        else
            fprintf(stderr, "%6u ms  %s\n", SIM_ClockNow(), name);
    }
}

void SIM_RecordCarrier(bool on)
{
    push(on ? SIM_EV_CARRIER_ON : SIM_EV_CARRIER_OFF, 0);
}

void SIM_RecordChar(char ch)
{
    push(SIM_EV_CHAR, ch);
    if (s_text_len + 1 < SIM_MAX_TEXT) {
        s_text[s_text_len++] = ch;
        s_text[s_text_len]   = '\0';
    }
}

void SIM_RecordDebug(const char *text, unsigned int size)
{
    if (s_verbose && text != NULL && size > 0)
        fprintf(stderr, "%6u ms  [dbg] %.*s", SIM_ClockNow(), (int)size, text);
}

const char *SIM_RecordedText(void)
{
    return s_text;
}

// Walks the event list pairing each CARRIER_ON with the following CARRIER_OFF.
// Returns the duration of element `index`, or 0 when it does not exist.
static bool element_span(unsigned index, uint32_t *start, uint32_t *end)
{
    unsigned seen = 0;
    for (unsigned i = 0; i < s_event_count; i++) {
        if (s_events[i].kind != SIM_EV_CARRIER_ON)
            continue;
        for (unsigned j = i + 1; j < s_event_count; j++) {
            if (s_events[j].kind == SIM_EV_CARRIER_ON)
                break;  // unterminated; treat as incomplete
            if (s_events[j].kind == SIM_EV_CARRIER_OFF) {
                if (seen == index) {
                    *start = s_events[i].at_ms;
                    *end   = s_events[j].at_ms;
                    return true;
                }
                seen++;
                break;
            }
        }
    }
    return false;
}

unsigned int SIM_RecordedElementCount(void)
{
    unsigned n = 0;
    uint32_t a, b;
    while (element_span(n, &a, &b))
        n++;
    return n;
}

uint32_t SIM_RecordedElementMs(unsigned int index)
{
    uint32_t start, end;
    return element_span(index, &start, &end) ? end - start : 0;
}

uint32_t SIM_RecordedGapMs(unsigned int index)
{
    uint32_t s0, e0, s1, e1;
    if (!element_span(index, &s0, &e0) || !element_span(index + 1, &s1, &e1))
        return 0;
    return s1 - e0;
}

unsigned int SIM_RecordedEventCount(void)
{
    return s_event_count;
}

const SIM_Event_t *SIM_RecordedEvent(unsigned int index)
{
    return index < s_event_count ? &s_events[index] : NULL;
}
