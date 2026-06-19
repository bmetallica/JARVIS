// Mikrofon-Frontend über esp-sr AFE: Wake-Word „Jarvis" + Noise Suppression / AGC / VAD.
//
// Eine Pipeline für alles: rohe Mic-Samples → AFE.feed → AFE.fetch liefert
//   • wakeup_state  (Wake-Word erkannt)
//   • vad_state     (Sprache/Stille — ersetzt die alte Energie-VAD)
//   • data          (verarbeitetes EIN-Kanal-Audio, NS/AGC angewandt) → das streamen wir zum Server
//
// Mono ("M") oder Dual-Mic ("MM") über JARVIS_DUAL_MIC. Die NS/SE-Stufe ist im AFE_TYPE_SR-
// Default aktiv; AGC/AEC schalten wir per Config dazu.
#include <string.h>
#include "hardware.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_afe_sr_models.h"
#include "esp_wn_iface.h"
#include "model_path.h"

static const char *TAG = "wakeword";
static const esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_data;
static int s_feed_samples;      // int16 pro feed() (samples/Kanal * Kanäle)

void wakeword_init(void) {
    srmodel_list_t *models = esp_srmodel_init("model");
    // "M" = 1 Mikrofon, "MM" = 2 Mikrofone (Array). Kein AEC-Referenzkanal hier.
    afe_config_t *cfg = afe_config_init(JARVIS_DUAL_MIC ? "MM" : "M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    cfg->wakenet_init = true;
    cfg->vad_init = true;                 // AFE-VAD statt Energie-Schwellwert
    cfg->vad_mode = VAD_MODE_3;
    cfg->aec_init = JARVIS_AFE_AEC;
#if JARVIS_AFE_AGC
    cfg->agc_init = true;                 // Auto-Gain-Control der Aufnahme
#endif
    // Noise Suppression / Speech Enhancement: im AFE_TYPE_SR-Default bereits aktiv.

    s_afe = esp_afe_handle_from_config(cfg);
    s_data = s_afe->create_from_config(cfg);

    int chunk = s_afe->get_feed_chunksize(s_data);   // Samples pro Kanal
    int channels = s_afe->get_feed_channel_num(s_data);
    s_feed_samples = chunk * channels;
    ESP_LOGI(TAG, "AFE bereit: %s, chunk=%d/Kanal, Kanäle=%d → feed=%d int16, VAD+%s%s",
             JARVIS_DUAL_MIC ? "Dual-Mic(MM)" : "Mono(M)", chunk, channels, s_feed_samples,
             JARVIS_AFE_AGC ? "AGC" : "", JARVIS_AFE_AEC ? "+AEC" : "");
}

int afe_feed_samples(void) {
    return s_feed_samples;
}

// Feed-Task: rohe Mic-Samples in die AFE schieben. Blockt intern nur kurz.
void afe_feed(const int16_t *in, int samples) {
    if (s_afe && samples >= s_feed_samples) s_afe->feed(s_data, in);
}

// Fetch-Task: ein verarbeitetes Frame abholen. fetch() blockiert bis ein Ergebnis bereit ist
// → natürliche Echtzeit-Taktung, und der Feed-Ringpuffer läuft nicht über.
bool afe_fetch(afe_out_t *out) {
    out->wake = false;
    out->speech = false;
    out->audio = NULL;
    out->samples = 0;
    if (!s_afe) return false;

    afe_fetch_result_t *res = s_afe->fetch(s_data);
    if (!res || res->ret_value == ESP_FAIL) return false;

    out->wake = (res->wakeup_state == WAKENET_DETECTED);
    out->speech = (res->vad_state == VAD_SPEECH);
    if (res->data && res->data_size > 0) {
        out->audio = res->data;                 // verarbeitetes Mono (gültig bis zum nächsten fetch)
        out->samples = res->data_size / (int)sizeof(int16_t);
    }
    return true;
}
