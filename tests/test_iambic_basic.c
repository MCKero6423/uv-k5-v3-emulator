/* Baseline iambic keyer behaviour: element durations and decoded characters.
 *
 * These are the assertions that would have caught the "keyer never arms" class
 * of bug on a PC in milliseconds instead of on the radio by trial and error.
 */

#include <stdio.h>
#include <string.h>

#include "harness/sim_keyer.h"
#include "harness/sim_paddle.h"
#include "harness/sim_record.h"
#include "settings.h"

static int failures;

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("  FAIL %s:%d " fmt "\n", __func__, __LINE__, __VA_ARGS__); \
            failures++;                                                        \
        }                                                                      \
    } while (0)

// Element timing is generated from a 1 ms poll, so allow a tick of slack either
// way rather than demanding an exact match.
#define NEAR(actual, expected)                                                 \
    ((actual) + 2 >= (expected) && (actual) <= (expected) + 2)

// Buttons mode: PTT is the dit paddle, SIDE1 the dah paddle.
#define KEY_BUTTONS 0x04

static void test_single_dit(void)
{
    SIM_KeyerBegin(KEY_BUTTONS, CW_IAMBIC_MODE_B, 20);
    const uint32_t dit = SIM_KeyerDitMs();

    // Hold the dit paddle briefly; the keyer times the element itself.
    SIM_PaddleTap(SIM_CONTACT_TIP, dit / 2, 0);
    SIM_KeyerRun(10 * dit);

    CHECK(SIM_RecordedElementCount() == 1, "expected 1 element, got %u",
          SIM_RecordedElementCount());
    const uint32_t len = SIM_RecordedElementMs(0);
    CHECK(NEAR(len, dit), "dit was %u ms, expected ~%u", len, dit);
}

static void test_single_dah(void)
{
    SIM_KeyerBegin(KEY_BUTTONS, CW_IAMBIC_MODE_B, 20);
    const uint32_t dit = SIM_KeyerDitMs();

    SIM_PaddleTap(SIM_CONTACT_RING, dit / 2, 0);
    SIM_KeyerRun(10 * dit);

    CHECK(SIM_RecordedElementCount() == 1, "expected 1 element, got %u",
          SIM_RecordedElementCount());
    const uint32_t len = SIM_RecordedElementMs(0);
    CHECK(NEAR(len, 3 * dit), "dah was %u ms, expected ~%u", len, 3 * dit);
}

static void test_letter_a(void)
{
    // "A" is dit-dah. Two taps with a gap shorter than the character gap.
    SIM_KeyerBegin(KEY_BUTTONS, CW_IAMBIC_MODE_B, 20);
    const uint32_t dit = SIM_KeyerDitMs();

    SIM_PaddleTap(SIM_CONTACT_TIP,  dit / 2, dit);
    SIM_PaddleTap(SIM_CONTACT_RING, dit / 2, 0);
    SIM_KeyerRun(10 * dit);

    CHECK(SIM_RecordedElementCount() == 2, "expected 2 elements, got %u",
          SIM_RecordedElementCount());
    CHECK(NEAR(SIM_RecordedElementMs(0), dit), "element 0 was %u ms, expected ~%u",
          SIM_RecordedElementMs(0), dit);
    CHECK(NEAR(SIM_RecordedElementMs(1), 3 * dit), "element 1 was %u ms, expected ~%u",
          SIM_RecordedElementMs(1), 3 * dit);
    // Inter-element gap is one dit.
    CHECK(NEAR(SIM_RecordedGapMs(0), dit), "gap was %u ms, expected ~%u",
          SIM_RecordedGapMs(0), dit);
}

static void test_wpm_scales_timing(void)
{
    // Doubling the speed halves the dit. Guards the WPM plumbing, which the
    // menu writes and the keyer reads through a separate path.
    SIM_KeyerBegin(KEY_BUTTONS, CW_IAMBIC_MODE_B, 10);
    const uint32_t slow_dit = SIM_KeyerDitMs();
    SIM_PaddleTap(SIM_CONTACT_TIP, slow_dit / 2, 0);
    SIM_KeyerRun(10 * slow_dit);
    const uint32_t slow = SIM_RecordedElementMs(0);

    SIM_KeyerBegin(KEY_BUTTONS, CW_IAMBIC_MODE_B, 20);
    const uint32_t fast_dit = SIM_KeyerDitMs();
    SIM_PaddleTap(SIM_CONTACT_TIP, fast_dit / 2, 0);
    SIM_KeyerRun(10 * fast_dit);
    const uint32_t fast = SIM_RecordedElementMs(0);

    CHECK(slow > 0 && fast > 0, "missing elements: slow=%u fast=%u", slow, fast);
    CHECK(NEAR(slow, 2 * fast), "10 WPM dit %u ms should be ~2x 20 WPM dit %u ms",
          slow, fast);
}

static void test_handkey_produces_no_elements(void)
{
    // Handkey modes have no timing engine, so the iambic path must stay silent.
    // This is the behaviour that makes macro recording impossible with a straight
    // key -- worth pinning down so it does not change by accident.
    SIM_KeyerBegin(0x08 /* NO_KEYER */, CW_IAMBIC_MODE_B, 20);
    const uint32_t dit = SIM_KeyerDitMs();

    SIM_PaddleTap(SIM_CONTACT_TIP,  dit, dit);
    SIM_PaddleTap(SIM_CONTACT_RING, dit, dit);
    SIM_KeyerRun(10 * dit);

    // The straight-key path keys the carrier directly from PTT rather than
    // producing timed elements, so no decoded characters should appear.
    CHECK(SIM_RecordedText()[0] == '\0', "handkey decoded '%s', expected nothing",
          SIM_RecordedText());
}

int main(void)
{
    printf("iambic keyer baseline\n");
    test_single_dit();
    test_single_dah();
    test_letter_a();
    test_wpm_scales_timing();
    test_handkey_produces_no_elements();

    if (failures == 0) {
        printf("  all checks passed\n");
        return 0;
    }
    printf("  %d check(s) failed\n", failures);
    return 1;
}
