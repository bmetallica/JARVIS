// Audio für Waveshare ESP32-S3-AUDIO-Board:
//   I2C (SDA11/SCL10) → ES8311 (0x18, Speaker), ES7210 (0x40, Mic), TCA9555 (0x20, PA-Enable)
//   I2S (MCLK12/BCLK13/LRCLK14/DIN15/DOUT16), 16 kHz 16 bit; Mono ODER Stereo (Dual-Mic).
//   Codec-Init über esp_codec_dev (managed component).
//
// Dual-Mic (JARVIS_DUAL_MIC=1): I2S Stereo → ES7210 liefert 2 Mic-Kanäle (interleaved) an die AFE;
// der Speaker (ES8311, mono) wird beim Schreiben auf L/R dupliziert. Mono (=0): wie gehabt 1 Kanal.
#include <math.h>
#include <string.h>
#include "hardware.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"

static const char *TAG = "audio";
static i2s_chan_handle_t s_tx, s_rx;
static esp_codec_dev_handle_t s_play, s_rec;
static i2c_master_bus_handle_t s_i2c_bus_handle;
static i2c_master_dev_handle_t s_tca9555_handle;
static volatile float s_gain = 1.0f;         // Software-Lautstärke (zuverlässige Dämpfung der Ausgabe)

// ── I2C-Bus + TCA9555 (Verstärker-Enable) ─────────────────────────────────────
static void i2c_bus_init(void) {
    i2c_master_bus_config_t bus_config = {
        .i2c_port = BSP_I2C_PORT,
        .sda_io_num = BSP_I2C_SDA,
        .scl_io_num = BSP_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_config, &s_i2c_bus_handle));

    i2c_device_config_t tca_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = TCA9555_ADDR,
        .scl_speed_hz = 400000,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c_bus_handle, &tca_cfg, &s_tca9555_handle));
}

static void tca9555_write(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = { reg, val };
    ESP_ERROR_CHECK(i2c_master_transmit(s_tca9555_handle, buf, sizeof(buf), -1));
}

static void pa_enable(bool on) {
    tca9555_write(0x07, 0xFE);                 // P1.0 als Ausgang
    tca9555_write(0x03, on ? 0x01 : 0x00);     // P1.0 high = Verstärker an
}

// ── I2S (Standard-Modus, Full-Duplex; Mono oder Stereo je nach Dual-Mic) ───────
static void i2s_init(void) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, &s_rx));
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(JARVIS_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
#if JARVIS_DUAL_MIC
                        I2S_SLOT_MODE_STEREO),
#else
                        I2S_SLOT_MODE_MONO),
#endif
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK, .bclk = BSP_I2S_BCLK, .ws = BSP_I2S_LRCLK,
            .dout = BSP_I2S_DOUT, .din = BSP_I2S_DIN,
            .invert_flags = { 0 },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
}

void audio_init(void) {
    i2c_bus_init();
    pa_enable(true);
    i2s_init();

    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&(audio_codec_i2s_cfg_t){
        .port = I2S_NUM_0, .rx_handle = s_rx, .tx_handle = s_tx,
    });
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    // ES8311 (Speaker/DAC)
    const audio_codec_ctrl_if_t *out_ctrl = audio_codec_new_i2c_ctrl(&(audio_codec_i2c_cfg_t){
        .port = BSP_I2C_PORT, .addr = ES8311_ADDR << 1, .bus_handle = s_i2c_bus_handle,
    });
    es8311_codec_cfg_t es8311 = {
        .ctrl_if = out_ctrl, .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = -1,
        .use_mclk = true, .hw_gain.pa_voltage = 5.0, .hw_gain.codec_dac_voltage = 3.3,
    };
    s_play = esp_codec_dev_new(&(esp_codec_dev_cfg_t){
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311), .data_if = data_if,
    });

    // ES7210 (Mic/ADC) — bei Dual-Mic beide Mikrofone, sonst eines
    const audio_codec_ctrl_if_t *in_ctrl = audio_codec_new_i2c_ctrl(&(audio_codec_i2c_cfg_t){
        .port = BSP_I2C_PORT, .addr = ES7210_ADDR << 1, .bus_handle = s_i2c_bus_handle,
    });
    es7210_codec_cfg_t es7210 = {
        .ctrl_if = in_ctrl,
#if JARVIS_DUAL_MIC
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2,
#else
        .mic_selected = ES7210_SEL_MIC1,
#endif
    };
    s_rec = esp_codec_dev_new(&(esp_codec_dev_cfg_t){
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = es7210_codec_new(&es7210), .data_if = data_if,
    });

    esp_codec_dev_sample_info_t fs = {
        .sample_rate = JARVIS_SAMPLE_RATE, .channel = JARVIS_MIC_CHANNELS, .bits_per_sample = 16,
    };
    esp_codec_dev_open(s_play, &fs);
    esp_codec_dev_open(s_rec, &fs);
    esp_codec_dev_set_in_gain(s_rec, JARVIS_MIC_GAIN_DB);
    ESP_LOGI(TAG, "Codecs bereit (ES8311/ES7210, %s, %d Kanal/-äle).",
             JARVIS_DUAL_MIC ? "Dual-Mic" : "Mono", JARVIS_MIC_CHANNELS);
}

#if JARVIS_AUDIO_DIAG
static void diag_levels(const int16_t *buf, int samples) {
    static int64_t last = 0;
    int64_t now = esp_timer_get_time() / 1000;
    if (now - last < 2000) return;             // höchstens alle 2 s
    last = now;
    long long sq = 0; int peak = 0, clip = 0;
    for (int i = 0; i < samples; i++) {
        int a = buf[i] < 0 ? -buf[i] : buf[i];
        if (a > peak) peak = a;
        if (a > 32000) clip++;
        sq += (long long)buf[i] * buf[i];
    }
    int rms = samples ? (int)sqrt((double)sq / samples) : 0;
    ESP_LOGI(TAG, "Mic-Pegel: rms=%d peak=%d clip=%d/%d%s", rms, peak, clip, samples,
             clip > samples / 50 ? "  ⚠ Übersteuerung!" : "");
}
#endif

int audio_read_raw(int16_t *buf, int samples) {
    if (esp_codec_dev_read(s_rec, buf, samples * 2) == ESP_CODEC_DEV_OK) {
#if JARVIS_AUDIO_DIAG
        diag_levels(buf, samples);
#endif
        return samples;
    }
    return 0;
}

void audio_write(const int16_t *buf, int samples) {
    // Software-Lautstärke anwenden; bei Stereo-I2S Mono → L/R duplizieren.
    static int16_t tmp[2048];                  // bis 1024 Eingangs-Samples je Durchlauf (×2 bei Stereo)
    float g = s_gain;
#if JARVIS_AUDIO_DIAG
    int in_peak = 0, out_peak = 0;             // Diagnose: wirkt der Gain?
#endif
    int i = 0;
    while (i < samples) {
        int n = samples - i;
        if (n > 1024) n = 1024;
        int w = 0;
        for (int k = 0; k < n; k++) {
            int s = buf[i + k];
            int v = (int)(s * g);
            if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
#if JARVIS_AUDIO_DIAG
            int as = s < 0 ? -s : s, av = v < 0 ? -v : v;
            if (as > in_peak) in_peak = as;
            if (av > out_peak) out_peak = av;
#endif
            tmp[w++] = (int16_t)v;
#if JARVIS_DUAL_MIC
            tmp[w++] = (int16_t)v;             // zweiter Kanal (Stereo-I2S)
#endif
        }
        esp_codec_dev_write(s_play, tmp, w * 2);
        i += n;
    }
#if JARVIS_AUDIO_DIAG
    static int64_t last = 0;
    int64_t now = esp_timer_get_time() / 1000;
    if (now - last >= 1000 && in_peak > 0) {   // höchstens 1×/s, nur wenn Audio läuft
        last = now;
        ESP_LOGI(TAG, "TTS-Ausgabe: gain=%.2f in_peak=%d out_peak=%d", (double)g, in_peak, out_peak);
    }
#endif
}

void audio_set_mic_gain(float db) {
    if (db < 0.0f)  db = 0.0f;
    if (db > 42.0f) db = 42.0f;                              // ES7210-Bereich begrenzen
    if (s_rec) esp_codec_dev_set_in_gain(s_rec, db);
    ESP_LOGI(TAG, "Mic-Gain %.1f dB", (double)db);
}

void audio_set_volume(int percent) {
    if (percent > JARVIS_VOL_MAX) percent = JARVIS_VOL_MAX;   // Verstärkerschutz
    if (percent < 0) percent = 0;
    s_gain = percent / 100.0f;                               // zuverlässige Software-Dämpfung
    esp_codec_dev_set_out_vol(s_play, percent);              // zusätzlich Codec-Lautstärke
    ESP_LOGI(TAG, "Lautstärke %d %% (gain %.2f)", percent, (double)s_gain);
}

void audio_play_tone(bool rising) {
    const int sr = JARVIS_SAMPLE_RATE, dur_ms = 120, n = sr * dur_ms / 1000;
    static int16_t buf[JARVIS_SAMPLE_RATE * 120 / 1000];
    float f0 = rising ? 660.0f : 880.0f, f1 = rising ? 990.0f : 660.0f;
    for (int i = 0; i < n; i++) {
        float t = (float)i / n, f = f0 + (f1 - f0) * t;
        float env = fminf(1.0f, fminf(i / (n * 0.1f), (n - i) / (n * 0.1f)));
        buf[i] = (int16_t)(0.5f * env * 32767.0f * sinf(2.0f * (float)M_PI * f * i / sr));
    }
    audio_write(buf, n);                       // nutzt Software-Lautstärke + Stereo-Dup
}
