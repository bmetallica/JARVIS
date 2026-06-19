// WS2812-Statusanzeige (7 LEDs). Weitgehend vollständig (led_strip-Komponente).
#include "hardware.h"
#include "led_strip.h"

static led_strip_handle_t s_strip;

void leds_init(void) {
    led_strip_config_t sc = { .strip_gpio_num = JARVIS_LED_GPIO, .max_leds = JARVIS_LED_COUNT };
    led_strip_rmt_config_t rc = { .resolution_hz = 10 * 1000 * 1000 };
    led_strip_new_rmt_device(&sc, &rc, &s_strip);
    led_strip_clear(s_strip);
}

static void fill(uint8_t r, uint8_t g, uint8_t b) {
    if (!s_strip) return;
    for (int i = 0; i < JARVIS_LED_COUNT; i++) led_strip_set_pixel(s_strip, i, r, g, b);
    led_strip_refresh(s_strip);
}

void leds_set_state(jarvis_state_t st) {
    switch (st) {
        case ST_SETUP:     fill(0, 0, 60);  break;   // blau: WLAN-Setup
        case ST_IDLE:      fill(2, 2, 5);   break;   // sehr schwach
        case ST_LISTENING: fill(0, 50, 0);  break;   // grün
        case ST_THINKING:  fill(35, 25, 0); break;   // gelb
        case ST_SPEAKING:  fill(50, 18, 0); break;   // orange
        case ST_ERROR:     fill(60, 0, 0);  break;   // rot
    }
}

void leds_show_volume(int percent) {
    if (!s_strip) return;
    int lit = (percent * JARVIS_LED_COUNT + 50) / 100;
    for (int i = 0; i < JARVIS_LED_COUNT; i++)
        led_strip_set_pixel(s_strip, i, 0, (i < lit) ? 30 : 0, 0);
    led_strip_refresh(s_strip);
}
