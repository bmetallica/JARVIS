idf_component_register(
    SRCS "jarvis_satellite.c" "audio_codec.c" "wakeword.c" "leds.c" "wifi_prov.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_driver_i2s esp_driver_i2c esp_wifi nvs_flash esp_http_server esp_netif esp_event
             cjson esp-tls esp_timer
             esp_websocket_client led_strip esp-sr esp_codec_dev
)
