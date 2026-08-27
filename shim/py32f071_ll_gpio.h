/* Minimal stand-in for the PY32 GPIO LL header.
 *
 * driver/gpio.h names GPIO port pointers and LL_GPIO_PIN_* constants at file
 * scope, so those have to exist for the firmware headers to parse. Nothing here
 * touches real hardware: the ports are dummy objects and the pin masks only need
 * to be distinct, because the simulator resolves key state through the scripted
 * paddle timeline instead of reading pins.
 */

#ifndef PY32F071_LL_GPIO_H_SHIM
#define PY32F071_LL_GPIO_H_SHIM

#include <stdint.h>

typedef struct {
    volatile uint32_t MODER;
    volatile uint32_t OTYPER;
    volatile uint32_t OSPEEDR;
    volatile uint32_t PUPDR;
    volatile uint32_t IDR;
    volatile uint32_t ODR;
    volatile uint32_t BSRR;
    volatile uint32_t LCKR;
    volatile uint32_t AFR[2];
    volatile uint32_t BRR;
} GPIO_TypeDef;

// The firmware treats these as numeric addresses: driver/gpio.h packs a port
// into the high half of a pin id (GPIO_MAKE_PIN) inside an enum, so they must be
// integer constant expressions, and GPIO_PORT() casts them back to a pointer.
// Keep that shape -- the accessors below resolve the fake address to real
// storage instead of dereferencing it, so no host memory at 0x0000 is touched.
#define IOPORT_BASE 0u

#define GPIOA 0x0000u
#define GPIOB 0x0100u
#define GPIOC 0x0200u
#define GPIOF 0x0300u

// Resolves a fake port address to backing storage. Defined in
// stubs/firmware_globals.c.
GPIO_TypeDef *SIM_GpioPort(void *fake_address);

#define LL_GPIO_PIN_0  (1u << 0)
#define LL_GPIO_PIN_1  (1u << 1)
#define LL_GPIO_PIN_2  (1u << 2)
#define LL_GPIO_PIN_3  (1u << 3)
#define LL_GPIO_PIN_4  (1u << 4)
#define LL_GPIO_PIN_5  (1u << 5)
#define LL_GPIO_PIN_6  (1u << 6)
#define LL_GPIO_PIN_7  (1u << 7)
#define LL_GPIO_PIN_8  (1u << 8)
#define LL_GPIO_PIN_9  (1u << 9)
#define LL_GPIO_PIN_10 (1u << 10)
#define LL_GPIO_PIN_11 (1u << 11)
#define LL_GPIO_PIN_12 (1u << 12)
#define LL_GPIO_PIN_13 (1u << 13)
#define LL_GPIO_PIN_14 (1u << 14)
#define LL_GPIO_PIN_15 (1u << 15)

#define LL_GPIO_MODE_INPUT     0u
#define LL_GPIO_MODE_OUTPUT    1u
#define LL_GPIO_MODE_ALTERNATE 2u
#define LL_GPIO_MODE_ANALOG    3u

#define LL_GPIO_PULL_NO   0u
#define LL_GPIO_PULL_UP   1u
#define LL_GPIO_PULL_DOWN 2u

#define LL_GPIO_OUTPUT_PUSHPULL  0u
#define LL_GPIO_OUTPUT_OPENDRAIN 1u

#define LL_GPIO_SPEED_FREQ_LOW       0u
#define LL_GPIO_SPEED_FREQ_MEDIUM    1u
#define LL_GPIO_SPEED_FREQ_HIGH      2u
#define LL_GPIO_SPEED_FREQ_VERY_HIGH 3u

static inline uint32_t LL_GPIO_IsInputPinSet(GPIO_TypeDef *port, uint32_t pin)
{
    return (SIM_GpioPort(port)->IDR & pin) ? 1u : 0u;
}
static inline void LL_GPIO_SetOutputPin(GPIO_TypeDef *port, uint32_t pin)
{
    SIM_GpioPort(port)->ODR |= pin;
}
static inline void LL_GPIO_ResetOutputPin(GPIO_TypeDef *port, uint32_t pin)
{
    SIM_GpioPort(port)->ODR &= ~pin;
}
static inline uint32_t LL_GPIO_IsOutputPinSet(GPIO_TypeDef *port, uint32_t pin)
{
    return (SIM_GpioPort(port)->ODR & pin) ? 1u : 0u;
}
static inline void LL_GPIO_SetPinMode(GPIO_TypeDef *port, uint32_t pin, uint32_t mode)  { (void)port; (void)pin; (void)mode; }
static inline void LL_GPIO_SetPinPull(GPIO_TypeDef *port, uint32_t pin, uint32_t pull)  { (void)port; (void)pin; (void)pull; }
static inline void LL_GPIO_SetPinOutputType(GPIO_TypeDef *port, uint32_t pin, uint32_t t) { (void)port; (void)pin; (void)t; }
static inline void LL_GPIO_SetPinSpeed(GPIO_TypeDef *port, uint32_t pin, uint32_t s)   { (void)port; (void)pin; (void)s; }

#endif
