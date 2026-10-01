/* Minesweeper — overlay app for the F4HWN Labs edition.
 *
 * Written against App/apps/app_api.h. The constraints shaped it:
 *   - the radio has NO left/right keys (UP, DOWN, MENU, EXIT, STAR, F, 0-9 only), so the
 *     cursor walks the field with UP/DOWN and digits jump straight to a row and column
 *   - 4 KiB for text+rodata+data+bss together: no lookup tables, no floats, no libc,
 *     adjacency counted on the fly, one bit per cell
 *   - the resident pixel helpers do not bound-check, so put() clips
 *
 * Keys: UP/DOWN move the cursor, 1-9 pick row then column (3 then 5 = row 3, col 5),
 *       MENU reveal, F flag, STAR new game, EXIT quit. The first reveal is always safe:
 *       the mines are placed after it, keeping the 3x3 around it clear.
 *
 * The 81 cells do not fit in any single integer type this chip shifts cheaply, so the
 * three cell sets are 9-byte bit arrays addressed by cell index (cell >> 3, cell & 7).
 * A uint16_t version of this compiled fine and would have been wrong past cell 15.
 */
#include <stdint.h>
#include <stdbool.h>
#include "../app_api.h"

#define N        9                  /* 9x9: cell pitch 6 px -> 54x54 on a 128x64 screen */
#define PITCH    6
#define FIELD_X  4
#define FIELD_Y  10
#define MINES    10
#define CELLS    (N * N)
#define BYTES    ((CELLS + 7) / 8)

static const app_api_t *A;

static uint8_t g_mine[BYTES];
static uint8_t g_open[BYTES];
static uint8_t g_flag[BYTES];
static uint8_t g_cursor;
static uint8_t g_pending;           /* 0 = no digit typed, 1 = row typed */
static uint8_t g_row_pick;
static uint8_t g_placed;
static uint8_t g_state;             /* 0 play, 1 lost, 2 won */
static int8_t  g_mine_left;

static bool bit(const uint8_t *set, uint8_t cell)
{
    return ((set[cell >> 3] >> (cell & 7)) & 1u) != 0;
}

static void setbit(uint8_t *set, uint8_t cell, bool on)
{
    uint8_t mask = (uint8_t)(1u << (cell & 7));
    if (on)
        set[cell >> 3] |= mask;
    else
        set[cell >> 3] &= (uint8_t)~mask;
}

static void put(int16_t x, int16_t y, bool ink)
{
    if (x < 0 || x >= 128 || y < 0 || y >= 64)
        return;                                  /* the API's helpers do not clip */
    if (ink)
        A->fb[y][x >> 3] |= (uint8_t)(1u << (7u - (x & 7)));
    else
        A->fb[y][x >> 3] &= (uint8_t)~(1u << (7u - (x & 7)));
}

static void invert(int16_t x, int16_t y)
{
    if (x < 0 || x >= 128 || y < 0 || y >= 64)
        return;
    A->fb[y][x >> 3] ^= (uint8_t)(1u << (7u - (x & 7)));
}

static void box(int16_t x0, int16_t y0, int16_t x1, int16_t y1, bool ink)
{
    for (int16_t x = x0; x <= x1; x++) {
        put(x, y0, ink);
        put(x, y1, ink);
    }
    for (int16_t y = y0; y <= y1; y++) {
        put(x0, y, ink);
        put(x1, y, ink);
    }
}

static int8_t neighbours(uint8_t cell)
{
    int8_t row = (int8_t)(cell / N);
    int8_t col = (int8_t)(cell % N);
    int8_t count = 0;

    for (int8_t dy = -1; dy <= 1; dy++) {
        for (int8_t dx = -1; dx <= 1; dx++) {
            int8_t r = (int8_t)(row + dy);
            int8_t c = (int8_t)(col + dx);
            if ((dx == 0 && dy == 0) || r < 0 || r >= N || c < 0 || c >= N)
                continue;
            if (bit(g_mine, (uint8_t)(r * N + c)))
                count++;
        }
    }
    return count;
}

static void place_mines(uint8_t safe)
{
    /* No rand() in a freestanding blob, so a cheap LCG. Seeded from the first reveal and
     * a counter so two games in a row differ. */
    static uint32_t seed = 1u;
    int8_t want = MINES;

    for (uint8_t i = 0; i < BYTES; i++)
        g_mine[i] = 0;

    seed += (uint32_t)safe * 2654435761u + 1u;
    while (want > 0) {
        seed = seed * 1103515245u + 12345u;
        uint8_t cell = (uint8_t)((seed >> 16) % CELLS);
        int8_t row = (int8_t)(cell / N), col = (int8_t)(cell % N);
        int8_t srow = (int8_t)(safe / N), scol = (int8_t)(safe % N);

        if (bit(g_mine, cell))
            continue;
        /* the 3x3 around the first reveal stays clear, so it cannot lose at once */
        if (row >= srow - 1 && row <= srow + 1 && col >= scol - 1 && col <= scol + 1)
            continue;
        setbit(g_mine, cell, true);
        want--;
    }
    g_placed = 1;
}

static void reveal(uint8_t cell)
{
    /* Iterative flood fill: an explicit stack of cell indices, no recursion. */
    static uint8_t stack[CELLS];
    int16_t top = 0;

    stack[top++] = cell;
    while (top > 0) {
        uint8_t cur = stack[--top];

        if (bit(g_open, cur))
            continue;
        setbit(g_open, cur, true);
        if (neighbours(cur) != 0)
            continue;

        int8_t row = (int8_t)(cur / N), col = (int8_t)(cur % N);
        for (int8_t dy = -1; dy <= 1; dy++) {
            for (int8_t dx = -1; dx <= 1; dx++) {
                int8_t r = (int8_t)(row + dy), c = (int8_t)(col + dx);
                if (r < 0 || r >= N || c < 0 || c >= N)
                    continue;
                uint8_t next = (uint8_t)(r * N + c);
                if (!bit(g_open, next) && !bit(g_flag, next) && top < CELLS)
                    stack[top++] = next;
            }
        }
    }
}

static void new_game(void)
{
    for (uint8_t i = 0; i < BYTES; i++) {
        g_mine[i] = 0;
        g_open[i] = 0;
        g_flag[i] = 0;
    }
    g_cursor = (uint8_t)(4 * N + 4);
    g_pending = 0;
    g_row_pick = 0;
    g_placed = 0;
    g_state = 0;
    g_mine_left = MINES;
}

static void step(int8_t delta)
{
    int8_t cell = (int8_t)((int8_t)g_cursor + delta);
    if (cell < 0)
        cell = (int8_t)(CELLS - 1);
    if (cell >= CELLS)
        cell = 0;
    g_cursor = (uint8_t)cell;
}

static void two_digits(char *out, int8_t value)
{
    if (value < 0)
        value = 0;
    if (value > 99)
        value = 99;
    out[0] = (char)('0' + (value / 10) % 10);
    out[1] = (char)('0' + value % 10);
    out[2] = 0;
}

static void draw(void)
{
    char text[3];

    A->display_clear();

    A->print_tiny("M", 0, 1, false, false);
    two_digits(text, g_mine_left);
    A->print_tiny(text, 8, 1, false, false);

    if (g_state == 1)
        A->print_tiny("BOOM", 46, 1, false, false);
    else if (g_state == 2)
        A->print_tiny("CLEAR", 42, 1, false, false);
    else
        A->print_tiny("F4HWN MINES", 34, 1, false, false);

    text[0] = (char)('A' + (g_cursor / N));
    text[1] = (char)('1' + (g_cursor % N));
    text[2] = 0;
    A->print_tiny(text, 110, 1, false, false);
    if (g_pending)
        A->print_tiny("-", 122, 1, false, false);

    for (uint8_t cell = 0; cell < CELLS; cell++) {
        int16_t x = (int16_t)(FIELD_X + (cell % N) * PITCH);
        int16_t y = (int16_t)(FIELD_Y + (cell / N) * PITCH);
        bool opened = bit(g_open, cell);
        bool mine = bit(g_mine, cell);
        /* mines show once the game is over, whether or not they were flagged */
        bool show_mine = mine && (g_state != 0 || opened);

        if (show_mine) {
            box(x, y, (int16_t)(x + 4), (int16_t)(y + 4), true);
            put((int16_t)(x + 2), (int16_t)(y + 2), false);
        } else if (opened) {
            int8_t n = neighbours(cell);
            if (n > 0) {
                text[0] = (char)('0' + n);
                text[1] = 0;
                A->print_tiny(text, (uint8_t)x, (uint8_t)y, false, false);
            }
        } else if (bit(g_flag, cell)) {
            box((int16_t)(x + 1), (int16_t)(y + 1), (int16_t)(x + 3), (int16_t)(y + 3), true);
            put((int16_t)(x + 2), (int16_t)(y + 4), true);
        }
    }

    /* the cursor inverts the frame around its cell, so it shows on ink and on paper */
    int16_t cx = (int16_t)(FIELD_X + (g_cursor % N) * PITCH - 1);
    int16_t cy = (int16_t)(FIELD_Y + (g_cursor / N) * PITCH - 1);
    for (int16_t i = 0; i <= PITCH + 1; i++) {
        invert((int16_t)(cx + i), cy);
        invert((int16_t)(cx + i), (int16_t)(cy + PITCH + 1));
        invert(cx, (int16_t)(cy + i));
        invert((int16_t)(cx + PITCH + 1), (int16_t)(cy + i));
    }

    A->blit_full();
}

static void check_win(void)
{
    int16_t closed = 0;

    for (uint8_t cell = 0; cell < CELLS; cell++)
        if (!bit(g_open, cell) && !bit(g_mine, cell))
            closed++;
    if (closed == 0) {
        g_state = 2;
        A->play_tone(880, 120);
        A->play_tone(1320, 160);
    }
}

void app_main(const app_api_t *api)
{
    A = api;
    new_game();

    while (true) {
        draw();

        uint8_t key = A->get_key();
        if (key == APP_KEY_INVALID || key == APP_KEY_SAVER) {
            A->delay_ms(40);
            continue;
        }
        if (key == APP_KEY_EXIT)
            return;                                   /* the loader restores the radio */
        if (key == APP_KEY_STAR) {
            new_game();
            continue;
        }
        if (g_state != 0) {                           /* after BOOM or CLEAR, MENU restarts */
            if (key == APP_KEY_MENU)
                new_game();
            continue;
        }
        if (key == APP_KEY_UP) {
            step(-1);
            continue;
        }
        if (key == APP_KEY_DOWN) {
            step(1);
            continue;
        }
        if (key >= APP_KEY_1 && key <= APP_KEY_9) {
            uint8_t digit = (uint8_t)(key - APP_KEY_0);           /* 1..9 */
            if (!g_pending) {
                g_row_pick = digit;                                /* row first ... */
                g_pending = 1;
            } else {
                g_cursor = (uint8_t)((g_row_pick - 1) * N + (digit - 1));  /* ... then column */
                g_pending = 0;
            }
            continue;
        }
        if (key == APP_KEY_F) {                                    /* flag */
            if (!bit(g_open, g_cursor)) {
                if (bit(g_flag, g_cursor)) {
                    setbit(g_flag, g_cursor, false);
                    g_mine_left++;
                } else if (g_mine_left > 0) {
                    setbit(g_flag, g_cursor, true);
                    g_mine_left--;
                }
            }
            continue;
        }
        if (key == APP_KEY_MENU) {                                 /* reveal */
            if (bit(g_flag, g_cursor))
                continue;
            if (!g_placed)
                place_mines(g_cursor);
            if (bit(g_mine, g_cursor)) {
                setbit(g_open, g_cursor, true);
                g_state = 1;
                A->play_tone(160, 400);
                continue;
            }
            reveal(g_cursor);
            check_win();
        }
    }
}
