// SH-Jarvis ESP32-S3 Audio-Satellit - App-Logik & WS-Protokoll
//
// Ablauf: WLAN (oder SoftAP-Provisioning) -> WebSocket /ws/satellite -> hello.
// Loop: Mic-Frames in microWakeWord; bei "Jarvis": audio_start, Frames streamen bis Stille,
// audio_end. Antwort (state + TTS-PCM) kommt ueber den WS-Event-Handler und wird ausgegeben.
//
// Board-spezifische Teile (Codec/Pins, Wake-Word-Modell) liegen in audio_codec.c / wakeword.c.

#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_websocket_client.h"
#include "cJSON.h"
#include "jarvis_config.h"
#include "hardware.h"

static const char *TAG = "jarvis";

static esp_websocket_client_handle_t s_ws;
static jarvis_cfg_t s_cfg;
static volatile jarvis_state_t s_state = ST_IDLE;
static volatile bool s_busy = false;          // true: Aufnahme/Antwort laeuft -> Wake-Word pausieren
static char s_session_id[24] = {0};

static volatile bool s_tts_finished = false;
static volatile int64_t s_tts_activity_ms = 0;
static StreamBufferHandle_t s_audio_rb = NULL;     // TTS-Empfang → Wiedergabe
static StreamBufferHandle_t s_uplink_rb = NULL;    // Aufnahme → WS-Versand (entkoppelt den Fetch-Loop)

static int16_t *s_feed_buf = NULL;   // Roh-Mic-Puffer für die AFE (Mono/Dual interleaved)
static int s_feed_n = 0;             // Größe in int16
static volatile bool s_afe_run = true;  // AFE aktiv? (während TTS-Wiedergabe aus → CPU frei, glattes Audio)

#define TTS_BUFFER_BYTES        (512 * 1024)
#define TTS_QUEUE_WAIT_MS       5000
#define TTS_IDLE_POLL_MS        20
#define TTS_STALL_TIMEOUT_MS    45000

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

static void mark_busy(void) {
    s_busy = true;
    s_tts_activity_ms = now_ms();
}

static void clear_busy(void) {
    s_busy = false;
    s_tts_activity_ms = 0;
    s_afe_run = true;             // Wiedergabe fertig/abgebrochen → AFE (Wake-Word) wieder aktivieren
}

// Lautstaerke: immer hart auf JARVIS_VOL_MAX clampen (Verstaerkerschutz)
static void apply_volume(int percent) {
    if (percent < 0) percent = 0;
    if (percent > JARVIS_VOL_MAX) percent = JARVIS_VOL_MAX;
    s_cfg.volume = percent;
    audio_set_volume(percent);
    leds_show_volume(percent);
    cfg_save(&s_cfg);
    ESP_LOGI(TAG, "Lautstaerke %d %%", percent);
}

static void set_state(jarvis_state_t st) {
    s_state = st;
    leds_set_state(st);
}

static void ws_send_text(const char *json) {
    if (s_ws && esp_websocket_client_is_connected(s_ws)) {
        esp_websocket_client_send_text(s_ws, json, strlen(json), pdMS_TO_TICKS(5000));
    }
}

// Lebenszeichen + Telemetrie -> Admin-Geraeteliste (Raum, Lautstaerke, WLAN-Signal, FW).
static void send_heartbeat(void) {
    int rssi = 0;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
    char hb[256];
    snprintf(hb, sizeof(hb),
             "{\"type\":\"heartbeat\",\"room\":\"%s\",\"volume\":%d,\"mic_gain\":%.1f,\"rssi\":%d,\"fw\":\"%s\"}",
             s_cfg.room, s_cfg.volume, (double)s_cfg.mic_gain, rssi, JARVIS_FW_VERSION);
    ws_send_text(hb);
}

static void reset_tts_buffer(void) {
    if (s_audio_rb) xStreamBufferReset(s_audio_rb);
    s_tts_finished = false;
}

static void queue_tts_audio(const char *data, int len) {
    if (!s_audio_rb || !data || len <= 0) return;

    mark_busy();
    set_state(ST_SPEAKING);

    size_t queued = 0;
    while (queued < (size_t)len) {
        size_t sent = xStreamBufferSend(
            s_audio_rb,
            data + queued,
            (size_t)len - queued,
            pdMS_TO_TICKS(TTS_QUEUE_WAIT_MS)
        );
        if (sent == 0) {
            ESP_LOGW(TAG, "TTS-Puffer voll, konnte %u Bytes nicht puffern", (unsigned)((size_t)len - queued));
            break;
        }
        queued += sent;
        s_tts_activity_ms = now_ms();
    }
}

static void on_ws_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    esp_websocket_event_data_t *ev = (esp_websocket_event_data_t *)data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED: {
        ESP_LOGI(TAG, "WS verbunden");
        char hello[224];
        snprintf(hello, sizeof(hello),
                 "{\"type\":\"hello\",\"name\":\"%s\",\"session_id\":\"%s\",\"volume\":%d,\"fw\":\"%s\"}",
                 s_cfg.room, s_session_id, s_cfg.volume, JARVIS_FW_VERSION);
        ws_send_text(hello);
        send_heartbeat();
        set_state(ST_IDLE);
        break;
    }
    case WEBSOCKET_EVENT_DATA:
        if (ev->op_code == 0x02) {                 // BINARY -> TTS-PCM puffern
            queue_tts_audio(ev->data_ptr, ev->data_len);
        } else if (ev->op_code == 0x01 && ev->data_len > 1) {   // TEXT -> JSON
            cJSON *j = cJSON_ParseWithLength(ev->data_ptr, ev->data_len);
            if (!j) break;
            const cJSON *t = cJSON_GetObjectItem(j, "type");
            const char *type = cJSON_IsString(t) ? t->valuestring : "";
            if (!strcmp(type, "welcome")) {
                const cJSON *s = cJSON_GetObjectItem(j, "session_id");
                if (cJSON_IsString(s)) strncpy(s_session_id, s->valuestring, sizeof(s_session_id) - 1);
            } else if (!strcmp(type, "tts_start")) {
                reset_tts_buffer();
                mark_busy();
                set_state(ST_SPEAKING);
            } else if (!strcmp(type, "state")) {
                const cJSON *s = cJSON_GetObjectItem(j, "state");
                if (cJSON_IsString(s)) {
                    if (!strcmp(s->valuestring, "thinking"))      set_state(ST_THINKING);
                    else if (!strcmp(s->valuestring, "speaking")) set_state(ST_SPEAKING);
                    else                                          set_state(ST_IDLE);
                }
            } else if (!strcmp(type, "tts_end")) {
                s_tts_finished = true;
                s_tts_activity_ms = now_ms();
            } else if (!strcmp(type, "set_volume")) {
                const cJSON *l = cJSON_GetObjectItem(j, "level");
                const cJSON *p = cJSON_GetObjectItem(j, "percent");
                if (cJSON_IsNumber(p)) {                      // direkter Prozentwert (Admin-UI)
                    ESP_LOGW(TAG, ">>> set_volume empfangen: percent=%d", (int)p->valuedouble);
                    apply_volume((int)p->valuedouble);
                } else if (cJSON_IsNumber(l)) {               // Stufe 1..10 (Sprachbefehl)
                    ESP_LOGW(TAG, ">>> set_volume empfangen: level=%d", (int)l->valuedouble);
                    apply_volume(((int)l->valuedouble) * 10);
                } else {
                    ESP_LOGW(TAG, ">>> set_volume empfangen, aber weder level noch percent!");
                }
            } else if (!strcmp(type, "set_mic_gain")) {
                const cJSON *d = cJSON_GetObjectItem(j, "db");
                if (cJSON_IsNumber(d)) {
                    float db = (float)d->valuedouble;
                    ESP_LOGW(TAG, ">>> set_mic_gain empfangen: %.1f dB", (double)db);
                    s_cfg.mic_gain = db;
                    audio_set_mic_gain(db);
                    cfg_save(&s_cfg);
                }
            } else if (!strcmp(type, "timer_alarm") || !strcmp(type, "notify")) {
                audio_play_tone(true);
                vTaskDelay(pdMS_TO_TICKS(120));
                audio_play_tone(true);
            }
            cJSON_Delete(j);
        }
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WS getrennt");
        clear_busy();
        reset_tts_buffer();
        set_state(ST_IDLE);
        break;
    default:
        break;
    }
}

// Aufnahme bis Stille -> AFE-verarbeitetes Mono-Audio streamen (NS/AGC angewandt; AFE-VAD beendet)
static void capture_and_stream(void) {
    mark_busy();
    set_state(ST_LISTENING);
    audio_play_tone(true);
    ws_send_text("{\"type\":\"audio_start\"}");

    afe_out_t out;
    int64_t start = now_ms(), last_speech = start;
    int started = 0;
    // Mic-Lesen läuft im Feed-Task; hier nur die AFE-Frames abholen und in den Uplink-Puffer geben.
    // Der eigentliche WS-Versand passiert im uplink_task → der Fetch-Loop stockt nie (kein Feed-Overflow).
    while (now_ms() - start < JARVIS_MAX_UTTER_MS) {
        if (!afe_fetch(&out)) continue;
        if (out.audio && out.samples > 0 && s_uplink_rb) {
            xStreamBufferSend(s_uplink_rb, out.audio, out.samples * 2, pdMS_TO_TICKS(20));
        }
        if (out.speech) {
            started = 1;
            last_speech = now_ms();
        } else if (started && now_ms() - last_speech > JARVIS_SILENCE_MS) {
            break;
        }
    }
    // Uplink leeren lassen, damit audio_end erst nach dem letzten Audio kommt (max. 2 s warten).
    int64_t flush_start = now_ms();
    while (s_uplink_rb && xStreamBufferBytesAvailable(s_uplink_rb) > 0 && now_ms() - flush_start < 2000) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ws_send_text("{\"type\":\"audio_end\"}");
    audio_play_tone(false);
    s_afe_run = false;            // AFE pausieren → volle CPU für die TTS-Wiedergabe (glattes Audio)
    set_state(ST_THINKING);
    // s_busy bleibt true bis tts_end vom Server kommt und der Playback-Task den Puffer geleert hat.
}

// Uplink: aufgenommenes Audio entkoppelt vom Fetch-Loop über die WS senden.
// Ein langsamer/blockierender WS-Versand bremst so nur diesen Task, nicht die AFE.
static void uplink_task(void *arg) {
    static uint8_t buf[2048];
    while (1) {
        size_t n = xStreamBufferReceive(s_uplink_rb, buf, sizeof(buf), portMAX_DELAY);
        if (n > 0 && s_ws && esp_websocket_client_is_connected(s_ws)) {
            esp_websocket_client_send_bin(s_ws, (const char *)buf, n, pdMS_TO_TICKS(3000));
        }
    }
}

// TTS-Wiedergabe: entkoppelt WebSocket-Empfang von der langsameren Audioausgabe.
static void playback_task(void *arg) {
    uint8_t chunk[JARVIS_AUDIO_CHUNK];
    while (1) {
        size_t bytes = xStreamBufferReceive(
            s_audio_rb,
            chunk,
            sizeof(chunk),
            pdMS_TO_TICKS(TTS_IDLE_POLL_MS)
        );

        if (bytes > 0) {
            if (bytes & 1) bytes--;
            if (bytes > 0) audio_write((const int16_t *)chunk, bytes / 2);
            continue;
        }

        if (s_tts_finished && xStreamBufferBytesAvailable(s_audio_rb) == 0) {
            s_tts_finished = false;
            clear_busy();
            set_state(ST_IDLE);
        } else if (s_busy && !s_tts_finished && s_tts_activity_ms > 0 && now_ms() - s_tts_activity_ms > TTS_STALL_TIMEOUT_MS) {
            ESP_LOGW(TAG, "TTS/Antwort Timeout, gebe Wake-Word wieder frei");
            reset_tts_buffer();
            clear_busy();
            set_state(ST_IDLE);
        }
    }
}

// Feed-Task: Mikrofon kontinuierlich leeren und in die AFE schieben (darf NIE pausieren,
// sonst staut sich der I2S-RX- bzw. AFE-Feed-Ringpuffer). Läuft die ganze Zeit, auch bei TTS.
static void feed_task(void *arg) {
    while (1) {
        int n = audio_read_raw(s_feed_buf, s_feed_n);   // Mic IMMER leeren (sonst I2S-Overflow)
        if (n > 0) {
            if (s_afe_run) afe_feed(s_feed_buf, n);      // bei TTS-Wiedergabe nur verwerfen → AFE-CPU frei
        } else {
            vTaskDelay(1);
        }
    }
}

// Haupt-Task: AFE-Frames abholen → Wake-Word. fetch() blockiert bis Frame bereit (Echtzeit-Takt),
// daher kein Busy-Wait. Es wird IMMER gefetcht (sonst Feed-Overflow); bei TTS nur ignoriert.
static void voice_task(void *arg) {
    int64_t last_hb = 0;
    afe_out_t out;
    while (1) {
        int64_t now = now_ms();
        if (!s_busy && now - last_hb > JARVIS_HEARTBEAT_MS) {   // nicht während Aufnahme/TTS senden
            send_heartbeat();
            last_hb = now;
        }
        if (!s_afe_run) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }   // AFE pausiert (TTS läuft)
        if (!afe_fetch(&out)) continue;
        if (s_busy) continue;                           // während Antwort/TTS nicht auf Wake-Word lauschen
        if (out.wake) {
            ESP_LOGI(TAG, "Wake-Word Jarvis erkannt");
            capture_and_stream();
        }
    }
}

void app_main(void) {
    leds_init();
    set_state(ST_SETUP);

    if (!cfg_load(&s_cfg)) {
        memset(&s_cfg, 0, sizeof(s_cfg));
        s_cfg.volume = JARVIS_VOL_DEFAULT;
    }
    if (s_cfg.volume <= 0 || s_cfg.volume > JARVIS_VOL_MAX) s_cfg.volume = JARVIS_VOL_DEFAULT;
    if (s_cfg.mic_gain <= 0.0f || s_cfg.mic_gain > 42.0f) s_cfg.mic_gain = JARVIS_MIC_GAIN_DB;

    if (!wifi_start_or_provision(&s_cfg)) {
        ESP_LOGI(TAG, "Provisioning aktiv (SoftAP). Nach Setup folgt Reboot.");
        return;
    }
    esp_wifi_set_ps(WIFI_PS_NONE);   // kein Modem-Sleep → niedrige WS-Latenz (sonst stockt der Audio-Versand)

    audio_init();
    s_audio_rb = xStreamBufferCreateWithCaps(TTS_BUFFER_BYTES, 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_audio_rb) {
        ESP_LOGW(TAG, "PSRAM-TTS-Puffer nicht verfuegbar, nutze kleineren internen Puffer");
        s_audio_rb = xStreamBufferCreate(64 * 1024, 1);
    }
    if (!s_audio_rb) {
        ESP_LOGE(TAG, "StreamBuffer konnte nicht erstellt werden");
        return;
    }
    s_uplink_rb = xStreamBufferCreateWithCaps(64 * 1024, 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_uplink_rb) s_uplink_rb = xStreamBufferCreate(32 * 1024, 1);
    if (!s_uplink_rb) { ESP_LOGE(TAG, "Uplink-Puffer konnte nicht erstellt werden"); return; }

    apply_volume(s_cfg.volume);
    audio_set_mic_gain(s_cfg.mic_gain);
    wakeword_init();

    s_feed_n = afe_feed_samples();
    s_feed_buf = heap_caps_malloc(s_feed_n * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_feed_buf) s_feed_buf = malloc(s_feed_n * sizeof(int16_t));
    if (!s_feed_buf) {
        ESP_LOGE(TAG, "AFE-Feed-Puffer konnte nicht erstellt werden");
        return;
    }

    if (!s_session_id[0]) {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        snprintf(s_session_id, sizeof(s_session_id), "esp%02x%02x%02x%02x%02x%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }

    char uri[160];
    snprintf(uri, sizeof(uri), "%s/ws/satellite", s_cfg.server);
    esp_websocket_client_config_t wcfg = {
        .uri = uri,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = 60000,
        .ping_interval_sec = 30,
        .skip_cert_common_name_check = true,
        .disable_pingpong_discon = true,
        .keep_alive_enable = true,
    };
    s_ws = esp_websocket_client_init(&wcfg);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, on_ws_event, NULL);
    esp_websocket_client_start(s_ws);

    set_state(ST_IDLE);
    ESP_LOGI(TAG, "Bereit - sage Jarvis. Raum=%s Server=%s", s_cfg.room, s_cfg.server);
    xTaskCreate(playback_task, "tts_play", 8192, NULL, 7, NULL);  // höchste Prio → kein Audio-Unterlauf
    xTaskCreate(uplink_task, "uplink", 4096, NULL, 6, NULL);   // Aufnahme→WS (entkoppelt)
    xTaskCreate(feed_task, "afe_feed", 4096, NULL, 6, NULL);   // Mic→AFE, läuft dauerhaft
    xTaskCreate(voice_task, "voice", 8192, NULL, 5, NULL);     // AFE→Wake/Stream
}