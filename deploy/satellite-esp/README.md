# SH-Jarvis — ESP32-S3 Audio-Satellit (Firmware)

Firmware für das **Waveshare ESP32-S3-AUDIO-Board** (ES7210 Mic-ADC, ES8311 Speaker-DAC, 7× WS2812).
Wake-Word **„Jarvis"** lokal → Audio streamen zum Orchestrator (`/ws/satellite`) → Antwort über den Lautsprecher.

> ✅ **Status:** Auf dem Waveshare ESP32-S3-AUDIO-Board **gebaut, geflasht und getestet** — Wake-Word,
> Aufnahme/Streaming, TTS-Wiedergabe sowie Lautstärke- und Mic-Gain-Steuerung laufen.
>
> ⚠️ **Wichtig — CPU auf 240 MHz:** Die 2-Mikrofon-AFE (BSS + VAD + WakeNet + AGC) braucht die volle
> Taktrate. Steht die CPU auf 160 MHz, hinkt der interne AFE-Task hinterher → der Feed-Ringpuffer läuft
> über (`AFE(FEED) is full`). In `sdkconfig.defaults` ist bereits `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y`
> gesetzt; falls eine ältere `sdkconfig` existiert, diese löschen **oder** in `menuconfig` unter
> *ESP System Settings → CPU frequency → 240 MHz* setzen. Im Boot-Log muss `cpu freq: 240000000 Hz` stehen.

## Architektur / Protokoll (passt 1:1 zum Server)
WebSocket `wss://<orchestrator>:8088/ws/satellite`, JSON-Steuerung + Binär-Audio (PCM **s16le mono 16 kHz**):
- **ESP→Server:** `{"type":"hello","session_id","name","volume","fw"}` · `{"type":"audio_start"}` · *(binäre PCM-Frames)* · `{"type":"audio_end"}` · `{"type":"heartbeat","room","volume","rssi","fw"}` *(alle 15 s → Admin-Geräteliste)*
- **ESP→Server (Telemetrie):** im Heartbeat zusätzlich `mic_gain` (dB) → Admin-Geräteliste.
- **Server→ESP:** `{"type":"welcome","session_id"}` · `{"type":"transcript","text","speaker"}` · `{"type":"state","state":"thinking|speaking|idle"}` · `{"type":"tts_start","sr":16000}` · *(getaktete binäre PCM-Frames)* · `{"type":"tts_end"}` · `{"type":"set_volume","level":1..10}` *(Sprache)* oder `{"type":"set_volume","percent":0..90}` *(Admin-UI)* · `{"type":"set_mic_gain","db":0..42}` *(Admin-UI)* · `{"type":"timer_alarm","message"}` · `{"type":"notify","message"}`

> Der Server **taktet den TTS-Stream** (kleine Pause pro Frame) statt ihn im Burst zu senden — sonst kann
> der ESP den TLS-Empfang nicht schnell genug leeren und die WS-Verbindung bricht mitten im TTS ab.

## Funktionsumfang
- **Wake-Word „Jarvis"** (microWakeWord, on-device) + AFE (AEC/NS/VAD) → Barge-In.
- **WLAN-Einrichtung via SoftAP** (Captive-Portal `Jarvis-Setup-XXXX`, wie Tasmota): SSID/PW, Orchestrator-URL, Raumname → NVS.
- **7× RGB-LED-Status** (Setup/Idle/Listening/Thinking/Speaking/Volume/Error).
- **Rückkanal**: Timer-Alarme/Benachrichtigungen werden hier gesprochen; **Lautstärke per Sprache** („Jarvis, Lautstärke 1–10").
- **Lautstärke-Schutz:** Default **50 %**, **harte Obergrenze 90 %** (sonst Verstärker-Defekt). In NVS persistiert.
- Quittungstöne, Reconnect, Heartbeat, Factory-Reset (Taster lang), OTA-Hook.

## Komponenten / Abhängigkeiten (`main/idf_component.yml`)
Werden beim ersten Build automatisch vom **ESP Component Manager** geladen:
- `espressif/esp-sr` (AFE + WakeNet, „hey_jarvis"-Modell)
- `espressif/esp_codec_dev` (ES8311/ES7210-Treiber)
- `espressif/esp_websocket_client`
- `espressif/led_strip` (WS2812)

WLAN-Provisioning (SoftAP + Captive-Portal) ist mit `esp_http_server` **selbst implementiert**
(`wifi_prov.c`) — keine externe Komponente nötig.

## Build / Flash — mit VS Code
1. **ESP-IDF-Extension** in VS Code installieren (Marketplace: „Espressif IDF"), darin **ESP-IDF v5.1+**
   einrichten („Configure ESP-IDF Extension" → Express).
2. Ordner `satellite-esp/` in VS Code öffnen.
3. Unten in der Statusleiste **Target = `esp32s3`** wählen (oder Befehl „ESP-IDF: Set Espressif Device Target").
4. **Build** (🔧-Symbol bzw. „ESP-IDF: Build your project"). Der Component Manager lädt esp-sr usw.
   automatisch und baut das Wake-Word-Modell in die `model`-Partition.
5. Board per USB anstecken, **COM-Port** wählen, **Flash** (⚡) und **Monitor** (🖥) starten.

### Alternativ auf der Kommandozeile
```bash
. $IDF_PATH/export.sh
idf.py set-target esp32s3
idf.py build flash monitor      # erstmaliger Build lädt die managed components
```
- Standardwerte (Volume 50 %, Cap 90 %, Wake „Jarvis") liegen in `jarvis_config.h`.
- Das Wake-Word-Modell `hey_jarvis` ist über `sdkconfig.defaults` (`CONFIG_SR_WN_WN9_HEYJARVIS=y`)
  vorausgewählt; in `menuconfig` unter „ESP Speech Recognition" prüfbar.
- TLS: derzeit `skip_cert_common_name_check=true` (selbstsigniert, nur LAN). Für ein echtes Cert
  in `jarvis_satellite.c` `cert_pem` setzen.

## Erste Inbetriebnahme
1. Flashen → Board spannt AP **`Jarvis-Setup-XXXX`** auf (LED blau).
2. Mit dem AP verbinden → Browser **`http://192.168.4.1`** → Formular: WLAN, Orchestrator-URL
   (`wss://192.168.66.224:8088`), Raumname → Speichern.
3. Reboot → verbindet sich, LED idle. „**Jarvis**" sagen.

## Dateien
- `main/jarvis_satellite.c` — App-Logik, Zustandsautomat, WS-Protokoll, Lautstärke
- `main/jarvis_config.h` — Defaults & Board-Pins (Volume 50/90, I2C/I2S-Pins, Codec-Adressen)
- `main/audio_codec.c` — I2C + TCA9555-PA-Enable + I2S + ES8311/ES7210 via `esp_codec_dev`
- `main/wakeword.c` — esp-sr AFE + WakeNet („Jarvis") + AFE-VAD; getrenntes `afe_feed`/`afe_fetch` (Feed-/Fetch-Task)
- `main/leds.c` — 7× WS2812-Statusanzeige (`led_strip`)
- `main/wifi_prov.c` — STA-Verbindung + SoftAP-Captive-Portal (`esp_http_server`) + NVS-Config
- `main/CMakeLists.txt`, `main/idf_component.yml`, `CMakeLists.txt`, `partitions.csv`, `sdkconfig.defaults`

## Audio-Frontend (NS/AGC/VAD + Dual-Mic) — neu

Das Mikrofon läuft jetzt über die esp-sr **AFE**-Pipeline: Noise Suppression + (optional) AGC,
**AFE-VAD** statt Energie-Schwellwert, und es wird das **AFE-bereinigte Mono-Audio** zum Server
gestreamt. Konfiguration zentral in `main/jarvis_config.h`:

| Schalter | Wirkung |
|---|---|
| `JARVIS_DUAL_MIC` | **1 = beide Mikrofone** (Array, AFE „MM", I2S Stereo) · **0 = ein Mikro** (Mono, sicher) |
| `JARVIS_AFE_AGC`  | Auto-Gain-Control der Aufnahme |
| `JARVIS_AFE_AEC`  | Echo-Unterdrückung (Barge-In; CPU-intensiver) |
| `JARVIS_MIC_GAIN_DB` | ES7210-Eingangsverstärkung (dB) |
| `JARVIS_AUDIO_DIAG`  | RMS/Peak/Clipping ins Log (Pegel-Diagnose, alle 2 s) |

**Dual-Mic einfach umschalten:** in `jarvis_config.h` `JARVIS_DUAL_MIC` auf `1`/`0` setzen → neu bauen/flashen.
Falls nach dem Aktivieren Audio/Wake-Word zickt (Board-spezifische I2S/TDM-Eigenheiten), einfach `0`
(Mono) — das läuft sicher; der Server bekommt in beiden Fällen sauberes Mono.

**Lautstärke:** zusätzlich zur ES8311-Codec-Lautstärke wird die Ausgabe per **Software-Gain** gedämpft
(`audio_set_volume` → skaliert die PCM-Samples). Damit greift die Lautstärke zuverlässig, unabhängig von
Codec-Eigenheiten. Obergrenze `JARVIS_VOL_MAX` (90 %) bleibt hart.

**Mic-Gain:** `audio_set_mic_gain` setzt die ES7210-Eingangsverstärkung (0–42 dB), remote über die
Admin-UI einstellbar und in NVS persistiert. Default `JARVIS_MIC_GAIN_DB`.

### Task-Architektur (wichtig für stabiles Audio)
- **`feed_task`** liest das Mikrofon kontinuierlich und schiebt es in die AFE (darf nie pausieren — sonst I2S-/Feed-Overflow).
- **`voice_task`** holt AFE-Frames (`afe_fetch`) → Wake-Word; startet die Aufnahme.
- **`uplink_task`** sendet das aufgenommene Audio **entkoppelt** über die WS (blockierender Versand bremst so nie den Fetch-Loop → kein Feed-Overflow während des Streamens).
- **`playback_task`** (höchste Priorität) spielt empfangenes TTS-PCM ab → kein Audio-Unterlauf.
- Während der **TTS-Wiedergabe pausiert die AFE** (`s_afe_run`) → volle CPU fürs Abspielen, glatte Ausgabe.
- **WiFi-Power-Save ist aus** (`WIFI_PS_NONE`) → niedrige WS-Latenz, sonst stockt der Audio-Versand.

### Remote-Steuerung über die Admin-UI
Im Admin-UI (Tab **📡 Geräte**) lassen sich **Lautstärke (%)** und **Mic-Gain (dB)** pro verbundenem
Gerät direkt setzen (`POST /api/admin/devices/control` → Push an das Gerät). Unabhängig von der
Spracherkennung — praktisch zum Abstimmen anderer Mikrofone/Standorte.

> **Wake-Word reagiert mäßig?** Bei aktivem Dual-Mic bekommt das WakeNet das BSS-getrennte Signal, was die
> Erkennung verschlechtern kann. Dann `JARVIS_DUAL_MIC 0` (Mono) testen — sauberes, latenzarmes
> WakeNet-Signal, immer noch mit NS/AGC/VAD.
