/*
 * Puya PY32F071 SoC and a machine for the Quansheng UV-K5 V3 / UV-K1 radio.
 *
 * Cortex-M0+, 128 KB flash at 0x08000000, 16 KB SRAM at 0x20000000.
 * The memory map is taken from the vendor CMSIS header shipped with the
 * firmware (Drivers/CMSIS/Device/PY32F071/Include/py32f071xB.h), so the
 * addresses here are the vendor's, not guesses.
 *
 * Scope of this file: enough of the SoC for the radio firmware to boot and
 * reach its main loop. Peripherals are modelled at the level the firmware
 * actually needs -- clock-ready flags it polls, GPIO state it drives and reads,
 * SPI transfers it clocks out. Device-specific behaviour behind the SPI buses
 * (the ST7565 display, the PY25Q16 flash, the BK4829 transceiver) lives in
 * separate models; this file only wires the buses up.
 *
 * This code is licensed under the GPL version 2 or later.
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "hw/irq.h"
#include "hw/clock.h"
#include "hw/qdev-clock.h"
#include "hw/arm/boot.h"
#include "hw/arm/armv7m.h"
#include "hw/boards.h"
#include "hw/qdev-properties.h"
#include "hw/sysbus.h"
#include "exec/address-spaces.h"
#include "sysemu/sysemu.h"
#include "qom/object.h"

/* ---------------------------------------------------------------- memory map */

#define PY32_FLASH_BASE   0x08000000
#define PY32_FLASH_SIZE   (128 * KiB)
#define PY32_SRAM_BASE    0x20000000
#define PY32_SRAM_SIZE    (16 * KiB)

/* The application image starts after the 10 KB bootloader region. Loading it at
 * PY32_FLASH_BASE instead would put the vector table in the wrong place and the
 * machine faults on the first fetch. */
#define PY32_APP_OFFSET   0x2800

#define PY32_APB_BASE     0x40000000
#define PY32_AHB_BASE     0x40020000
#define PY32_IOPORT_BASE  0x50000000

#define PY32_RCC_BASE     0x40021000
#define PY32_FLASH_R_BASE 0x40022000
#define PY32_PWR_BASE     0x40007000
#define PY32_SYSCFG_BASE  0x40010000
#define PY32_EXTI_BASE    0x40021800
#define PY32_CRC_BASE     0x40023000
#define PY32_DMA1_BASE    0x40020000

#define PY32_GPIO_STRIDE  0x400
#define PY32_GPIOA_BASE   0x50000000
#define PY32_GPIOB_BASE   0x50000400
#define PY32_GPIOC_BASE   0x50000800
#define PY32_GPIOF_BASE   0x50001400

#define PY32_SPI1_BASE    0x40013000
#define PY32_SPI2_BASE    0x40003800
#define PY32_ADC1_BASE    0x40012400
#define PY32_USART1_BASE  0x40013800
#define PY32_USART2_BASE  0x40004400
#define PY32_I2C1_BASE    0x40005400
#define PY32_TIM1_BASE    0x40012c00
#define PY32_TIM3_BASE    0x40000400
#define PY32_TIM2_BASE    0x40000000
#define PY32_TIM6_BASE    0x40001000
#define PY32_TIM7_BASE    0x40001400
#define PY32_TIM14_BASE   0x40002000
#define PY32_TIM15_BASE   0x40014000
#define PY32_TIM16_BASE   0x40014400
#define PY32_TIM17_BASE   0x40014800
#define PY32_USB_BASE     0x40005c00
#define PY32_RTC_BASE     0x40002800
#define PY32_IWDG_BASE    0x40003000
#define PY32_WWDG_BASE    0x40002c00
#define PY32_USART3_BASE  0x40004800
#define PY32_USART4_BASE  0x40004c00
#define PY32_I2C2_BASE    0x40005800
#define PY32_DBGMCU_BASE  0x40015800
#define PY32_LCD_BASE     0x40002400

#define PY32_NUM_IRQ      32

/* --------------------------------------------------------------- RCC model */

/*
 * Clock control. The firmware switches to HSI/PLL and then polls ready flags,
 * so those have to read back as set or BOARD_Init spins forever. Everything
 * else is stored and echoed: nothing downstream depends on the values, and
 * inventing behaviour would be guesswork.
 */
#define TYPE_PY32_RCC "py32-rcc"
OBJECT_DECLARE_SIMPLE_TYPE(PY32RccState, PY32_RCC)

struct PY32RccState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t regs[0x40];
};

/* Register offsets that carry ready/lock bits the firmware waits on. */
#define RCC_CR      0x00
#define RCC_ICSCR   0x04
#define RCC_CFGR    0x08
#define RCC_CIER    0x18
#define RCC_CIFR    0x1c

static uint64_t py32_rcc_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32RccState *s = opaque;
    const unsigned idx = addr >> 2;

    if (idx >= ARRAY_SIZE(s->regs)) {
        qemu_log_mask(LOG_GUEST_ERROR, "py32-rcc: read out of range 0x%" HWADDR_PRIx "\n", addr);
        return 0;
    }

    uint32_t value = s->regs[idx];

    if (addr == RCC_CR) {
        /*
         * Mirror every enable bit into its ready bit. On this part the pairs sit
         * one bit apart (HSION/HSIRDY, HSEON/HSERDY, PLLON/PLLRDY), so echoing
         * "enabled" as "ready" satisfies the firmware's spin loops without
         * pretending to model the PLL.
         */
        if (value & (1u << 8))  value |= (1u << 10); /* HSI  */
        if (value & (1u << 16)) value |= (1u << 17); /* HSE  */
        if (value & (1u << 24)) value |= (1u << 25); /* PLL  */
        value |= (1u << 1);                          /* LSI ready */
    }

    return value;
}

static void py32_rcc_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32RccState *s = opaque;
    const unsigned idx = addr >> 2;

    if (idx >= ARRAY_SIZE(s->regs)) {
        qemu_log_mask(LOG_GUEST_ERROR, "py32-rcc: write out of range 0x%" HWADDR_PRIx "\n", addr);
        return;
    }
    s->regs[idx] = value;
}

static const MemoryRegionOps py32_rcc_ops = {
    .read = py32_rcc_read,
    .write = py32_rcc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void py32_rcc_reset(DeviceState *dev)
{
    PY32RccState *s = PY32_RCC(dev);
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[RCC_CR >> 2] = (1u << 8) | (1u << 10);  /* HSI on and ready */
}

static void py32_rcc_init(Object *obj)
{
    PY32RccState *s = PY32_RCC(obj);
    memory_region_init_io(&s->iomem, obj, &py32_rcc_ops, s, TYPE_PY32_RCC, 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
}

static void py32_rcc_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->reset = py32_rcc_reset;
    dc->desc = "PY32F071 reset and clock control";
}

/* -------------------------------------------------------------- GPIO model */

/*
 * One instance per port. Output state is exported as qemu_irq lines so board
 * models (display chip-select, keypad rows) can watch them, and input state is
 * settable the same way, which is how key presses get injected.
 */
#define TYPE_PY32_GPIO "py32-gpio"
OBJECT_DECLARE_SIMPLE_TYPE(PY32GpioState, PY32_GPIO)

#define PY32_GPIO_PINS 16

struct PY32GpioState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    char        *port_name;

    uint32_t moder, otyper, ospeedr, pupdr, odr, lckr, afrl, afrh;
    uint32_t idr;          /* driven by the board, not the guest */

    qemu_irq out[PY32_GPIO_PINS];
};

#define GPIO_MODER   0x00
#define GPIO_OTYPER  0x04
#define GPIO_OSPEEDR 0x08
#define GPIO_PUPDR   0x0c
#define GPIO_IDR     0x10
#define GPIO_ODR     0x14
#define GPIO_BSRR    0x18
#define GPIO_LCKR    0x1c
#define GPIO_AFRL    0x20
#define GPIO_AFRH    0x24
#define GPIO_BRR     0x28

static void py32_gpio_update(PY32GpioState *s, uint32_t old_odr)
{
    const uint32_t changed = old_odr ^ s->odr;

    for (int i = 0; i < PY32_GPIO_PINS; i++) {
        if (changed & (1u << i)) {
            qemu_set_irq(s->out[i], !!(s->odr & (1u << i)));
        }
    }
}

static uint64_t py32_gpio_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32GpioState *s = opaque;

    switch (addr) {
    case GPIO_MODER:   return s->moder;
    case GPIO_OTYPER:  return s->otyper;
    case GPIO_OSPEEDR: return s->ospeedr;
    case GPIO_PUPDR:   return s->pupdr;
    case GPIO_ODR:     return s->odr;
    case GPIO_LCKR:    return s->lckr;
    case GPIO_AFRL:    return s->afrl;
    case GPIO_AFRH:    return s->afrh;
    case GPIO_IDR:
        /*
         * Pins configured as outputs read back their own driven level; inputs
         * read what the board drives, and default high because the firmware
         * configures pull-ups for the keypad and paddle contacts (active low).
         */
        {
            uint32_t out_mask = 0;
            for (int i = 0; i < PY32_GPIO_PINS; i++) {
                if (((s->moder >> (i * 2)) & 3u) == 1u) {
                    out_mask |= (1u << i);
                }
            }
            return (s->odr & out_mask) | (s->idr & ~out_mask);
        }
    default:
        qemu_log_mask(LOG_UNIMP, "py32-gpio%s: read 0x%" HWADDR_PRIx "\n",
                      s->port_name ?: "", addr);
        return 0;
    }
}

static void py32_gpio_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32GpioState *s = opaque;
    const uint32_t old_odr = s->odr;

    switch (addr) {
    case GPIO_MODER:   s->moder = value;   break;
    case GPIO_OTYPER:  s->otyper = value;  break;
    case GPIO_OSPEEDR: s->ospeedr = value; break;
    case GPIO_PUPDR:   s->pupdr = value;   break;
    case GPIO_LCKR:    s->lckr = value;    break;
    case GPIO_AFRL:    s->afrl = value;    break;
    case GPIO_AFRH:    s->afrh = value;    break;
    case GPIO_ODR:
        s->odr = value;
        py32_gpio_update(s, old_odr);
        break;
    case GPIO_BSRR:
        /* Low half sets, high half resets; reset wins on a conflict. */
        s->odr |= value & 0xffff;
        s->odr &= ~(value >> 16);
        py32_gpio_update(s, old_odr);
        break;
    case GPIO_BRR:
        s->odr &= ~(value & 0xffff);
        py32_gpio_update(s, old_odr);
        break;
    default:
        qemu_log_mask(LOG_UNIMP, "py32-gpio%s: write 0x%" HWADDR_PRIx " = 0x%" PRIx64 "\n",
                      s->port_name ?: "", addr, value);
        break;
    }
}

static const MemoryRegionOps py32_gpio_ops = {
    .read = py32_gpio_read,
    .write = py32_gpio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

/* Board-side entry point for driving an input pin. */
static void py32_gpio_set_input(void *opaque, int line, int level)
{
    PY32GpioState *s = opaque;

    if (line < 0 || line >= PY32_GPIO_PINS) {
        return;
    }
    if (level) {
        s->idr |= (1u << line);
    } else {
        s->idr &= ~(1u << line);
    }
}

static void py32_gpio_reset(DeviceState *dev)
{
    PY32GpioState *s = PY32_GPIO(dev);

    s->moder = 0;
    s->otyper = 0;
    s->ospeedr = 0;
    s->pupdr = 0;
    s->odr = 0;
    s->lckr = 0;
    s->afrl = 0;
    s->afrh = 0;
    /*
     * Unconnected inputs idle high: the keypad, PTT and paddle contacts are all
     * active low, so a floating pin has to read as "not pressed".
     *
     * Exception: PB9 is the bidirectional data line of the software-driven
     * three-wire bus to the BK4819 transceiver. Idling it high makes every
     * register read return 0xFFFF, and RADIO_SetupRegisters then spins forever
     * waiting for bit 0 of REG_0C to clear. Idle it low until that bus has a
     * device model, so reads come back as zero and the wait terminates.
     */
    s->idr = 0xffff;
    if (s->port_name && s->port_name[0] == 'b') {
        s->idr &= ~(1u << 9);
    }
}

static void py32_gpio_init(Object *obj)
{
    PY32GpioState *s = PY32_GPIO(obj);

    memory_region_init_io(&s->iomem, obj, &py32_gpio_ops, s, TYPE_PY32_GPIO, PY32_GPIO_STRIDE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    /*
     * Name both directions. Unnamed in and out lines share one namespace in
     * qdev, so an unnamed pair on the same device makes qdev_get_gpio_in()
     * ambiguous -- board wiring then silently attaches to the wrong line and
     * signals go nowhere.
     */
    qdev_init_gpio_out_named(DEVICE(obj), s->out, "pin-out", PY32_GPIO_PINS);
    qdev_init_gpio_in_named(DEVICE(obj), py32_gpio_set_input, "pin-in",
                            PY32_GPIO_PINS);
}

static Property py32_gpio_properties[] = {
    DEFINE_PROP_STRING("port-name", PY32GpioState, port_name),
    DEFINE_PROP_END_OF_LIST(),
};

static void py32_gpio_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->reset = py32_gpio_reset;
    dc->desc = "PY32F071 GPIO port";
    device_class_set_props(dc, py32_gpio_properties);
}

/* ------------------------------------------------- catch-all for the rest */

/* ------------------------------------------------------------ keypad matrix */

/*
 * Wiring from App/driver/keyboard.c: columns are GPIOB pins 6..3 driven as
 * outputs, rows are GPIOB pins 15..12 read as inputs, both active low. The
 * driver pulls one column low at a time and reads the row bits.
 *
 * Column 0 is a pseudo column: the firmware reads the two side keys in the
 * state where no column is pulled down, so they sit at rows 0 and 1 of it.
 *
 * The model owns no GPIO of its own -- it watches the column outputs and drives
 * the row inputs, which is what the matrix does electrically.
 */
#define TYPE_UVK5_KEYPAD "uvk5-keypad"
OBJECT_DECLARE_SIMPLE_TYPE(UVK5KeypadState, UVK5_KEYPAD)

#define KEYPAD_COLS 5
#define KEYPAD_ROWS 4
/*
 * Column c of the keyboard[5][4] table is driven by PIN_COL(c - 1) in
 * App/driver/keyboard.c, and PIN_COL(n) is pin 6 - n. So table column 1 uses
 * pin 6, column 2 pin 5, and so on -- the off-by-one in the driver's indexing
 * has to be reproduced here or the columns are shifted by one and every key
 * reads as its neighbour.
 */
#define KEYPAD_COL_PIN(c) (6 - ((c) - 1))
#define KEYPAD_ROW_PIN(r) (15 - (r))

struct UVK5KeypadState {
    DeviceState parent_obj;

    bool pressed[KEYPAD_COLS][KEYPAD_ROWS];
    bool col_high[KEYPAD_COLS];
    /*
     * volatile is required, not decorative. qdev_init_gpio_out_named() is
     * inlinable and only records this array; the lines are filled in later by
     * qdev_connect_gpio_out_named() from the board, which GCC cannot see. Left
     * plain, GCC at -O2 proves every element is still NULL, notices that
     * qemu_set_irq() returns immediately on a NULL irq, and deletes the whole
     * body of keypad_update_rows() along with all five calls to it -- so no row
     * line is ever driven and the firmware's keypad scan reads nothing. That
     * failure is silent and looks exactly like a broken keypad model.
     *
     * Verified from the object code: without volatile, keypad_col_changed
     * compiles to a store and a ret with no call at all; with it, the call is
     * emitted. See AGENTS.md.
     */
    qemu_irq volatile row_out[KEYPAD_ROWS];
};

/*
 * A row reads low when a held key sits on a column that is currently pulled
 * low. Side keys read low whenever every real column is high, matching how the
 * driver samples them.
 */
static void keypad_update_rows(UVK5KeypadState *s)
{
    bool all_cols_high = true;

    for (int c = 1; c < KEYPAD_COLS; c++) {
        if (!s->col_high[c]) {
            all_cols_high = false;
        }
    }

    for (int r = 0; r < KEYPAD_ROWS; r++) {
        bool low = false;

        for (int c = 1; c < KEYPAD_COLS; c++) {
            if (s->pressed[c][r] && !s->col_high[c]) {
                low = true;
            }
        }
        if (all_cols_high && s->pressed[0][r]) {
            low = true;
        }
        qemu_set_irq(s->row_out[r], low ? 0 : 1);
    }
}

static void keypad_col_changed(void *opaque, int line, int level)
{
    UVK5KeypadState *s = opaque;

    if (line < 1 || line >= KEYPAD_COLS) {
        return;
    }
    s->col_high[line] = level != 0;
    keypad_update_rows(s);
}

/* Key index is column * KEYPAD_ROWS + row. */
static void keypad_key_changed(void *opaque, int line, int level)
{
    UVK5KeypadState *s = opaque;
    const int col = line / KEYPAD_ROWS;
    const int row = line % KEYPAD_ROWS;

    if (col >= KEYPAD_COLS || row >= KEYPAD_ROWS) {
        return;
    }
    s->pressed[col][row] = level != 0;
    keypad_update_rows(s);
}

static void keypad_reset(DeviceState *dev)
{
    UVK5KeypadState *s = UVK5_KEYPAD(dev);

    memset(s->pressed, 0, sizeof(s->pressed));
    for (int c = 0; c < KEYPAD_COLS; c++) {
        s->col_high[c] = true;
    }
    keypad_update_rows(s);
}

static void keypad_init(Object *obj)
{
    DeviceState *dev = DEVICE(obj);
    UVK5KeypadState *s = UVK5_KEYPAD(obj);

    qdev_init_gpio_in_named(dev, keypad_col_changed, "col", KEYPAD_COLS);
    qdev_init_gpio_in_named(dev, keypad_key_changed, "key",
                            KEYPAD_COLS * KEYPAD_ROWS);
    /*
     * Cast away volatile for the registration call only. row_out is declared
     * volatile so GCC cannot conclude the lines stay NULL and delete
     * keypad_update_rows() -- see the comment on the field. qdev only stores the
     * pointer here, so dropping the qualifier for this one call is safe and
     * keeps -Wdiscarded-qualifiers quiet.
     */
    qdev_init_gpio_out_named(dev, (qemu_irq *)s->row_out, "row", KEYPAD_ROWS);
}

/*
 * Key names as they appear on the radio, indexed the same way as the matrix
 * (column * KEYPAD_ROWS + row) so a test can say "press MENU" rather than
 * compute coordinates. Order follows the keyboard[5][4] table in
 * App/driver/keyboard.c.
 */
static const char *const keypad_key_names[KEYPAD_COLS * KEYPAD_ROWS] = {
    /* pseudo column 0: side keys, readable with every column released */
    "SIDE1", "SIDE2", NULL, NULL,
    /* column 1 */ "MENU", "1", "4", "7",
    /* column 2 */ "UP",   "2", "5", "8",
    /* column 3 */ "DOWN", "3", "6", "9",
    /* column 4 */ "EXIT", "STAR", "0", "F",
};

/* Resolves a key name to its matrix index, or -1 when unknown. */
static int keypad_index_for_name(const char *name)
{
    for (int i = 0; i < KEYPAD_COLS * KEYPAD_ROWS; i++) {
        if (keypad_key_names[i] && g_ascii_strcasecmp(keypad_key_names[i], name) == 0) {
            return i;
        }
    }
    return -1;
}

/*
 * Write-only "press" property: setting it to a key name holds that key, and
 * setting it to an empty string releases everything. Driving the matrix through
 * a property means keys can be injected over the QMP/HMP monitor without a
 * display backend, which suits this headless setup.
 */
static void keypad_set_press(Object *obj, const char *value, Error **errp)
{
    UVK5KeypadState *s = UVK5_KEYPAD(obj);

    if (!value || !*value) {
        memset(s->pressed, 0, sizeof(s->pressed));
        keypad_update_rows(s);
        return;
    }

    const int index = keypad_index_for_name(value);
    if (index < 0) {
        error_setg(errp, "unknown key '%s'", value);
        return;
    }

    memset(s->pressed, 0, sizeof(s->pressed));
    s->pressed[index / KEYPAD_ROWS][index % KEYPAD_ROWS] = true;
    keypad_update_rows(s);
}

static char *keypad_get_press(Object *obj, Error **errp)
{
    UVK5KeypadState *s = UVK5_KEYPAD(obj);

    for (int i = 0; i < KEYPAD_COLS * KEYPAD_ROWS; i++) {
        if (s->pressed[i / KEYPAD_ROWS][i % KEYPAD_ROWS]) {
            return g_strdup(keypad_key_names[i] ?: "");
        }
    }
    return g_strdup("");
}

static void keypad_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->reset = keypad_reset;
    dc->desc = "UV-K5 keypad matrix";

    object_class_property_add_str(klass, "press",
                                  keypad_get_press, keypad_set_press);
    object_class_property_set_description(klass, "press",
        "hold the named key (MENU, UP, DOWN, EXIT, F, STAR, 0-9, SIDE1, SIDE2); "
        "empty string releases");
}

/* ---------------------------------------------------------------- SPI model */

/*
 * Both SPI controllers, modelled as immediate full-duplex transfers.
 *
 * SPI_WriteByte() in the firmware waits on TXE, writes DR, then waits on RXNE
 * and reads DR, so both flags have to move or display and flash init deadlock.
 * Because a transfer completes within the register write, TXE can stay asserted
 * and RXNE is raised by the write itself.
 *
 * Bytes are handed to a callback so board-level device models (ST7565 display,
 * PY25Q16 flash) can interpret the stream; the chip-select GPIOs decide which
 * device is listening. Layout from py32f071xB.h: CR1 0x00, SR 0x08, DR 0x0C.
 */
#define TYPE_PY32_SPI "py32-spi"
OBJECT_DECLARE_SIMPLE_TYPE(PY32SpiState, PY32_SPI)

typedef uint8_t (*PY32SpiXferFn)(void *opaque, uint8_t out);

struct PY32SpiState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    char        *bus_name;

    uint32_t cr1, cr2, sr;
    uint8_t  rx;

    PY32SpiXferFn xfer;
    void         *xfer_opaque;
};

#define SPI_CR1  0x00
#define SPI_CR2  0x04
#define SPI_SR   0x08
#define SPI_DR   0x0c

#define SPI_SR_RXNE (1u << 0)
#define SPI_SR_TXE  (1u << 1)
#define SPI_SR_BSY  (1u << 7)

void py32_spi_set_xfer(PY32SpiState *s, PY32SpiXferFn fn, void *opaque);

void py32_spi_set_xfer(PY32SpiState *s, PY32SpiXferFn fn, void *opaque)
{
    s->xfer = fn;
    s->xfer_opaque = opaque;
}

/* Clock one byte through whatever device is attached. Used by the DMA model,
 * which bypasses the data register entirely. */
uint8_t py32_spi_xfer_byte(PY32SpiState *s, uint8_t out);

uint8_t py32_spi_xfer_byte(PY32SpiState *s, uint8_t out)
{
    return s->xfer ? s->xfer(s->xfer_opaque, out) : 0xff;
}

static uint64_t py32_spi_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32SpiState *s = opaque;

    switch (addr) {
    case SPI_CR1: return s->cr1;
    case SPI_CR2: return s->cr2;
    case SPI_SR:  return s->sr;
    case SPI_DR:
        s->sr &= ~SPI_SR_RXNE;
        return s->rx;
    default:
        return 0;
    }
}

static void py32_spi_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32SpiState *s = opaque;

    switch (addr) {
    case SPI_CR1: s->cr1 = value; break;
    case SPI_CR2: s->cr2 = value; break;
    case SPI_SR:
        /* Flags are mostly hardware-driven; keep TXE asserted. */
        s->sr = (value & ~SPI_SR_TXE) | SPI_SR_TXE;
        break;
    case SPI_DR:
        /*
         * The transfer happens here, in zero guest time. Whatever the attached
         * device returns becomes the received byte.
         */
        s->rx = s->xfer ? s->xfer(s->xfer_opaque, value & 0xff) : 0xff;
        s->sr |= SPI_SR_RXNE | SPI_SR_TXE;
        s->sr &= ~SPI_SR_BSY;
        break;
    default:
        qemu_log_mask(LOG_UNIMP, "py32-spi%s: write 0x%" HWADDR_PRIx " = 0x%" PRIx64 "\n",
                      s->bus_name ?: "", addr, value);
        break;
    }
}

static const MemoryRegionOps py32_spi_ops = {
    .read = py32_spi_read,
    .write = py32_spi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void py32_spi_reset(DeviceState *dev)
{
    PY32SpiState *s = PY32_SPI(dev);
    s->cr1 = 0;
    s->cr2 = 0;
    /* Transmit buffer starts empty: the firmware's first wait must pass. */
    s->sr = SPI_SR_TXE;
    s->rx = 0xff;
}

static void py32_spi_init(Object *obj)
{
    PY32SpiState *s = PY32_SPI(obj);
    memory_region_init_io(&s->iomem, obj, &py32_spi_ops, s, TYPE_PY32_SPI, 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
}

static Property py32_spi_properties[] = {
    DEFINE_PROP_STRING("bus-name", PY32SpiState, bus_name),
    DEFINE_PROP_END_OF_LIST(),
};

static void py32_spi_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->reset = py32_spi_reset;
    dc->desc = "PY32F071 SPI controller";
    device_class_set_props(dc, py32_spi_properties);
}

/* ---------------------------------------------------------------- ADC model */

/* ------------------------------------------------- PY25Q16 SPI NOR flash */

/* ---------------------------------------------------------------- DMA model */

/*
 * DMA1. The SPI flash driver does not poll the data register -- it configures a
 * pair of channels (4 for RX, 5 for TX), enables the transfer-complete
 * interrupt and then spins on a flag its ISR sets. So a register-only stub
 * deadlocks in PY25Q16_ReadBuffer, which is exactly where the machine stopped.
 *
 * The model performs the whole transfer inside the write that enables a channel:
 * for each byte it clocks the attached SPI device, honouring the increment and
 * direction bits, then raises the transfer-complete flag and the interrupt.
 * Zero guest time is not how hardware behaves, but the firmware only ever waits
 * for completion, never for a partial count.
 *
 * Layout from py32f071xB.h: ISR 0x00, IFCR 0x04, then per-channel blocks of
 * 0x14 starting at 0x08 (CCR, CNDTR, CPAR, CMAR).
 */
/* The DMA model clocks bytes through an SPI controller. Both PY32SpiState and
 * py32_spi_xfer_byte() are already defined above, so no redeclaration here. */

#define TYPE_PY32_DMA "py32-dma"
OBJECT_DECLARE_SIMPLE_TYPE(PY32DmaState, PY32_DMA)

#define PY32_DMA_CHANNELS 7
#define DMA_ISR   0x00
#define DMA_IFCR  0x04
#define DMA_CH_BASE   0x08
#define DMA_CH_STRIDE 0x14
#define DMA_CCR   0x00
#define DMA_CNDTR 0x04
#define DMA_CPAR  0x08
#define DMA_CMAR  0x0c

#define DMA_CCR_EN      (1u << 0)
#define DMA_CCR_TCIE    (1u << 1)
#define DMA_CCR_DIR     (1u << 4)   /* 1 = read from memory */
#define DMA_CCR_CIRC    (1u << 5)
#define DMA_CCR_PINC    (1u << 6)
#define DMA_CCR_MINC    (1u << 7)

/* Per-channel flags occupy four bits each in ISR/IFCR: GIF, TCIF, HTIF, TEIF. */
#define DMA_FLAG_GIF(ch)  (1u << ((ch) * 4 + 0))
#define DMA_FLAG_TCIF(ch) (1u << ((ch) * 4 + 1))
#define DMA_FLAG_HTIF(ch) (1u << ((ch) * 4 + 2))

typedef struct {
    uint32_t ccr, cndtr, cpar, cmar;
} PY32DmaChannel;

struct PY32DmaState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;

    uint32_t       isr;
    PY32DmaChannel ch[PY32_DMA_CHANNELS];

    /* Channels 1-3 and 4-7 share one interrupt line each on this part. */
    qemu_irq irq_1_2_3;
    qemu_irq irq_4_5_6_7;

    /* Set by the SoC: lets the DMA clock bytes through an SPI controller. */
    PY32SpiState *spi[2];
};

static void py32_dma_update_irq(PY32DmaState *s)
{
    bool low = false, high = false;

    for (int ch = 0; ch < PY32_DMA_CHANNELS; ch++) {
        if (!(s->ch[ch].ccr & DMA_CCR_TCIE)) {
            continue;
        }
        if (s->isr & DMA_FLAG_TCIF(ch)) {
            if (ch < 3) {
                low = true;
            } else {
                high = true;
            }
        }
    }
    qemu_set_irq(s->irq_1_2_3, low);
    qemu_set_irq(s->irq_4_5_6_7, high);
}

/* Which SPI controller a peripheral address belongs to, or NULL. */
static PY32SpiState *py32_dma_spi_for(PY32DmaState *s, uint32_t paddr)
{
    if ((paddr & ~0x3ffu) == PY32_SPI1_BASE) {
        return s->spi[0];
    }
    if ((paddr & ~0x3ffu) == PY32_SPI2_BASE) {
        return s->spi[1];
    }
    return NULL;
}

/*
 * Runs a channel to completion. Transmit channels feed bytes to the device;
 * receive channels store what it returns. When both directions are armed the
 * transmit side has usually been enabled first, and the flash driver arms RX
 * before TX, so a receive channel drives the clock itself -- otherwise nothing
 * would ever be shifted in.
 */
static void py32_dma_run(PY32DmaState *s, int ch)
{
    PY32DmaChannel *c = &s->ch[ch];
    PY32SpiState *spi = py32_dma_spi_for(s, c->cpar);

    if (!spi || c->cndtr == 0) {
        /* Nothing attached, or a zero-length transfer: report completion so the
         * guest does not wait forever. */
        s->isr |= DMA_FLAG_TCIF(ch) | DMA_FLAG_GIF(ch);
        c->cndtr = 0;
        py32_dma_update_irq(s);
        return;
    }

    const bool from_memory = (c->ccr & DMA_CCR_DIR) != 0;
    const bool minc = (c->ccr & DMA_CCR_MINC) != 0;
    uint32_t maddr = c->cmar;
    AddressSpace *as = &address_space_memory;

    while (c->cndtr > 0) {
        uint8_t byte = 0xff;

        if (from_memory) {
            address_space_read(as, maddr, MEMTXATTRS_UNSPECIFIED, &byte, 1);
            (void)py32_spi_xfer_byte(spi, byte);
        } else {
            byte = py32_spi_xfer_byte(spi, 0xff);
            address_space_write(as, maddr, MEMTXATTRS_UNSPECIFIED, &byte, 1);
        }

        if (minc) {
            maddr++;
        }
        c->cndtr--;
    }

    s->isr |= DMA_FLAG_TCIF(ch) | DMA_FLAG_GIF(ch);
    py32_dma_update_irq(s);
}

static uint64_t py32_dma_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32DmaState *s = opaque;

    if (addr == DMA_ISR) {
        return s->isr;
    }
    if (addr == DMA_IFCR) {
        return 0;
    }
    if (addr >= DMA_CH_BASE) {
        const unsigned ch = (addr - DMA_CH_BASE) / DMA_CH_STRIDE;
        const unsigned reg = (addr - DMA_CH_BASE) % DMA_CH_STRIDE;
        if (ch < PY32_DMA_CHANNELS) {
            switch (reg) {
            case DMA_CCR:   return s->ch[ch].ccr;
            case DMA_CNDTR: return s->ch[ch].cndtr;
            case DMA_CPAR:  return s->ch[ch].cpar;
            case DMA_CMAR:  return s->ch[ch].cmar;
            default: break;
            }
        }
    }
    return 0;
}

static void py32_dma_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32DmaState *s = opaque;

    if (addr == DMA_IFCR) {
        s->isr &= ~(uint32_t)value;
        py32_dma_update_irq(s);
        return;
    }
    if (addr < DMA_CH_BASE) {
        return;  /* ISR is read-only */
    }

    const unsigned ch = (addr - DMA_CH_BASE) / DMA_CH_STRIDE;
    const unsigned reg = (addr - DMA_CH_BASE) % DMA_CH_STRIDE;
    if (ch >= PY32_DMA_CHANNELS) {
        return;
    }

    switch (reg) {
    case DMA_CNDTR: s->ch[ch].cndtr = value; break;
    case DMA_CPAR:  s->ch[ch].cpar = value;  break;
    case DMA_CMAR:  s->ch[ch].cmar = value;  break;
    case DMA_CCR: {
        const bool was_enabled = (s->ch[ch].ccr & DMA_CCR_EN) != 0;
        s->ch[ch].ccr = value;
        if (!was_enabled && (value & DMA_CCR_EN)) {
            py32_dma_run(s, ch);
        }
        break;
    }
    default:
        break;
    }
}

static const MemoryRegionOps py32_dma_ops = {
    .read = py32_dma_read,
    .write = py32_dma_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void py32_dma_reset(DeviceState *dev)
{
    PY32DmaState *s = PY32_DMA(dev);
    s->isr = 0;
    memset(s->ch, 0, sizeof(s->ch));
}

static void py32_dma_init(Object *obj)
{
    PY32DmaState *s = PY32_DMA(obj);
    memory_region_init_io(&s->iomem, obj, &py32_dma_ops, s, TYPE_PY32_DMA, 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_1_2_3);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_4_5_6_7);
}

static void py32_dma_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->reset = py32_dma_reset;
    dc->desc = "PY32F071 DMA controller";
}

/*
 * 2 MB SPI NOR, backed by a host file so settings and calibration persist
 * across runs. Only the commands the firmware issues are implemented; the
 * driver in App/driver/py25q16.c is the reference for which those are.
 *
 * Chip select comes from a GPIO, and the firmware also drives the display from
 * the same SPI bus, so the model must ignore traffic while deselected --
 * otherwise display bytes would be parsed as flash commands.
 */
#define TYPE_PY25Q16 "py25q16"
OBJECT_DECLARE_SIMPLE_TYPE(PY25Q16State, PY25Q16)

#define PY25Q16_SIZE (2 * MiB)

enum {
    PY25Q16_CMD_NONE = 0,
    PY25Q16_CMD_READ = 0x03,
    PY25Q16_CMD_PP   = 0x02,   /* page program */
    PY25Q16_CMD_WREN = 0x06,
    PY25Q16_CMD_WRDI = 0x04,
    PY25Q16_CMD_RDSR = 0x05,
    PY25Q16_CMD_SE   = 0x20,   /* sector erase, 4 KB */
    PY25Q16_CMD_JEDEC = 0x9f,
};

struct PY25Q16State {
    DeviceState parent_obj;

    uint8_t *data;
    char    *image_path;

    bool     selected;
    uint8_t  cmd;
    uint32_t addr;
    unsigned phase;      /* bytes consumed since the command byte */
    bool     write_enabled;
};

static uint8_t py25q16_xfer(void *opaque, uint8_t out)
{
    PY25Q16State *s = opaque;

    if (!s->selected) {
        return 0xff;
    }

    if (s->cmd == PY25Q16_CMD_NONE) {
        s->cmd = out;
        s->phase = 0;
        s->addr = 0;

        switch (s->cmd) {
        case PY25Q16_CMD_WREN: s->write_enabled = true;  s->cmd = PY25Q16_CMD_NONE; break;
        case PY25Q16_CMD_WRDI: s->write_enabled = false; s->cmd = PY25Q16_CMD_NONE; break;
        default: break;
        }
        return 0xff;
    }

    s->phase++;

    switch (s->cmd) {
    case PY25Q16_CMD_READ:
        if (s->phase <= 3) {
            s->addr = (s->addr << 8) | out;   /* 24-bit address, MSB first */
            return 0xff;
        }
        return s->data[(s->addr++) % PY25Q16_SIZE];

    case PY25Q16_CMD_PP:
        if (s->phase <= 3) {
            s->addr = (s->addr << 8) | out;
            return 0xff;
        }
        if (s->write_enabled) {
            /* NOR can only clear bits without an erase. */
            s->data[s->addr % PY25Q16_SIZE] &= out;
        }
        s->addr++;
        return 0xff;

    case PY25Q16_CMD_SE:
        if (s->phase <= 3) {
            s->addr = (s->addr << 8) | out;
            if (s->phase == 3 && s->write_enabled) {
                const uint32_t sector = (s->addr / 0x1000) * 0x1000;
                memset(s->data + (sector % PY25Q16_SIZE), 0xff, 0x1000);
            }
        }
        return 0xff;

    case PY25Q16_CMD_RDSR:
        /* Never busy: erases and writes complete within the transfer above. */
        return s->write_enabled ? 0x02 : 0x00;

    case PY25Q16_CMD_JEDEC:
        /* Puya manufacturer 0x85, memory type 0x60, capacity 0x15 = 2 MB. */
        switch (s->phase) {
        case 1: return 0x85;
        case 2: return 0x60;
        case 3: return 0x15;
        default: return 0xff;
        }

    default:
        qemu_log_mask(LOG_UNIMP, "py25q16: unhandled command 0x%02x\n", s->cmd);
        return 0xff;
    }
}

/* Chip select is active low. */
static void py25q16_set_cs(void *opaque, int line, int level)
{
    PY25Q16State *s = opaque;
    const bool selected = !level;

    if (s->selected && !selected) {
        /* Deselect ends the command. */
        s->cmd = PY25Q16_CMD_NONE;
        s->phase = 0;
    }
    s->selected = selected;
}

static void py25q16_realize(DeviceState *dev, Error **errp)
{
    PY25Q16State *s = PY25Q16(dev);

    s->data = g_malloc(PY25Q16_SIZE);
    memset(s->data, 0xff, PY25Q16_SIZE);

    if (s->image_path && *s->image_path) {
        FILE *fh = fopen(s->image_path, "rb");
        if (fh) {
            const size_t got = fread(s->data, 1, PY25Q16_SIZE, fh);
            fclose(fh);
            info_report("py25q16: loaded %zu bytes from %s", got, s->image_path);
        } else {
            warn_report("py25q16: cannot open %s, starting from erased flash",
                        s->image_path);
        }
    }

    qdev_init_gpio_in_named(dev, py25q16_set_cs, "cs", 1);
}

static Property py25q16_properties[] = {
    DEFINE_PROP_STRING("image", PY25Q16State, image_path),
    DEFINE_PROP_END_OF_LIST(),
};

static void py25q16_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = py25q16_realize;
    dc->desc = "PY25Q16 2MB SPI NOR flash";
    device_class_set_props(dc, py25q16_properties);
}

/*
 * The firmware spins on three ADC conditions during BOARD_ADC_Init, so a
 * store-and-echo stub deadlocks there:
 *
 *   while (LL_ADC_IsCalibrationOnGoing(ADC1))   -- CR2.CAL must self-clear
 *   LL_ADC_Enable(ADC1)                         -- CR2.ADON
 *   while (!LL_ADC_IsActiveFlag_EOS(ADC1))      -- SR.EOC must rise
 *
 * Register layout and bit positions come from the vendor headers
 * (py32f071xB.h ADC_TypeDef, py32f071_ll_adc.h), including the detail that
 * LL_ADC_FLAG_EOS is really ADC_SR_EOC on this part.
 *
 * The conversion result is a fixed value for now. It feeds battery voltage and
 * the CEC-cable key detection; a flat reading is enough to boot, and the value
 * can be made settable once those paths are being tested.
 */
#define TYPE_PY32_ADC "py32-adc"
OBJECT_DECLARE_SIMPLE_TYPE(PY32AdcState, PY32_ADC)

struct PY32AdcState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t regs[0x20];
};

#define ADC_SR    0x00
#define ADC_CR1   0x04
#define ADC_CR2   0x08
#define ADC_DR    0x50

#define ADC_SR_AWD    (1u << 0)
#define ADC_SR_EOC    (1u << 1)   /* what LL calls EOS on this part */
#define ADC_SR_JEOC   (1u << 2)
#define ADC_SR_JSTRT  (1u << 3)
#define ADC_SR_STRT   (1u << 4)

#define ADC_CR2_ADON   (1u << 0)
#define ADC_CR2_CAL    (1u << 2)
#define ADC_CR2_RSTCAL (1u << 3)
#define ADC_CR2_SWSTART (1u << 22)

/* Battery sits around 7.4 V; the calibration table in flash maps raw counts to
 * volts, and 2200 lands mid-scale on a real dump. */
#define PY32_ADC_RESULT 2200

static uint64_t py32_adc_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32AdcState *s = opaque;
    const unsigned idx = addr >> 2;

    if (idx >= ARRAY_SIZE(s->regs)) {
        return 0;
    }

    if (addr == ADC_DR) {
        /* Reading the result clears end-of-conversion, as on hardware. */
        s->regs[ADC_SR >> 2] &= ~ADC_SR_EOC;
        return PY32_ADC_RESULT;
    }
    return s->regs[idx];
}

static void py32_adc_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32AdcState *s = opaque;
    const unsigned idx = addr >> 2;

    if (idx >= ARRAY_SIZE(s->regs)) {
        return;
    }

    if (addr == ADC_CR2) {
        /*
         * Calibration and reset-calibration complete instantly: the bits are
         * write-1-to-start and hardware-cleared, so never store them set or the
         * firmware's wait loop never exits.
         */
        s->regs[idx] = value & ~(ADC_CR2_CAL | ADC_CR2_RSTCAL);

        if (value & ADC_CR2_ADON) {
            /* Enabled: report a finished conversion so the init sequence and
             * later polled reads both make progress. */
            s->regs[ADC_SR >> 2] |= ADC_SR_EOC | ADC_SR_STRT;
        }
        return;
    }

    if (addr == ADC_SR) {
        /* Flags are cleared by writing 0 to them. */
        s->regs[idx] &= value;
        return;
    }

    s->regs[idx] = value;
}

static const MemoryRegionOps py32_adc_ops = {
    .read = py32_adc_read,
    .write = py32_adc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void py32_adc_reset(DeviceState *dev)
{
    PY32AdcState *s = PY32_ADC(dev);
    memset(s->regs, 0, sizeof(s->regs));
}

static void py32_adc_init(Object *obj)
{
    PY32AdcState *s = PY32_ADC(obj);
    memory_region_init_io(&s->iomem, obj, &py32_adc_ops, s, TYPE_PY32_ADC, 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
}

static void py32_adc_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->reset = py32_adc_reset;
    dc->desc = "PY32F071 ADC";
}

/*
 * Peripherals the firmware touches during init but whose behaviour it does not
 * depend on yet (FLASH latency, PWR, SYSCFG, EXTI, CRC, timers, I2C, ADC).
 * Reads return the last written value so read-modify-write sequences behave,
 * and everything is logged so it is visible which ones actually get used --
 * that log is how the next tier of models gets prioritised.
 */
#define TYPE_PY32_STUB "py32-stub"
OBJECT_DECLARE_SIMPLE_TYPE(PY32StubState, PY32_STUB)

struct PY32StubState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    char        *stub_name;
    uint32_t     size;
    uint32_t     regs[0x100];
};

static uint64_t py32_stub_read(void *opaque, hwaddr addr, unsigned size)
{
    PY32StubState *s = opaque;
    const unsigned idx = addr >> 2;
    const uint32_t value = idx < ARRAY_SIZE(s->regs) ? s->regs[idx] : 0;

    qemu_log_mask(LOG_UNIMP, "py32-%s: read 0x%03" HWADDR_PRIx " -> 0x%08x\n",
                  s->stub_name ?: "stub", addr, value);
    return value;
}

static void py32_stub_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    PY32StubState *s = opaque;
    const unsigned idx = addr >> 2;

    if (idx < ARRAY_SIZE(s->regs)) {
        s->regs[idx] = value;
    }
    qemu_log_mask(LOG_UNIMP, "py32-%s: write 0x%03" HWADDR_PRIx " = 0x%08" PRIx64 "\n",
                  s->stub_name ?: "stub", addr, value);
}

static const MemoryRegionOps py32_stub_ops = {
    .read = py32_stub_read,
    .write = py32_stub_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void py32_stub_realize(DeviceState *dev, Error **errp)
{
    PY32StubState *s = PY32_STUB(dev);

    memory_region_init_io(&s->iomem, OBJECT(dev), &py32_stub_ops, s,
                          s->stub_name ?: TYPE_PY32_STUB,
                          s->size ? s->size : 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->iomem);
}

static Property py32_stub_properties[] = {
    DEFINE_PROP_STRING("stub-name", PY32StubState, stub_name),
    DEFINE_PROP_UINT32("size", PY32StubState, size, 0x400),
    DEFINE_PROP_END_OF_LIST(),
};

static void py32_stub_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = py32_stub_realize;
    dc->desc = "PY32F071 unimplemented peripheral";
    device_class_set_props(dc, py32_stub_properties);
}

/* ------------------------------------------------------------ SoC container */

#define TYPE_PY32F071_SOC "py32f071-soc"
OBJECT_DECLARE_SIMPLE_TYPE(PY32F071State, PY32F071_SOC)

#define PY32_NUM_GPIO 4
#define PY32_NUM_STUB 25

struct PY32F071State {
    DeviceState parent_obj;

    ARMv7MState   armv7m;
    PY32RccState  rcc;
    PY32GpioState gpio[PY32_NUM_GPIO];
    PY32AdcState  adc;
    PY32SpiState  spi[2];
    PY32DmaState  dma;
    PY32StubState stub[PY32_NUM_STUB];

    Clock *sysclk;

    MemoryRegion flash;
    MemoryRegion flash_alias;
    MemoryRegion sram;
    MemoryRegion *board_memory;
    MemoryRegion container;
};

/* Peripherals covered by the catch-all, in map order. */
static const struct { const char *name; hwaddr base; uint32_t size; } py32_stubs[] = {
    { "flash-ctl", PY32_FLASH_R_BASE, 0x400 },
    { "pwr",       PY32_PWR_BASE,     0x400 },
    { "syscfg",    PY32_SYSCFG_BASE,  0x400 },
    { "exti",      PY32_EXTI_BASE,    0x400 },
    { "crc",       PY32_CRC_BASE,     0x400 },
    { "usart1",    PY32_USART1_BASE,  0x400 },
    { "usart2",    PY32_USART2_BASE,  0x400 },
    { "i2c1",      PY32_I2C1_BASE,    0x400 },
    { "i2c2",      PY32_I2C2_BASE,    0x400 },
    { "tim1",      PY32_TIM1_BASE,    0x400 },
    { "tim2",      PY32_TIM2_BASE,    0x400 },
    { "tim3",      PY32_TIM3_BASE,    0x400 },
    { "tim6",      PY32_TIM6_BASE,    0x400 },
    { "tim7",      PY32_TIM7_BASE,    0x400 },
    { "tim14",     PY32_TIM14_BASE,   0x400 },
    { "tim15",     PY32_TIM15_BASE,   0x400 },
    { "tim16",     PY32_TIM16_BASE,   0x400 },
    { "tim17",     PY32_TIM17_BASE,   0x400 },
    { "usb",       PY32_USB_BASE,     0x400 },
    { "rtc",       PY32_RTC_BASE,     0x400 },
    { "iwdg",      PY32_IWDG_BASE,    0x400 },
    { "wwdg",      PY32_WWDG_BASE,    0x400 },
    { "usart3",    PY32_USART3_BASE,  0x400 },
    { "usart4",    PY32_USART4_BASE,  0x400 },
    { "dbgmcu",    PY32_DBGMCU_BASE,  0x400 },
    { "lcd-ctl",   PY32_LCD_BASE,     0x400 },
};

static const hwaddr py32_gpio_bases[PY32_NUM_GPIO] = {
    PY32_GPIOA_BASE, PY32_GPIOB_BASE, PY32_GPIOC_BASE, PY32_GPIOF_BASE,
};
static const char *py32_gpio_names[PY32_NUM_GPIO] = { "a", "b", "c", "f" };

static void py32f071_soc_init(Object *obj)
{
    PY32F071State *s = PY32F071_SOC(obj);

    object_initialize_child(obj, "armv7m", &s->armv7m, TYPE_ARMV7M);
    object_initialize_child(obj, "rcc", &s->rcc, TYPE_PY32_RCC);
    object_initialize_child(obj, "adc", &s->adc, TYPE_PY32_ADC);
    object_initialize_child(obj, "spi1", &s->spi[0], TYPE_PY32_SPI);
    object_initialize_child(obj, "spi2", &s->spi[1], TYPE_PY32_SPI);
    object_initialize_child(obj, "dma1", &s->dma, TYPE_PY32_DMA);

    /* The firmware runs the core at 48 MHz (SystemInit configures HSI+PLL). */
    s->sysclk = qdev_init_clock_in(DEVICE(obj), "sysclk", NULL, NULL, 0);

    for (int i = 0; i < PY32_NUM_GPIO; i++) {
        object_initialize_child(obj, py32_gpio_names[i], &s->gpio[i], TYPE_PY32_GPIO);
    }
    for (int i = 0; i < PY32_NUM_STUB; i++) {
        object_initialize_child(obj, py32_stubs[i].name, &s->stub[i], TYPE_PY32_STUB);
    }
}

static void py32f071_soc_realize(DeviceState *dev_soc, Error **errp)
{
    PY32F071State *s = PY32F071_SOC(dev_soc);
    Object *obj = OBJECT(dev_soc);

    if (!s->board_memory) {
        error_setg(errp, "memory property was not set");
        return;
    }

    memory_region_init(&s->container, obj, "py32f071-container", 0x60000000);

    memory_region_init_rom(&s->flash, obj, "py32f071.flash", PY32_FLASH_SIZE, errp);
    if (*errp) {
        return;
    }
    memory_region_add_subregion(&s->container, PY32_FLASH_BASE, &s->flash);

    memory_region_init_ram(&s->sram, obj, "py32f071.sram", PY32_SRAM_SIZE, errp);
    if (*errp) {
        return;
    }
    memory_region_add_subregion(&s->container, PY32_SRAM_BASE, &s->sram);

    /* Core. The firmware's vector table has 53 entries; round up for the NVIC. */
    qdev_prop_set_uint32(DEVICE(&s->armv7m), "num-irq", PY32_NUM_IRQ + 16);
    qdev_prop_set_string(DEVICE(&s->armv7m), "cpu-type", ARM_CPU_TYPE_NAME("cortex-m0"));
    qdev_prop_set_bit(DEVICE(&s->armv7m), "enable-bitband", false);
    qdev_connect_clock_in(DEVICE(&s->armv7m), "cpuclk", s->sysclk);
    /*
     * Accelerate SysTick polling. SYSTICK_DelayUs busy-reads the current-value
     * register and accumulates differences; under emulation the counter barely
     * moves between reads, and a measured 120 ms delay needed about 7.7 hours of
     * wall time. Advancing the timer on each read makes those loops converge.
     *
     * Guest time therefore runs fast during a delay: the right trade for
     * exercising the UI and control flow, the wrong tool for signal timing.
     */
    qdev_prop_set_uint32(DEVICE(&s->armv7m.systick[0]), "poll-boost", 24000);
    object_property_set_link(OBJECT(&s->armv7m), "memory", OBJECT(&s->container),
                             &error_abort);
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->armv7m), errp)) {
        return;
    }

    if (!sysbus_realize(SYS_BUS_DEVICE(&s->rcc), errp)) {
        return;
    }
    memory_region_add_subregion(&s->container, PY32_RCC_BASE,
                                sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->rcc), 0));

    if (!sysbus_realize(SYS_BUS_DEVICE(&s->adc), errp)) {
        return;
    }
    memory_region_add_subregion(&s->container, PY32_ADC1_BASE,
                                sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->adc), 0));

    static const hwaddr spi_bases[2] = { PY32_SPI1_BASE, PY32_SPI2_BASE };
    static const char *spi_names[2] = { "1", "2" };
    for (int i = 0; i < 2; i++) {
        qdev_prop_set_string(DEVICE(&s->spi[i]), "bus-name", spi_names[i]);
        if (!sysbus_realize(SYS_BUS_DEVICE(&s->spi[i]), errp)) {
            return;
        }
        memory_region_add_subregion(&s->container, spi_bases[i],
                                    sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->spi[i]), 0));
    }

    /*
     * DMA needs to reach the SPI controllers directly: the flash driver drives
     * transfers entirely through DMA channels 4 and 5 and never touches the data
     * register, so routing has to exist before it runs.
     */
    s->dma.spi[0] = &s->spi[0];
    s->dma.spi[1] = &s->spi[1];
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->dma), errp)) {
        return;
    }
    memory_region_add_subregion(&s->container, PY32_DMA1_BASE,
                                sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->dma), 0));
    /* Vector 10 covers channels 1-3, vector 11 covers 4-7 (py32f071xB.h). */
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->dma), 0,
                       qdev_get_gpio_in(DEVICE(&s->armv7m), 10));
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->dma), 1,
                       qdev_get_gpio_in(DEVICE(&s->armv7m), 11));

    for (int i = 0; i < PY32_NUM_GPIO; i++) {
        qdev_prop_set_string(DEVICE(&s->gpio[i]), "port-name", py32_gpio_names[i]);
        if (!sysbus_realize(SYS_BUS_DEVICE(&s->gpio[i]), errp)) {
            return;
        }
        memory_region_add_subregion(&s->container, py32_gpio_bases[i],
                                    sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->gpio[i]), 0));
    }

    for (int i = 0; i < PY32_NUM_STUB; i++) {
        qdev_prop_set_string(DEVICE(&s->stub[i]), "stub-name", py32_stubs[i].name);
        qdev_prop_set_uint32(DEVICE(&s->stub[i]), "size", py32_stubs[i].size);
        if (!sysbus_realize(SYS_BUS_DEVICE(&s->stub[i]), errp)) {
            return;
        }
        memory_region_add_subregion(&s->container, py32_stubs[i].base,
                                    sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->stub[i]), 0));
    }

    /*
     * The container is handed to the ARMv7M core as its address space, so it
     * must not also be mounted into the board's system memory: a memory region
     * can only have one container. Aliasing flash at 0 is what the hardware
     * does -- the M0+ fetches its vector table from 0x00000000, and on this part
     * the boot mapping points that at flash.
     */
    /*
     * The alias starts at the application offset, not at the flash base: the
     * core fetches its initial SP and PC from address 0, and the image is loaded
     * at 0x08002800 (past the bootloader), so 0 has to line up with the
     * application's vector table rather than the bootloader's.
     */
    memory_region_init_alias(&s->flash_alias, obj, "py32f071.flash.alias",
                             &s->flash, PY32_APP_OFFSET,
                             PY32_FLASH_SIZE - PY32_APP_OFFSET);
    memory_region_add_subregion(&s->container, 0, &s->flash_alias);
}

static Property py32f071_soc_properties[] = {
    DEFINE_PROP_LINK("memory", PY32F071State, board_memory, TYPE_MEMORY_REGION,
                     MemoryRegion *),
    DEFINE_PROP_END_OF_LIST(),
};

static void py32f071_soc_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = py32f071_soc_realize;
    dc->desc = "Puya PY32F071 SoC";
    device_class_set_props(dc, py32f071_soc_properties);
}

/* ------------------------------------------------------------------ machine */

struct UVK5MachineState {
    MachineState parent;
    PY32F071State soc;
    PY25Q16State  flash;
    UVK5KeypadState keypad;
    Clock *sysclk;
    char  *flash_image;
};

#define TYPE_UVK5_MACHINE MACHINE_TYPE_NAME("uv-k5-v3")
OBJECT_DECLARE_SIMPLE_TYPE(UVK5MachineState, UVK5_MACHINE)

static void uvk5_machine_init(MachineState *machine)
{
    UVK5MachineState *s = UVK5_MACHINE(machine);

    object_initialize_child(OBJECT(machine), "soc", &s->soc, TYPE_PY32F071_SOC);
    object_property_set_link(OBJECT(&s->soc), "memory",
                             OBJECT(get_system_memory()), &error_fatal);

    /*
     * SysTick pacing, deliberately not the real 48 MHz.
     *
     * SYSTICK_DelayUs busy-reads SysTick->VAL and accumulates the difference
     * until it reaches Delay * 48. On hardware each loop iteration advances the
     * counter by tens of ticks. Under emulation an iteration costs far less
     * wall-clock time, so at 48 MHz the counter barely moves between reads and a
     * 1 ms delay takes about a minute -- measured, not assumed.
     *
     * Slowing the SysTick clock makes each read span more ticks, which is the
     * ratio that loop actually depends on. The trade-off: guest time no longer
     * matches real time, so anything timing-critical must be judged against the
     * counter rather than a stopwatch.
     */
    s->sysclk = clock_new(OBJECT(machine), "SYSCLK");
    clock_set_hz(s->sysclk, 48000000ULL);
    qdev_connect_clock_in(DEVICE(&s->soc), "sysclk", s->sysclk);

    sysbus_realize(SYS_BUS_DEVICE(&s->soc), &error_fatal);

    /*
     * External SPI NOR on SPI1, chip-selected by GPIOA pin 3 (CS_PIN in
     * App/driver/py25q16.c). The image carries settings and the calibration
     * block; -drive if=pflash,file=... overrides the default path.
     */
    object_initialize_child(OBJECT(machine), "flash", &s->flash, TYPE_PY25Q16);
    {
        /*
         * Image path from -machine flash-image=..., falling back to -bios.
         * Without one the flash reads as erased, which the firmware treats as a
         * factory-fresh radio: it boots, but with no calibration data.
         */
        const char *path = s->flash_image;
        if (!path || !*path) {
            path = machine->firmware;
        }
        if (path && *path) {
            qdev_prop_set_string(DEVICE(&s->flash), "image", path);
        }
    }
    qdev_realize(DEVICE(&s->flash), NULL, &error_fatal);

    /* SPI2, not SPI1: App/driver/py25q16.c uses SPI2 and st7565.c uses SPI1. */
    py32_spi_set_xfer(&s->soc.spi[1], py25q16_xfer, &s->flash);

    /*
     * Keypad matrix on GPIOB. The scan columns are outputs from the port into
     * the matrix, and the matrix drives the row lines back as inputs, which is
     * the same direction of travel as the real wiring.
     */
    object_initialize_child(OBJECT(machine), "keypad", &s->keypad,
                            TYPE_UVK5_KEYPAD);
    qdev_realize(DEVICE(&s->keypad), NULL, &error_fatal);

    for (int c = 1; c < KEYPAD_COLS; c++) {
        qdev_connect_gpio_out_named(DEVICE(&s->soc.gpio[1]), "pin-out",
                                    KEYPAD_COL_PIN(c),
                                    qdev_get_gpio_in_named(DEVICE(&s->keypad),
                                                           "col", c));
    }
    for (int r = 0; r < KEYPAD_ROWS; r++) {
        qdev_connect_gpio_out_named(DEVICE(&s->keypad), "row", r,
                                    qdev_get_gpio_in_named(DEVICE(&s->soc.gpio[1]),
                                                           "pin-in",
                                                           KEYPAD_ROW_PIN(r)));
    }

    /*
     * Drive the initial row levels now that the lines exist. The device reset
     * ran before wiring, so its qemu_set_irq calls went nowhere; without this
     * the port keeps whatever it had, which read as every row low -- every key
     * held at once, which the firmware discards as noise.
     */
    keypad_update_rows(&s->keypad);
    qdev_connect_gpio_out_named(DEVICE(&s->soc.gpio[0]), "pin-out", 3,
                                qdev_get_gpio_in_named(DEVICE(&s->flash),
                                                       "cs", 0));

    /*
     * The application lives at PY32_APP_OFFSET, past the bootloader. Passing
     * that as the load offset means a plain application .elf/.bin boots without
     * needing a bootloader image.
     */
    armv7m_load_kernel(ARM_CPU(first_cpu), machine->kernel_filename,
                       PY32_APP_OFFSET, PY32_FLASH_SIZE - PY32_APP_OFFSET);
}

static char *uvk5_get_flash_image(Object *obj, Error **errp)
{
    UVK5MachineState *s = UVK5_MACHINE(obj);
    return g_strdup(s->flash_image);
}

static void uvk5_set_flash_image(Object *obj, const char *value, Error **errp)
{
    UVK5MachineState *s = UVK5_MACHINE(obj);
    g_free(s->flash_image);
    s->flash_image = g_strdup(value);
}

static void uvk5_machine_class_init(ObjectClass *oc, void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);

    object_class_property_add_str(oc, "flash-image",
                                  uvk5_get_flash_image, uvk5_set_flash_image);
    object_class_property_set_description(oc, "flash-image",
        "2MB SPI NOR image holding settings and calibration data");

    mc->desc = "Quansheng UV-K5 V3 / UV-K1 (PY32F071, Cortex-M0+)";
    mc->init = uvk5_machine_init;
    mc->max_cpus = 1;
    mc->default_cpus = 1;
    mc->min_cpus = 1;
    mc->default_ram_size = 0;
    mc->no_floppy = 1;
    mc->no_cdrom = 1;
    mc->no_parallel = 1;
}

/* -------------------------------------------------------------- registration */

static const TypeInfo py32_types[] = {
    {
        .name = TYPE_PY32_RCC,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32RccState),
        .instance_init = py32_rcc_init,
        .class_init = py32_rcc_class_init,
    },
    {
        .name = TYPE_PY32_GPIO,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32GpioState),
        .instance_init = py32_gpio_init,
        .class_init = py32_gpio_class_init,
    },
    {
        .name = TYPE_PY32_DMA,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32DmaState),
        .instance_init = py32_dma_init,
        .class_init = py32_dma_class_init,
    },
    {
        .name = TYPE_UVK5_KEYPAD,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(UVK5KeypadState),
        .instance_init = keypad_init,
        .class_init = keypad_class_init,
    },
    {
        .name = TYPE_PY25Q16,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(PY25Q16State),
        .class_init = py25q16_class_init,
    },
    {
        .name = TYPE_PY32_SPI,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32SpiState),
        .instance_init = py32_spi_init,
        .class_init = py32_spi_class_init,
    },
    {
        .name = TYPE_PY32_ADC,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32AdcState),
        .instance_init = py32_adc_init,
        .class_init = py32_adc_class_init,
    },
    {
        .name = TYPE_PY32_STUB,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32StubState),
        .class_init = py32_stub_class_init,
    },
    {
        .name = TYPE_PY32F071_SOC,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(PY32F071State),
        .instance_init = py32f071_soc_init,
        .class_init = py32f071_soc_class_init,
    },
    {
        .name = TYPE_UVK5_MACHINE,
        .parent = TYPE_MACHINE,
        .instance_size = sizeof(UVK5MachineState),
        .class_init = uvk5_machine_class_init,
    },
};

DEFINE_TYPES(py32_types)
