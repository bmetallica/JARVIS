// Hardware-Abstraktion — Implementierungen in audio_codec.c / wakeword.c / leds.c / wifi_prov.c.
// Diese Trennung hält die App-Logik (jarvis_satellite.c) board-unabhängig.
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "jarvis_config.h"

// ── Audio (ES7210 Mic / ES8311 Speaker via I2S, Waveshare-BSP) ────────────────
void    audio_init(void);                                  // I2S + Codecs initialisieren
// Rohe Mic-Samples lesen (blockierend). Bei Dual-Mic interleaved 2-kanalig → genau `samples` int16.
int     audio_read_raw(int16_t *buf, int samples);
void    audio_write(const int16_t *buf, int samples);      // Mono-TTS ausgeben (intern ggf. Stereo-Dup)
void    audio_set_volume(int percent);                     // 0..100 (App cappt vorher auf JARVIS_VOL_MAX)
void    audio_set_mic_gain(float db);                      // ES7210-Eingangsverstärkung (dB), remote einstellbar
void    audio_play_tone(bool rising);                      // Quittungston (rising=zuhören, falling=fertig)

// ── Mikrofon-Frontend: AFE (esp-sr) — Wake-Word + Noise Suppression/AGC/VAD ────
typedef struct {
    bool     wake;      // Wake-Word „Jarvis" erkannt
    bool     speech;    // AFE-VAD: Sprache aktiv (sonst Stille)
    int16_t *audio;     // verarbeitetes Mono-Audio (NS/AGC) — gültig bis zum nächsten Aufruf
    int      samples;   // Anzahl Samples in `audio` (0 = kein Ergebnis)
} afe_out_t;

void    wakeword_init(void);
// Anzahl int16 (bei Dual-Mic interleaved), die afe_feed pro Aufruf erwartet.
int     afe_feed_samples(void);
// Rohe Mic-Samples in die AFE schieben (Feed-Task — muss kontinuierlich laufen).
void    afe_feed(const int16_t *in, int samples);
// Ein verarbeitetes AFE-Frame abholen (Fetch-Task — blockiert bis Ergebnis bereit).
// Liefert Wake/VAD-Status + NS/AGC-bereinigtes Mono-Audio. false = kein gültiges Frame.
bool    afe_fetch(afe_out_t *out);

// ── LEDs (WS2812) ─────────────────────────────────────────────────────────────
void    leds_init(void);
void    leds_set_state(jarvis_state_t st);
void    leds_show_volume(int percent);

// ── WLAN/Provisioning + NVS-Config ────────────────────────────────────────────
typedef struct {
    char  ssid[64];
    char  psk[64];
    char  server[128];    // z.B. wss://192.168.66.224:8088
    char  room[48];       // Raumname
    int   volume;         // %
    float mic_gain;       // ES7210-Eingangsverstärkung (dB); 0 = Default JARVIS_MIC_GAIN_DB
} jarvis_cfg_t;

bool    cfg_load(jarvis_cfg_t *out);     // false → noch nicht eingerichtet
void    cfg_save(const jarvis_cfg_t *c);
void    cfg_factory_reset(void);

// Startet STA, wenn konfiguriert; sonst SoftAP-Captive-Portal. Blockiert bis verbunden (STA).
// Gibt false zurück, wenn Provisioning nötig war (Caller rebootet danach).
bool    wifi_start_or_provision(jarvis_cfg_t *cfg);
