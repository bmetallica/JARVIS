// WLAN: STA-Verbindung (mit IP-Warten) oder SoftAP-Captive-Portal zur Erstkonfiguration.
// NVS speichert: ssid, psk, server-URL, raumname, lautstärke.
#include <string.h>
#include <stdio.h>
#include "hardware.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

static const char *TAG = "wifi";
#define NS "jarvis"
static EventGroupHandle_t s_eg;
#define GOT_IP BIT0

static void ensure_nvs(void) {
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) { nvs_flash_erase(); nvs_flash_init(); }
}

bool cfg_load(jarvis_cfg_t *out) {
    ensure_nvs(); nvs_handle_t h; memset(out, 0, sizeof(*out)); out->volume = JARVIS_VOL_DEFAULT;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    size_t n; bool ok = false;
    n = sizeof(out->ssid);   ok = (nvs_get_str(h, "ssid", out->ssid, &n) == ESP_OK) && out->ssid[0];
    n = sizeof(out->psk);    nvs_get_str(h, "psk",    out->psk,    &n);
    n = sizeof(out->server); nvs_get_str(h, "server", out->server, &n);
    n = sizeof(out->room);   nvs_get_str(h, "room",   out->room,   &n);
    int32_t v = JARVIS_VOL_DEFAULT; nvs_get_i32(h, "vol", &v); out->volume = v;
    int32_t mg = 0; nvs_get_i32(h, "micg", &mg); out->mic_gain = mg / 10.0f;  // zehntel-dB
    nvs_close(h); return ok;
}

void cfg_save(const jarvis_cfg_t *c) {
    ensure_nvs(); nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, "ssid", c->ssid); nvs_set_str(h, "psk", c->psk);
    nvs_set_str(h, "server", c->server); nvs_set_str(h, "room", c->room);
    nvs_set_i32(h, "vol", c->volume);
    nvs_set_i32(h, "micg", (int32_t)(c->mic_gain * 10.0f + 0.5f));
    nvs_commit(h); nvs_close(h);
}

void cfg_factory_reset(void) {
    ensure_nvs(); nvs_handle_t h;
    if (nvs_open(NS, NVS_READWRITE, &h) == ESP_OK) { nvs_erase_all(h); nvs_commit(h); nvs_close(h); }
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
    else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) xEventGroupSetBits(s_eg, GOT_IP);
}

// ── Captive-Portal ────────────────────────────────────────────────────────────
static esp_err_t portal_get(httpd_req_t *r) {
    static const char *html =
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<h2>JARVIS Satellit – Einrichtung</h2><form method=POST action=/save>"
        "WLAN-SSID:<br><input name=ssid><br>Passwort:<br><input name=psk type=password><br>"
        "Orchestrator-URL:<br><input name=server value='wss://192.168.66.224:8088' size=40><br>"
        "Raumname:<br><input name=room value=Wohnzimmer><br><br>"
        "<button>Speichern &amp; Neustart</button></form>";
    httpd_resp_send(r, html, HTTPD_RESP_USE_STRLEN); return ESP_OK;
}

static void urldec(char *s) {  // simple in-place URL decode
    char *o = s; for (char *p = s; *p; p++) {
        if (*p == '+') *o++ = ' ';
        else if (*p == '%' && p[1] && p[2]) { int h; sscanf(p + 1, "%2x", &h); *o++ = (char)h; p += 2; }
        else *o++ = *p;
    } *o = 0;
}
static void field(const char *body, const char *key, char *out, int n) {
    char pat[24]; snprintf(pat, sizeof(pat), "%s=", key);
    const char *p = strstr(body, pat); if (!p) { out[0] = 0; return; }
    p += strlen(pat); const char *e = strchr(p, '&'); int len = e ? (int)(e - p) : (int)strlen(p);
    if (len >= n) len = n - 1;
    memcpy(out, p, len); out[len] = 0; urldec(out);
}

static void deferred_restart_task(void *arg) {
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t portal_save(httpd_req_t *r) {
    char body[512]; int len = httpd_req_recv(r, body, sizeof(body) - 1);
    if (len <= 0) return ESP_FAIL;
    body[len] = 0;
    jarvis_cfg_t c = {0}; c.volume = JARVIS_VOL_DEFAULT;
    field(body, "ssid", c.ssid, sizeof(c.ssid)); field(body, "psk", c.psk, sizeof(c.psk));
    field(body, "server", c.server, sizeof(c.server)); field(body, "room", c.room, sizeof(c.room));
    cfg_save(&c);
    httpd_resp_sendstr(r, "<h3>Gespeichert. Neustart...</h3>");
    xTaskCreate(deferred_restart_task, "restart_task", 2048, NULL, 5, NULL);
    return ESP_OK;
}

static void start_portal(void) {
    httpd_handle_t s; httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    if (httpd_start(&s, &cfg) == ESP_OK) {
        httpd_register_uri_handler(s, &(httpd_uri_t){ .uri = "/",     .method = HTTP_GET,  .handler = portal_get });
        httpd_register_uri_handler(s, &(httpd_uri_t){ .uri = "/save", .method = HTTP_POST, .handler = portal_save });
    }
}

bool wifi_start_or_provision(jarvis_cfg_t *cfg) {
    ensure_nvs(); esp_netif_init(); esp_event_loop_create_default();
    s_eg = xEventGroupCreate();
    wifi_init_config_t wc = WIFI_INIT_CONFIG_DEFAULT(); esp_wifi_init(&wc);
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL, NULL);

    if (cfg->ssid[0]) {
        esp_netif_create_default_wifi_sta();
        wifi_config_t sta = {0};
        strncpy((char *)sta.sta.ssid, cfg->ssid, sizeof(sta.sta.ssid) - 1);
        strncpy((char *)sta.sta.password, cfg->psk, sizeof(sta.sta.password) - 1);
        esp_wifi_set_mode(WIFI_MODE_STA); esp_wifi_set_config(WIFI_IF_STA, &sta);
        esp_wifi_start();
        esp_wifi_connect(); // WICHTIG: Die Verbindung explizit anstoßen!
        EventBits_t b = xEventGroupWaitBits(s_eg, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(20000));
        if (b & GOT_IP) { ESP_LOGI(TAG, "STA verbunden (%s)", cfg->ssid); return true; }
        ESP_LOGW(TAG, "STA-Verbindung fehlgeschlagen → Provisioning.");
    }

    // SoftAP + Captive-Portal
    uint8_t mac[6]; esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    char ssid[40]; snprintf(ssid, sizeof(ssid), "%s-%02X%02X", JARVIS_AP_PREFIX, mac[4], mac[5]);
    esp_netif_create_default_wifi_ap();
    wifi_config_t ap = {0};
    strncpy((char *)ap.ap.ssid, ssid, sizeof(ap.ap.ssid) - 1);
    ap.ap.ssid_len = strlen(ssid); ap.ap.max_connection = 2; ap.ap.authmode = WIFI_AUTH_OPEN;
    esp_wifi_set_mode(WIFI_MODE_AP); esp_wifi_set_config(WIFI_IF_AP, &ap); esp_wifi_start();
    start_portal();
    ESP_LOGW(TAG, "SoftAP '%s' aktiv → http://192.168.4.1 öffnen.", ssid);
    return false;
}
