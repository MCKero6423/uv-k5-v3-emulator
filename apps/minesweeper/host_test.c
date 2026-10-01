/* Host harness: run the minesweeper logic on the PC, without ARM or the radio.
 * Not shipped -- it lives in work/ (git-ignored). It includes the app source directly
 * and supplies a fake app_api_t, so the game's real state machine executes: keys go in,
 * the strings it draws come out, and the framebuffer can be counted.
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

/* the app's own includes pull ../app_api.h, which resolves from this directory */
#include "minesweeper_app.c"

static uint8_t fb[64][128];
static char drawn[8192][24];   /* the game redraws every frame; 64 was my limit, not its */
static int drawn_n;
static const uint8_t *keys;
static int keys_n, keys_i;
static int tones;

static void t_clear(void) { memset(fb, 0, sizeof fb); }
static void t_tiny(const char *s, uint8_t x, uint8_t y, bool statusbar, bool fill)
{
    (void)x; (void)y; (void)statusbar; (void)fill;
    if (drawn_n < 8192) snprintf(drawn[drawn_n++], 24, "%s", s);
}
static uint8_t t_key(void) { return keys_i < keys_n ? keys[keys_i++] : APP_KEY_EXIT; }
static void t_delay(uint32_t ms) { (void)ms; }
static void t_blit(void) {}
static void t_tone(uint16_t hz, uint16_t ms) { (void)hz; (void)ms; tones++; }

static bool saw(const char *needle)
{
    for (int i = 0; i < drawn_n; i++)
        if (strcmp(drawn[i], needle) == 0)
            return true;
    return false;
}

int main(void)
{
    app_api_t api;
    memset(&api, 0, sizeof api);
    api.fb = fb;
    api.display_clear = t_clear;
    api.print_tiny = t_tiny;
    api.blit_full = t_blit;
    api.get_key = t_key;
    api.delay_ms = t_delay;
    api.play_tone = t_tone;

    /* 1) a reveal, a flag, a new game, then quit -- the state machine must return */
    static const uint8_t script1[] = { APP_KEY_MENU, APP_KEY_DOWN, APP_KEY_DOWN, APP_KEY_F,
                                       APP_KEY_STAR, APP_KEY_3, APP_KEY_5, APP_KEY_MENU,
                                       APP_KEY_EXIT };
    keys = script1; keys_n = sizeof script1; keys_i = 0; drawn_n = 0; tones = 0;
    t_clear();
    app_main(&api);
    int lit = 0;
    for (int y = 0; y < 64; y++)
        for (int x = 0; x < 128; x++)
            if (fb[y][x >> 3] & (1u << (7 - (x & 7)))) lit++;
    printf("script 1 (reveal/flag/new/digits/quit): returned normally, tones=%d\n", tones);
    printf("  title drawn : %s\n", saw("F4HWN MINES") ? "yes" : "no");
    printf("  mine count  : %s (the app draws \"M\" and the number separately)\n", saw("10") ? "yes" : "no");
    printf("  lit pixels  : %d (rendering happened: %s)\n", lit, lit > 0 ? "yes" : "NO");

    /* 2) reveal a lot of cells: the game must reach a terminal state, and a mine hit
     *    must show BOOM; then MENU must start a new game and the title must come back */
    uint8_t script2[2 + 2 * 85 + 2 + 16];   /* sized for what the loops below actually write */
    int n = 0;
    script2[n++] = APP_KEY_MENU;                 /* first reveal is safe by design */
    for (int i = 0; i < 85; i++) {               /* walk and reveal everything */
        script2[n++] = APP_KEY_DOWN;
        script2[n++] = APP_KEY_MENU;
    }
    script2[n++] = APP_KEY_MENU;                 /* after the end: new game */
    for (int i = 0; i < 12; i++) script2[n++] = APP_KEY_INVALID;
    keys = script2; keys_n = n; keys_i = 0; drawn_n = 0; tones = 0;
    t_clear();
    app_main(&api);
    printf("\nscript 2 (reveal 85 cells): tones=%d (%s)\n", tones,
           tones > 0 ? "a terminal state played a tone" : "no terminal state reached");
    printf("  BOOM drawn  : %s\n", saw("BOOM") ? "yes" : "no");
    printf("  CLEAR drawn : %s\n", saw("CLEAR") ? "yes" : "no");
    /* after a terminal state the title must come back, i.e. MENU started a new game */
    int first_title = -1, later_title = -1, terminal_at = -1;
    for (int i = 0; i < drawn_n; i++) {
        if (!strcmp(drawn[i], "F4HWN MINES")) { if (first_title < 0) first_title = i; else later_title = i; }
        if (!strcmp(drawn[i], "BOOM") && terminal_at < 0) terminal_at = i;
    }
    printf("  new game after a terminal state: %s (terminal at %d, title again at %d)\n",
           (terminal_at >= 0 && later_title > terminal_at) ? "yes" : "NO", terminal_at, later_title);
    printf("  frames drawn: %d strings recorded\n", drawn_n);
    return 0;
}
