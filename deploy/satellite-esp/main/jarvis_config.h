// SH-Jarvis ESP32-S3 Satellit — zentrale Defaults
#pragma once

// Audio
#define JARVIS_SAMPLE_RATE     16000          // PCM s16le mono 16 kHz (Server-Protokoll)
#define JARVIS_FRAME_SAMPLES   320            // 20 ms Frames (Fallback, wenn kein AFE)
#define JARVIS_AUDIO_CHUNK     3200           // 100 ms Sende-/Empfangs-Blöcke (Bytes: *2)

// ── Mikrofon-Frontend (esp-sr AFE: Noise Suppression / AGC / VAD, optional Dual-Mic) ──
// EINFACH UMSCHALTBAR: 1 = beide Mikrofone (Array, AFE "MM"), 0 = ein Mikrofon (AFE "M").
// Hinweis: Dual-Mic stellt I2S auf Stereo um (2 ADC-Kanäle); falls Audio/Wake-Word danach
// zickt, einfach wieder auf 0 setzen (Mono läuft sicher). Server bekommt immer sauberes Mono.
#define JARVIS_DUAL_MIC        1
#define JARVIS_MIC_CHANNELS    (JARVIS_DUAL_MIC ? 2 : 1)
#define JARVIS_AFE_AGC         1              // Auto-Gain-Control der Aufnahme (AFE)
#define JARVIS_AFE_AEC         0              // Echo-Unterdrückung (Barge-In; CPU-intensiv)
#define JARVIS_MIC_GAIN_DB     30.0f          // ES7210-Eingangsverstärkung (dB)
#define JARVIS_AUDIO_DIAG      1              // RMS/Peak/Clipping ins Log (Pegel-Diagnose)

// Wake-Word
#define JARVIS_WAKEWORD        "jarvis"       // microWakeWord-Modell „hey_jarvis"

// Firmware-Version & Heartbeat (→ Admin-Geräteliste)
#define JARVIS_FW_VERSION      "1.0.0"
#define JARVIS_HEARTBEAT_MS    15000          // Lebenszeichen-Intervall

// Lautstärke  ── KRITISCH: Verstärker-Schutz ──
#define JARVIS_VOL_DEFAULT     50             // Startlautstärke nach dem Flashen (%)
#define JARVIS_VOL_MAX         90             // HARTE Obergrenze (%) — NIE höher (sonst Verstärker-Defekt!)

// ── Board-Pins (Waveshare ESP32-S3-AUDIO-Board) ──────────────────────────────
#define BSP_I2C_PORT           0
#define BSP_I2C_SDA            11
#define BSP_I2C_SCL            10
#define BSP_I2S_MCLK           12
#define BSP_I2S_BCLK           13
#define BSP_I2S_LRCLK          14
#define BSP_I2S_DIN            15             // vom ES7210 (Mic)
#define BSP_I2S_DOUT           16             // zum ES8311 (Speaker)
#define ES8311_ADDR            0x18
#define ES7210_ADDR            0x40
#define TCA9555_ADDR           0x20           // I/O-Expander
#define TCA9555_PA_PIN         8              // Verstärker-Enable (PA_CTRL)

// LEDs (Waveshare: 7 WS2812)
#define JARVIS_LED_COUNT       7
#define JARVIS_LED_GPIO        38

// Provisioning
#define JARVIS_AP_PREFIX       "Jarvis-Setup"

// VAD: Sprechende beenden nach so viel Stille (ms)
#define JARVIS_SILENCE_MS      800
#define JARVIS_MAX_UTTER_MS    10000

// Zustände
typedef enum {
    ST_SETUP, ST_IDLE, ST_LISTENING, ST_THINKING, ST_SPEAKING, ST_ERROR
} jarvis_state_t;
